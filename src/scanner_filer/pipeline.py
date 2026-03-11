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
from .rules import build_destination, unique_path
from .splitter import split_pdf_if_needed

logger = logging.getLogger(__name__)


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


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

            dest = unique_path(build_destination(part_pdf, classification, cfg))
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
        candidates = sorted(cfg.paths.processing.glob(f"{working_pdf.stem}*.pdf"))
        for candidate in candidates:
            rejected = unique_path(cfg.paths.rejected / candidate.name)
            candidate.replace(rejected)
            logger.warning("Moved failed file to rejected: %s", rejected)
        clear_active(cfg, status="failed", message=src_pdf.name)
        raise
