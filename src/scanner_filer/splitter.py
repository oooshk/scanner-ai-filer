from __future__ import annotations

import logging
import re
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .config import SplitterConfig

logger = logging.getLogger(__name__)


def _page_head_text(page, max_chars: int) -> str:
    text = (page.extract_text() or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]


def _looks_like_new_document(curr: str, prev: str, keywords: list[str]) -> bool:
    if not curr:
        return False

    # Strong separator: many generated statements restart numbering from page 1.
    if re.search(r"\bpage\s*1\s*(of|/)\s*\d+\b", curr):
        return True

    curr_has_kw = any(k.lower() in curr for k in keywords)
    prev_has_kw = any(k.lower() in prev for k in keywords)
    if curr_has_kw and not prev_has_kw:
        return True

    # New-doc hints: fresh sender + date/account style header near page top.
    has_date = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", curr) is not None
    has_account = any(k in curr for k in ["account", "policy", "reference", "customer number"]) 
    return has_date and has_account


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
        if _looks_like_new_document(curr_head, prev_head, cfg.boundary_keywords):
            boundaries.append(i)
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
