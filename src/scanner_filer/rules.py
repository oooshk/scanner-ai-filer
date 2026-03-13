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


def detect_inbox_user(pdf_path: Path, cfg: AppConfig) -> str | None:
    """
    Detect which user/inbox a PDF came from.
    Returns the username if the PDF is in a user-specific inbox, None otherwise.
    Only recognizes directory names that match username pattern:
    - 3-64 characters
    - Start with a letter
    - Can contain letters, numbers, dots, underscores, and hyphens
    """
    try:
        # Check if pdf is under inbox/username/ structure
        rel = pdf_path.relative_to(cfg.paths.inbox)
        parts = rel.parts
        # Require at least username/file.pdf so root-level inbox files are not treated as usernames.
        if len(parts) >= 2:
            # The first part might be the username
            candidate = parts[0]
            if candidate.lower().endswith(".pdf"):
                return None
            # Only treat as username if it starts with a letter and matches pattern
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,63}", candidate):
                return candidate
    except ValueError:
        # PDF is not under cfg.paths.inbox
        pass
    return None


def _get_user_archive_base(cfg: AppConfig, username: str | None) -> Path:
    """Get the archive base path for a user, or the default if no user specified."""
    if username:
        return cfg.paths.archive.parent / "archive" / username
    return cfg.paths.archive


def _get_user_review_base(cfg: AppConfig, username: str | None) -> Path:
    """Get the review base path for a user, or the default if no user specified."""
    if username:
        return cfg.paths.archive.parent / "review" / username
    return cfg.paths.review


def _get_user_rejected_base(cfg: AppConfig, username: str | None) -> Path:
    """Get the rejected base path for a user, or the default if no user specified."""
    if username:
        return cfg.paths.archive.parent / "rejected" / username
    return cfg.paths.rejected

def build_destination(
    pdf_path: Path,
    result: ClassificationResult,
    cfg: AppConfig,
    inbox_user: str | None = None,
    archive_root: Path | None = None,
    review_root: Path | None = None,
) -> Path:
    file_year = datetime.fromtimestamp(pdf_path.stat().st_mtime).year
    year = _year_from_date(result.date, file_year)
    sender = _safe_name(result.vendor_or_sender)
    base_name = _safe_name(pdf_path.stem)

    if result.doc_type == cfg.rules.unknown_doc_type:
        review_base = review_root or _get_user_review_base(cfg, inbox_user)
        review_base.mkdir(parents=True, exist_ok=True)
        return review_base / f"{base_name}.pdf"

    type_threshold = float(cfg.rules.per_type_min_confidence.get(result.doc_type, cfg.llm.min_confidence_autofile))
    if result.confidence < type_threshold:
        review_base = review_root or _get_user_review_base(cfg, inbox_user)
        review_base.mkdir(parents=True, exist_ok=True)
        return review_base / f"{base_name}.pdf"

    archive_base = archive_root or _get_user_archive_base(cfg, inbox_user)
    target_dir = archive_base / result.doc_type / year / sender
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
