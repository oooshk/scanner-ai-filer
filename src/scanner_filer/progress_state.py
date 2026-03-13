from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig


def _progress_path(cfg: AppConfig) -> Path:
    return cfg.paths.state / "progress.json"


def _load(cfg: AppConfig) -> dict[str, Any]:
    path = _progress_path(cfg)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def _save(cfg: AppConfig, payload: dict[str, Any]) -> None:
    path = _progress_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True)


def set_active(cfg: AppConfig, source: str, working: str, stage: str, percent: float, **extra: Any) -> None:
    payload = _load(cfg)
    payload["active"] = {
        "source": source,
        "working": working,
        "stage": stage,
        "percent": max(0.0, min(100.0, float(percent))),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **extra,
    }
    _save(cfg, payload)


def clear_active(cfg: AppConfig, status: str, message: str = "") -> None:
    payload = _load(cfg)
    payload["last"] = {
        "status": status,
        "message": message,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    payload.pop("active", None)
    _save(cfg, payload)


def read_progress(cfg: AppConfig) -> dict[str, Any]:
    return _load(cfg)
