from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .classifier import ClassificationResult
from .config import AppConfig


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("_") or "unknown"


def _year_from_date(raw_date: str, fallback_year: int) -> str:
    if not raw_date:
        return str(fallback_year)
    for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"]:
        try:
            return str(datetime.strptime(raw_date, fmt).year)
        except ValueError:
            continue
    return str(fallback_year)


def build_destination(pdf_path: Path, result: ClassificationResult, cfg: AppConfig) -> Path:
    file_year = datetime.fromtimestamp(pdf_path.stat().st_mtime).year
    year = _year_from_date(result.date, file_year)
    sender = _safe_name(result.vendor_or_sender)
    base_name = _safe_name(pdf_path.stem)

    if result.doc_type == cfg.rules.unknown_doc_type:
        return cfg.paths.review / f"{base_name}.pdf"

    if result.confidence < cfg.llm.min_confidence_autofile:
        return cfg.paths.review / f"{base_name}.pdf"

    target_dir = cfg.paths.archive / result.doc_type / year / sender
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"{base_name}.pdf"


def unique_path(path: Path) -> Path:
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
