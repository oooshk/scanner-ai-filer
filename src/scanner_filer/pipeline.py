from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
import re

import yaml

from .classifier import classify_text
from .config import AppConfig
from .manual_learning import predict_manual_learning
from .ocr import extract_text, run_ocr
from .progress_state import clear_active, set_active
from .rules import build_destination, detect_inbox_user, unique_path
from .splitter import split_pdf_if_needed

logger = logging.getLogger(__name__)


def _load_raw_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _save_raw_config(config_path: Path, payload: dict) -> None:
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False)


def _normalize_doc_type_name(value: str) -> str:
    token = re.sub(r"\s+", " ", str(value).strip().lower())
    if not token:
        return ""
    token = token.replace("-", "_").replace(" ", "_")
    token = re.sub(r"[^a-z0-9_]+", "", token)
    token = re.sub(r"_+", "_", token).strip("_")
    if not re.fullmatch(r"[a-z0-9_]{2,40}", token):
        return ""
    return token


def _maybe_apply_category_suggestion(
    cfg: AppConfig,
    classification,
    config_path: Path | None,
) -> tuple[bool, str]:
    if not getattr(cfg.llm, "category_suggestion_enabled", True):
        return False, ""

    suggested = _normalize_doc_type_name(getattr(classification, "suggested_doc_type", ""))
    suggestion_conf = float(getattr(classification, "suggested_doc_type_confidence", 0.0) or 0.0)
    if not suggested or suggested in cfg.rules.allowed_doc_types:
        return False, ""

    # Require high confidence before using or creating new categories.
    min_conf = float(getattr(cfg.llm, "auto_create_min_confidence", 0.93) or 0.93)
    if suggestion_conf < min_conf:
        return False, suggested

    if not getattr(cfg.llm, "auto_create_suggested_categories", False):
        return False, suggested
    if config_path is None:
        return False, suggested

    raw = _load_raw_config(config_path)
    rules = raw.setdefault("rules", {})
    allowed = [str(x).strip().lower() for x in list(rules.get("allowed_doc_types", [])) if str(x).strip()]
    unknown = str(rules.get("unknown_doc_type", "unknown")).strip().lower() or "unknown"
    if suggested in allowed:
        return False, suggested

    if unknown in allowed:
        insert_at = allowed.index(unknown)
        allowed.insert(insert_at, suggested)
    else:
        allowed.append(suggested)
    rules["allowed_doc_types"] = allowed
    _save_raw_config(config_path, raw)

    cfg.rules.allowed_doc_types = allowed
    classification.doc_type = suggested
    classification.confidence = max(classification.confidence, suggestion_conf)
    classification.reason = f"auto_created_category:{suggested}"
    if "auto_category" not in classification.tags:
        classification.tags.append("auto_category")
    return True, suggested


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


def process_one_file(src_pdf: Path, cfg: AppConfig, config_path: Path | None = None) -> Path:
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
            classification = classify_text(
                text,
                cfg.llm,
                cfg.rules,
                public_knowledge_enabled=cfg.public_knowledge_enabled,
                public_online_lookup_enabled=cfg.public_online_lookup_enabled,
                public_acronym_overrides=cfg.public_acronym_overrides,
            )
            category_created, category_suggested = _maybe_apply_category_suggestion(cfg, classification, config_path)
            if category_suggested and not category_created:
                tag = f"category_suggested:{category_suggested}"
                if tag not in classification.tags:
                    classification.tags.append(tag)
            learned = predict_manual_learning(cfg, text=text, inbox_user=inbox_user)
            if learned:
                learned_type, learned_conf, learned_reason = learned
                if learned_type in cfg.rules.allowed_doc_types and learned_type != cfg.rules.unknown_doc_type:
                    llm_is_weak_or_unknown = (
                        classification.doc_type == cfg.rules.unknown_doc_type
                        or classification.confidence < max(0.68, cfg.llm.min_confidence_autofile)
                        or "fallback" in classification.tags
                    )
                    if llm_is_weak_or_unknown:
                        classification.doc_type = learned_type
                        classification.confidence = max(classification.confidence, learned_conf)
                        classification.reason = learned_reason
                        if "manual_learning" not in classification.tags:
                            classification.tags.append("manual_learning")

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
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
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
                    "suggested_doc_type": getattr(classification, "suggested_doc_type", ""),
                    "suggested_doc_type_confidence": getattr(classification, "suggested_doc_type_confidence", 0.0),
                    "category_auto_created": category_created,
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
            shutil.move(str(candidate), str(rejected))
            logger.warning("Moved failed file to rejected: %s", rejected)
        clear_active(cfg, status="failed", message=src_pdf.name)
        raise
