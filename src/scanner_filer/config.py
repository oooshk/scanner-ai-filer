from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PathsConfig:
    inbox: Path
    processing: Path
    review: Path
    archive: Path
    rejected: Path
    state: Path


@dataclass
class OCRConfig:
    enabled: bool
    command: str
    language: str
    skip_text: bool


@dataclass
class LLMConfig:
    enabled: bool
    command_template: str
    timeout_seconds: int
    max_input_chars: int
    min_confidence_autofile: float


@dataclass
class SplitterConfig:
    enabled: bool
    min_pages_to_split: int
    max_first_page_chars: int
    boundary_keywords: list[str]


@dataclass
class RulesConfig:
    allowed_doc_types: list[str]
    unknown_doc_type: str


@dataclass
class AppConfig:
    paths: PathsConfig
    ocr: OCRConfig
    llm: LLMConfig
    splitter: SplitterConfig
    rules: RulesConfig
    poll_seconds: int
    inbox_settle_seconds: int
    require_size_stability: bool
    stable_cycles_required: int
    log_level: str


def _path(v: str) -> Path:
    return Path(v).expanduser().resolve()


def load_config(config_path: Path) -> AppConfig:
    with config_path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    paths = PathsConfig(**{k: _path(v) for k, v in raw["paths"].items()})
    ocr = OCRConfig(**raw["ocr"])
    llm = LLMConfig(**raw["llm"])
    splitter_raw: dict[str, Any] = raw.get("splitter", {})
    splitter = SplitterConfig(
        enabled=bool(splitter_raw.get("enabled", True)),
        min_pages_to_split=int(splitter_raw.get("min_pages_to_split", 3)),
        max_first_page_chars=int(splitter_raw.get("max_first_page_chars", 700)),
        boundary_keywords=list(
            splitter_raw.get(
                "boundary_keywords",
                [
                    "statement",
                    "invoice",
                    "receipt",
                    "policy",
                    "council tax",
                    "pension",
                    "account number",
                    "patient",
                ],
            )
        ),
    )
    rules = RulesConfig(**raw["rules"])

    return AppConfig(
        paths=paths,
        ocr=ocr,
        llm=llm,
        splitter=splitter,
        rules=rules,
        poll_seconds=int(raw.get("poll_seconds", 5)),
        inbox_settle_seconds=int(raw.get("inbox_settle_seconds", 20)),
        require_size_stability=bool(raw.get("require_size_stability", True)),
        stable_cycles_required=max(1, int(raw.get("stable_cycles_required", 2))),
        log_level=str(raw.get("log_level", "INFO")),
    )


def ensure_directories(cfg: AppConfig) -> None:
    for p in [
        cfg.paths.inbox,
        cfg.paths.processing,
        cfg.paths.review,
        cfg.paths.archive,
        cfg.paths.rejected,
        cfg.paths.state,
    ]:
        p.mkdir(parents=True, exist_ok=True)
