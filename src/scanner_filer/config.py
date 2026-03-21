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
    rotate_pages: bool
    deskew: bool
    clean: bool
    extra_args: str


@dataclass
class LLMConfig:
    enabled: bool
    command_template: str
    fallback_command_template: str
    timeout_seconds: int
    max_input_chars: int
    min_confidence_autofile: float
    json_metrics_path: Path
    json_failure_window_size: int
    json_failure_rate_threshold: float
    json_consecutive_failure_threshold: int
    json_fallback_cooldown_calls: int
    classification_descriptor: str
    classification_guidance: str
    category_suggestion_enabled: bool
    auto_create_suggested_categories: bool
    auto_create_min_confidence: float
    retry_on_failure_enabled: bool
    retry_profile: str
    retry_command_template: str


@dataclass
class SplitterConfig:
    enabled: bool
    min_pages_to_split: int
    max_first_page_chars: int
    boundary_keywords: list[str]
    content_aware_enabled: bool
    boundary_score_threshold: float
    low_similarity_threshold: float


@dataclass
class RulesConfig:
    allowed_doc_types: list[str]
    unknown_doc_type: str
    keyword_overrides: dict[str, str]
    negative_type_keywords: dict[str, list[str]]
    per_type_min_confidence: dict[str, float]


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
    auto_file_highlight_window: str
    keyword_extract_max_chars: int
    keyword_extract_limit: int
    keyword_extract_include_phrases: bool
    infer_year_from_text: bool
    learning_from_move_enabled: bool
    public_knowledge_enabled: bool
    public_online_lookup_enabled: bool
    public_acronym_overrides: dict[str, str]
    log_level: str
    hover_preview_enabled: bool


def _path(v: str) -> Path:
    return Path(v).expanduser().resolve()


def load_config(config_path: Path) -> AppConfig:
    with config_path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    paths = PathsConfig(**{k: _path(v) for k, v in raw["paths"].items()})
    ocr_raw: dict[str, Any] = raw.get("ocr", {})
    ocr = OCRConfig(
        enabled=bool(ocr_raw.get("enabled", True)),
        command=str(ocr_raw.get("command", "ocrmypdf")),
        language=str(ocr_raw.get("language", "eng")),
        skip_text=bool(ocr_raw.get("skip_text", True)),
        rotate_pages=bool(ocr_raw.get("rotate_pages", True)),
        deskew=bool(ocr_raw.get("deskew", True)),
        clean=bool(ocr_raw.get("clean", False)),
        extra_args=str(ocr_raw.get("extra_args", "")).strip(),
    )
    llm_raw: dict[str, Any] = raw.get("llm", {})
    llm = LLMConfig(
        enabled=bool(llm_raw.get("enabled", True)),
        command_template=str(llm_raw.get("command_template", "")),
        fallback_command_template=str(llm_raw.get("fallback_command_template", "")).strip(),
        timeout_seconds=int(llm_raw.get("timeout_seconds", 240)),
        max_input_chars=int(llm_raw.get("max_input_chars", 2800)),
        min_confidence_autofile=float(llm_raw.get("min_confidence_autofile", 0.60)),
        json_metrics_path=_path(str(llm_raw.get("json_metrics_path", str(paths.state / "llm_json_metrics.json")))),
        json_failure_window_size=max(10, int(llm_raw.get("json_failure_window_size", 40))),
        json_failure_rate_threshold=max(0.05, min(0.95, float(llm_raw.get("json_failure_rate_threshold", 0.35)))),
        json_consecutive_failure_threshold=max(1, int(llm_raw.get("json_consecutive_failure_threshold", 3))),
        json_fallback_cooldown_calls=max(1, int(llm_raw.get("json_fallback_cooldown_calls", 10))),
        classification_descriptor=str(
            llm_raw.get(
                "classification_descriptor",
                "You are an AI document filing assistant. Classify each document by meaning and context, not by isolated words.",
            )
        ).strip(),
        classification_guidance=str(llm_raw.get("classification_guidance", "")).strip(),
        category_suggestion_enabled=bool(llm_raw.get("category_suggestion_enabled", True)),
        auto_create_suggested_categories=bool(llm_raw.get("auto_create_suggested_categories", False)),
        auto_create_min_confidence=max(0.0, min(1.0, float(llm_raw.get("auto_create_min_confidence", 0.93)))),
        retry_on_failure_enabled=bool(llm_raw.get("retry_on_failure_enabled", False)),
        retry_profile=str(llm_raw.get("retry_profile", "deep")).strip().lower() or "deep",
        retry_command_template=str(llm_raw.get("retry_command_template", "")).strip(),
    )
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
        content_aware_enabled=bool(splitter_raw.get("content_aware_enabled", True)),
        boundary_score_threshold=max(0.4, min(2.0, float(splitter_raw.get("boundary_score_threshold", 0.95)))),
        low_similarity_threshold=max(0.05, min(0.8, float(splitter_raw.get("low_similarity_threshold", 0.22)))),
    )
    rules_raw: dict[str, Any] = raw.get("rules", {})
    keyword_overrides_raw = rules_raw.get("keyword_overrides", {})
    negative_raw = rules_raw.get("negative_type_keywords", {})
    per_type_conf_raw = rules_raw.get("per_type_min_confidence", {})

    keyword_overrides: dict[str, str] = {}
    if isinstance(keyword_overrides_raw, dict):
        for k, v in keyword_overrides_raw.items():
            key = str(k).strip().lower()
            val = str(v).strip().lower()
            if key and val:
                keyword_overrides[key] = val

    negative_type_keywords: dict[str, list[str]] = {}
    if isinstance(negative_raw, dict):
        for dtype, values in negative_raw.items():
            dt = str(dtype).strip().lower()
            if not dt or not isinstance(values, list):
                continue
            cleaned = [str(x).strip().lower() for x in values if str(x).strip()]
            if cleaned:
                negative_type_keywords[dt] = cleaned

    per_type_min_confidence: dict[str, float] = {}
    if isinstance(per_type_conf_raw, dict):
        for dtype, val in per_type_conf_raw.items():
            dt = str(dtype).strip().lower()
            if not dt:
                continue
            try:
                score = float(val)
            except (TypeError, ValueError):
                continue
            per_type_min_confidence[dt] = max(0.0, min(1.0, score))

    rules = RulesConfig(
        allowed_doc_types=list(rules_raw.get("allowed_doc_types", [])),
        unknown_doc_type=str(rules_raw.get("unknown_doc_type", "unknown")),
        keyword_overrides=keyword_overrides,
        negative_type_keywords=negative_type_keywords,
        per_type_min_confidence=per_type_min_confidence,
    )

    recognition_raw: dict[str, Any] = raw.get("recognition", {})
    learning_raw: dict[str, Any] = raw.get("learning", {})
    public_raw: dict[str, Any] = raw.get("public_knowledge", {})
    ui_raw: dict[str, Any] = raw.get("ui", {})

    public_acronym_overrides: dict[str, str] = {}
    overrides_raw = public_raw.get("acronym_overrides", {})
    if isinstance(overrides_raw, dict):
        for k, v in overrides_raw.items():
            key = str(k).strip().lower()
            val = str(v).strip().lower()
            if key and val:
                public_acronym_overrides[key] = val

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
        auto_file_highlight_window=(
            str(raw.get("auto_file_highlight_window", "1h")).strip().lower()
            if str(raw.get("auto_file_highlight_window", "1h")).strip().lower() in {"10m", "1h", "1d", "1w", "forever"}
            else "1h"
        ),
        keyword_extract_max_chars=max(1200, int(recognition_raw.get("keyword_extract_max_chars", 5000))),
        keyword_extract_limit=max(8, int(recognition_raw.get("keyword_extract_limit", 24))),
        keyword_extract_include_phrases=bool(recognition_raw.get("keyword_extract_include_phrases", True)),
        infer_year_from_text=bool(recognition_raw.get("infer_year_from_text", True)),
        learning_from_move_enabled=bool(learning_raw.get("enabled", True)),
        public_knowledge_enabled=bool(public_raw.get("enabled", False)),
        public_online_lookup_enabled=bool(public_raw.get("online_lookup_enabled", False)),
        public_acronym_overrides=public_acronym_overrides,
        log_level=str(raw.get("log_level", "INFO")),
        hover_preview_enabled=bool(ui_raw.get("hover_preview_enabled", True)),
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
