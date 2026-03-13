from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from .classifier import classify_text
from .config import AppConfig
from .ocr import extract_text, run_ocr
from .progress_state import clear_active, set_active
from .rules import build_destination, detect_inbox_user, unique_path
from .splitter import split_pdf_if_needed

logger = logging.getLogger(__name__)


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _load_user_inboxes(cfg: AppConfig) -> dict[str, dict]:
    path = cfg.paths.state / "user_inboxes.json"
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        data = payload.get("inboxes", {})
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        pass
    return {}


def _resolve_user_roots(cfg: AppConfig, src_pdf: Path) -> tuple[str | None, Path | None, Path | None, Path | None]:
    inbox_user = detect_inbox_user(src_pdf, cfg)
    user_archive: Path | None = None
    user_review: Path | None = None
    user_rejected: Path | None = None
    if inbox_user:
        inboxes = _load_user_inboxes(cfg)
        entry = inboxes.get(inbox_user, {})
        if not isinstance(entry, dict) or not entry:
            # If this subfolder is not configured as a user inbox, use generic roots.
            return None, None, None, None
        archive_raw = str(entry.get("archive_path", "")).strip()
        review_raw = str(entry.get("review_path", "")).strip()
        rejected_raw = str(entry.get("rejected_path", "")).strip()
        if archive_raw:
            user_archive = Path(archive_raw).expanduser().resolve()
        if review_raw:
            user_review = Path(review_raw).expanduser().resolve()
        if rejected_raw:
            user_rejected = Path(rejected_raw).expanduser().resolve()
        if user_archive is None and user_review is None and user_rejected is None:
            return None, None, None, None
    return inbox_user, user_archive, user_review, user_rejected


def process_one_file(src_pdf: Path, cfg: AppConfig) -> Path:
    logger.info("Processing %s", src_pdf.name)

    working_pdf = cfg.paths.processing / src_pdf.name
    src_pdf.replace(working_pdf)
    set_active(cfg, source=str(src_pdf), working=str(working_pdf), stage="moved_to_processing", percent=5)
    try:
        set_active(cfg, source=str(src_pdf), working=str(working_pdf), stage="running_ocr", percent=18)
        processed_pdf = run_ocr(working_pdf, cfg.ocr)
        set_active(cfg, source=str(src_pdf), working=str(processed_pdf), stage="splitting", percent=34)
        parts = split_pdf_if_needed(processed_pdf, cfg.splitter)
        set_active(
            cfg,
            source=str(src_pdf),
            working=str(processed_pdf),
            stage="split_ready",
            percent=42,
            parts_total=len(parts),
        )
        last_dest: Path | None = None

        inbox_user, user_archive, user_review, user_rejected = _resolve_user_roots(cfg, src_pdf)

        for idx, part_pdf in enumerate(parts, start=1):
            base_pct = 42 + ((idx - 1) / max(1, len(parts))) * 50
            set_active(
                cfg,
                source=str(src_pdf),
                working=str(part_pdf),
                stage="classifying",
                percent=base_pct,
                part_index=idx,
                parts_total=len(parts),
            )
            text = extract_text(part_pdf, max_chars=cfg.llm.max_input_chars)
            classification = classify_text(text, cfg.llm, cfg.rules)

            dest = unique_path(
                build_destination(
                    part_pdf,
                    classification,
                    cfg,
                    inbox_user=inbox_user,
                    archive_root=user_archive,
                    review_root=user_review,
                )
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(part_pdf), str(dest))
            last_dest = dest
            filed_pct = 42 + (idx / max(1, len(parts))) * 50
            set_active(
                cfg,
                source=str(src_pdf),
                working=str(dest),
                stage="filing",
                percent=filed_pct,
                part_index=idx,
                parts_total=len(parts),
            )

            _append_jsonl(
                cfg.paths.state / "events.jsonl",
                {
                    "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "source": str(src_pdf),
                    "source_part_index": idx,
                    "source_parts_total": len(parts),
                    "destination": str(dest),
                    "doc_type": classification.doc_type,
                    "vendor_or_sender": classification.vendor_or_sender,
                    "date": classification.date,
                    "tags": classification.tags,
                    "confidence": classification.confidence,
                    "reason": classification.reason,
                },
            )

            logger.info("Filed %s (part %s/%s) -> %s", src_pdf.name, idx, len(parts), dest)

        if last_dest is None:
            raise RuntimeError(f"No destination generated for {src_pdf}")
        clear_active(cfg, status="completed", message=str(last_dest))
        return last_dest
    except Exception:
        # If processing fails after moving from inbox, do not leave files stranded in processing.
        _, _, _, user_rejected = _resolve_user_roots(cfg, src_pdf)
        rejected_base = user_rejected or cfg.paths.rejected
        rejected_base.mkdir(parents=True, exist_ok=True)
        candidates = sorted(cfg.paths.processing.glob(f"{working_pdf.stem}*.pdf"))
        for candidate in candidates:
            rejected = unique_path(rejected_base / candidate.name)
            candidate.replace(rejected)
            logger.warning("Moved failed file to rejected: %s", rejected)
        clear_active(cfg, status="failed", message=src_pdf.name)
        raise
