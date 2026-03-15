from __future__ import annotations

import logging
import re
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .config import SplitterConfig

logger = logging.getLogger(__name__)

_HEAD_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "your", "you", "are", "was", "were", "have",
    "has", "will", "can", "not", "all", "any", "our", "out", "per", "www", "http", "https", "com",
    "ltd", "of", "to", "in", "on", "at", "by", "or", "is", "as", "be", "it", "an", "a",
}


def _page_head_text(page, max_chars: int) -> str:
    text = (page.extract_text() or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]


def _head_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9]{2,}", text.lower())
    return {w for w in words if w not in _HEAD_STOPWORDS}


def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return (inter / union) if union else 0.0


def _boundary_score(curr: str, prev: str, cfg: SplitterConfig) -> tuple[float, float]:
    if not curr:
        return 0.0, 1.0

    score = 0.0

    # Strong separator: many generated statements restart numbering from page 1.
    if re.search(r"\bpage\s*1\s*(of|/)\s*\d+\b", curr):
        score += 1.35

    # Letter/header patterns that often indicate document starts.
    if re.search(r"\b(dear\s+[a-z]|subject\s*:|to\s*:|from\s*:|invoice\s+no\.?|statement\s+period)\b", curr):
        score += 0.35

    keyword_hits = 0
    keyword_new_hits = 0
    for raw_kw in cfg.boundary_keywords:
        kw = str(raw_kw).strip().lower()
        if not kw:
            continue
        in_curr = kw in curr
        in_prev = kw in prev
        if in_curr:
            keyword_hits += 1
        if in_curr and not in_prev:
            keyword_new_hits += 1

    score += min(0.55, keyword_hits * 0.1)
    score += min(0.45, keyword_new_hits * 0.15)

    has_date = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", curr) is not None
    has_account = any(k in curr for k in ["account", "policy", "reference", "customer number", "statement", "invoice"])
    if has_date and has_account:
        score += 0.35

    curr_tokens = _head_tokens(curr)
    prev_tokens = _head_tokens(prev)
    similarity = _jaccard_similarity(curr_tokens, prev_tokens)
    if cfg.content_aware_enabled and similarity <= cfg.low_similarity_threshold:
        # Larger drop in lexical overlap increases split confidence.
        score += min(0.7, (cfg.low_similarity_threshold - similarity + 0.02) * 2.2)

    return score, similarity


def _next_available(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def split_pdf_if_needed(pdf_path: Path, cfg: SplitterConfig, delete_original: bool = True) -> list[Path]:
    if not cfg.enabled:
        return [pdf_path]

    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    if total < cfg.min_pages_to_split:
        return [pdf_path]

    boundaries = [0]
    prev_head = _page_head_text(reader.pages[0], cfg.max_first_page_chars)

    for i in range(1, total):
        curr_head = _page_head_text(reader.pages[i], cfg.max_first_page_chars)
        score, similarity = _boundary_score(curr_head, prev_head, cfg)
        if score >= cfg.boundary_score_threshold:
            boundaries.append(i)
            logger.info(
                "Split boundary detected in %s at page %s (score=%.2f sim=%.2f)",
                pdf_path.name,
                i + 1,
                score,
                similarity,
            )
        prev_head = curr_head

    if len(boundaries) == 1:
        return [pdf_path]

    boundaries.append(total)
    out_paths: list[Path] = []

    for idx in range(len(boundaries) - 1):
        start = boundaries[idx]
        end = boundaries[idx + 1]

        writer = PdfWriter()
        for page_idx in range(start, end):
            writer.add_page(reader.pages[page_idx])

        part_path = _next_available(pdf_path.with_name(f"{pdf_path.stem}_part{idx + 1}.pdf"))
        with part_path.open("wb") as f:
            writer.write(f)
        out_paths.append(part_path)

    logger.info("Split %s into %s part(s)", pdf_path.name, len(out_paths))
    if delete_original:
        pdf_path.unlink(missing_ok=True)
    return out_paths


def split_pdf_at_starts(pdf_path: Path, start_pages_one_based: list[int], delete_original: bool = False) -> list[Path]:
    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    if total == 0:
        return [pdf_path]

    starts = sorted(set(p for p in start_pages_one_based if 1 <= p <= total))
    if not starts:
        starts = [1]
    if starts[0] != 1:
        starts.insert(0, 1)

    if len(starts) == 1:
        return [pdf_path]

    bounds = starts + [total + 1]
    out_paths: list[Path] = []

    for idx in range(len(bounds) - 1):
        start = bounds[idx] - 1
        end = bounds[idx + 1] - 1
        writer = PdfWriter()
        for page_idx in range(start, end):
            writer.add_page(reader.pages[page_idx])

        part_path = _next_available(pdf_path.with_name(f"{pdf_path.stem}_manual_part{idx + 1}.pdf"))
        with part_path.open("wb") as f:
            writer.write(f)
        out_paths.append(part_path)

    if delete_original:
        pdf_path.unlink(missing_ok=True)

    logger.info("Manual split %s into %s part(s) at pages %s", pdf_path.name, len(out_paths), starts)
    return out_paths
