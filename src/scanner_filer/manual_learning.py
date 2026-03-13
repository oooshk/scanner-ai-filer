from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .ocr import extract_text

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "your", "you", "are", "was", "were", "have", "has",
    "will", "can", "not", "all", "any", "our", "out", "per", "www", "http", "https", "com", "ltd", "pdf",
    "statement", "document", "page", "of", "to", "in", "on", "at", "by", "or", "is", "as", "be", "it", "an", "a",
}


def _learning_path(cfg: AppConfig) -> Path:
    return cfg.paths.state / "manual_learning.json"


def _tokenize(text: str, limit: int = 24) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9._-]{2,}", text.lower())
    freq: dict[str, int] = {}
    for token in tokens:
        if token in STOPWORDS or token.isdigit() or len(token) < 3:
            continue
        freq[token] = freq.get(token, 0) + 1
    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [k for k, _ in ranked[:limit]]


def _normalize_keywords(keywords: list[str], limit: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for word in keywords:
        token = str(word).strip().lower()
        token = re.sub(r"\s+", " ", token)
        if len(token) < 3 or token in STOPWORDS or token in seen:
            continue
        if not re.fullmatch(r"[a-z0-9._ -]{3,50}", token):
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= limit:
            break
    return out


def _load_learning(cfg: AppConfig) -> dict:
    path = _learning_path(cfg)
    if not path.exists():
        return {"examples": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("examples"), list):
            return data
    except Exception:
        pass
    return {"examples": []}


def _save_learning(cfg: AppConfig, payload: dict) -> None:
    path = _learning_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True)


def record_manual_learning(
    cfg: AppConfig,
    *,
    target_type: str,
    target_sender: str,
    inbox_user: str | None,
    source_pdf: Path,
    seed_keywords: list[str] | None = None,
) -> bool:
    if target_type == cfg.rules.unknown_doc_type:
        return False

    base_keywords = _normalize_keywords(seed_keywords or [])
    if not base_keywords:
        text = extract_text(source_pdf, max_chars=2500)
        base_keywords = _normalize_keywords(_tokenize(text))

    if len(base_keywords) < 2:
        return False

    sender = str(target_sender).strip().lower() or "unknown"
    user = str(inbox_user).strip().lower() if inbox_user else ""
    doc_type = str(target_type).strip().lower()

    learning = _load_learning(cfg)
    examples = learning.get("examples", [])
    if not isinstance(examples, list):
        examples = []

    keywords_key = "|".join(sorted(base_keywords))
    record_key = f"{user}:{doc_type}:{sender}:{keywords_key}"
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    updated = False
    for row in examples:
        if not isinstance(row, dict):
            continue
        if str(row.get("key", "")) != record_key:
            continue
        row["count"] = int(row.get("count", 1)) + 1
        row["last_seen"] = now
        updated = True
        break

    if not updated:
        examples.append(
            {
                "key": record_key,
                "user": user,
                "doc_type": doc_type,
                "sender": sender,
                "keywords": base_keywords,
                "count": 1,
                "last_seen": now,
            }
        )

    # Keep only recent/high-signal examples.
    cleaned: list[dict] = []
    for row in examples:
        if not isinstance(row, dict):
            continue
        kws = row.get("keywords", [])
        if not isinstance(kws, list) or len(kws) < 2:
            continue
        cleaned.append(row)

    cleaned.sort(key=lambda r: (int(r.get("count", 1)), str(r.get("last_seen", ""))), reverse=True)
    learning["examples"] = cleaned[:2000]
    _save_learning(cfg, learning)
    return True


def predict_manual_learning(
    cfg: AppConfig,
    *,
    text: str,
    inbox_user: str | None,
) -> tuple[str, float, str] | None:
    learning = _load_learning(cfg)
    examples = learning.get("examples", [])
    if not isinstance(examples, list) or not examples:
        return None

    tokens = set(_normalize_keywords(_tokenize(text, limit=36), limit=36))
    if len(tokens) < 2:
        return None

    user = str(inbox_user).strip().lower() if inbox_user else ""
    best_type = ""
    best_score = 0.0
    best_overlap = 0.0

    for row in examples:
        if not isinstance(row, dict):
            continue
        row_user = str(row.get("user", "")).strip().lower()
        if row_user and user and row_user != user:
            continue

        kws = row.get("keywords", [])
        if not isinstance(kws, list):
            continue
        keyword_set = set(_normalize_keywords([str(k) for k in kws], limit=36))
        if len(keyword_set) < 2:
            continue

        overlap = len(tokens.intersection(keyword_set)) / float(len(keyword_set))
        if overlap < 0.45:
            continue

        count = max(1, int(row.get("count", 1)))
        strength = min(0.25, math.log1p(count) / 8.0)
        score = overlap * 0.75 + strength
        if score > best_score:
            best_score = score
            best_overlap = overlap
            best_type = str(row.get("doc_type", "")).strip().lower()

    if not best_type:
        return None

    confidence = min(0.97, max(0.65, 0.55 + best_score))
    reason = f"manual_learning:overlap={best_overlap:.2f}"
    return best_type, confidence, reason
