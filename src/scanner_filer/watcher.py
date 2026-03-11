from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig
from .pipeline import process_one_file
from .progress_state import read_progress
from .rules import unique_path

logger = logging.getLogger(__name__)


def _is_pdf(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".pdf"


_SIZE_STATE: dict[str, tuple[int, int]] = {}


def _recover_orphan_processing(cfg: AppConfig) -> int:
    moved = 0
    progress = read_progress(cfg).get("active", {})
    active_name = ""
    if isinstance(progress, dict):
        active_name = Path(str(progress.get("working", ""))).name

    now = datetime.now(timezone.utc).timestamp()
    for p in sorted(cfg.paths.processing.glob("*.pdf")):
        if p.name == active_name:
            continue
        age = now - p.stat().st_mtime
        if age < max(30, cfg.inbox_settle_seconds):
            continue
        requeued = unique_path(cfg.paths.inbox / p.name)
        p.replace(requeued)
        moved += 1
        logger.warning("Recovered orphan processing file back to inbox: %s", requeued)
    return moved


def _is_size_stable(path: Path, cfg: AppConfig) -> bool:
    if not cfg.require_size_stability:
        return True

    key = str(path.resolve())
    size = path.stat().st_size
    prev = _SIZE_STATE.get(key)

    if prev is None:
        _SIZE_STATE[key] = (size, 1)
        return False

    prev_size, stable_cycles = prev
    if size == prev_size:
        stable_cycles += 1
    else:
        stable_cycles = 1

    _SIZE_STATE[key] = (size, stable_cycles)
    return stable_cycles >= cfg.stable_cycles_required


def process_pending(cfg: AppConfig) -> int:
    count = 0
    recovered = _recover_orphan_processing(cfg)
    if recovered:
        logger.info("Recovered %s orphan file(s) from processing", recovered)
    now = datetime.now(timezone.utc).timestamp()
    seen_keys: set[str] = set()
    for entry in sorted(cfg.paths.inbox.iterdir()):
        if not _is_pdf(entry):
            continue
        seen_keys.add(str(entry.resolve()))
        file_age = now - entry.stat().st_mtime
        if file_age < cfg.inbox_settle_seconds:
            logger.debug(
                "Skipping %s (age %.1fs < settle %ss)",
                entry.name,
                file_age,
                cfg.inbox_settle_seconds,
            )
            continue
        if not _is_size_stable(entry, cfg):
            logger.debug("Skipping %s (size not yet stable)", entry.name)
            continue
        try:
            process_one_file(entry, cfg)
            count += 1
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed to process %s: %s", entry, exc)
            rejected = unique_path(cfg.paths.rejected / entry.name)
            if entry.exists():
                entry.replace(rejected)
            else:
                processing_copy = cfg.paths.processing / entry.name
                if processing_copy.exists():
                    processing_copy.replace(rejected)

    # Drop stale size-state entries for files no longer in inbox.
    for key in list(_SIZE_STATE.keys()):
        if key not in seen_keys:
            _SIZE_STATE.pop(key, None)
    return count


def run_polling_watcher(cfg: AppConfig) -> None:
    logger.info("Watching %s", cfg.paths.inbox)
    while True:
        processed = process_pending(cfg)
        if processed:
            logger.info("Processed %s file(s) in this cycle", processed)
        time.sleep(cfg.poll_seconds)
