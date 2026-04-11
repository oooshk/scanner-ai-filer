from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import LLMConfig, RulesConfig

logger = logging.getLogger(__name__)

_PUBLIC_ACRONYM_CACHE: dict[str, str] = {}
_ALLOWED_SINGLE_WORD_OVERRIDES = {"dvsa", "hmrc", "nhs", "v5c", "vin", "mot"}
_LOW_SIGNAL_OVERRIDE_WORDS = {
    "account",
    "act",
    "age",
    "april",
    "after",
    "affect",
    "address",
    "car",
    "details",
    "email",
    "income",
    "interest",
    "may",
    "parts",
    "royston",
    "take",
    "terms",
    "total",
}

_DEFAULT_CLASSIFICATION_DESCRIPTOR = (
    "You are an AI document filing assistant. Classify each document by meaning and context, "
    "not by isolated words."
)

_ULTRA_SCENARIO_GUIDANCE = (
    "Ultra scenario guidance for mixed filing archives:\n"
    "- Optical, prescription, optometrist, clinic, NHS, patient, diagnosis, treatment, occupational health => medical (never payslip).\n"
    "- Employment letters, disciplinary/grievance, long-service awards, HR correspondence, crew memos without payroll fields => poferries/personal/legal context, not payslip.\n"
    "- Use payslip only when explicit payroll markers exist (gross pay, net pay, NI number, tax code, pay period/pay date, employer/employee payroll layout).\n"
    "- Pension benefit statements, retirement fund updates, SIPP/annuity content => pension (not payslip).\n"
    "- If uncertain between payslip and medical/letter categories, prefer unknown for review over confident wrong payslip."
)


def _is_ultra_profile_command(command_template: str) -> bool:
    cmd = str(command_template or "").strip().lower()
    if not cmd:
        return False
    if cmd.startswith("ollama run") and any(tok in cmd for tok in ["14b", "16b", "32b"]):
        return True
    return any(tok in cmd for tok in ["14b-instruct", "16b-instruct", "32b-instruct", "14b", "16b", "32b"])


def _default_json_metrics() -> dict:
    return {
        "total_calls": 0,
        "json_successes": 0,
        "json_failures": 0,
        "consecutive_json_failures": 0,
        "recent_json_results": [],
        "fallback_active_until_call": 0,
        "fallback_triggers": 0,
        "last_result": "",
    }


def _load_json_metrics(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return _default_json_metrics()


def _save_json_metrics(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=True)
    except Exception:
        logger.debug("Unable to persist JSON metrics", exc_info=True)


def _select_command_template(llm_cfg: LLMConfig) -> str:
    metrics_path = llm_cfg.json_metrics_path
    metrics = _load_json_metrics(metrics_path)
    call_index = int(metrics.get("total_calls", 0)) + 1
    metrics["total_calls"] = call_index

    fallback_until = int(metrics.get("fallback_active_until_call", 0))
    fallback_cmd = str(llm_cfg.fallback_command_template or "").strip()
    use_fallback = bool(fallback_cmd) and call_index <= fallback_until
    metrics["last_mode"] = "fallback" if use_fallback else "primary"
    _save_json_metrics(metrics_path, metrics)

    return fallback_cmd if use_fallback else llm_cfg.command_template


def _record_json_result(llm_cfg: LLMConfig, success: bool, result_label: str) -> None:
    metrics_path = llm_cfg.json_metrics_path
    metrics = _load_json_metrics(metrics_path)

    window_size = max(10, int(llm_cfg.json_failure_window_size))
    recent = metrics.get("recent_json_results", [])
    if not isinstance(recent, list):
        recent = []
    recent.append(1 if success else 0)
    if len(recent) > window_size:
        recent = recent[-window_size:]
    metrics["recent_json_results"] = recent

    if success:
        metrics["json_successes"] = int(metrics.get("json_successes", 0)) + 1
        metrics["consecutive_json_failures"] = 0
    else:
        metrics["json_failures"] = int(metrics.get("json_failures", 0)) + 1
        metrics["consecutive_json_failures"] = int(metrics.get("consecutive_json_failures", 0)) + 1

    metrics["last_result"] = str(result_label)

    failure_rate = 1.0 - (sum(recent) / len(recent) if recent else 1.0)
    consecutive_failures = int(metrics.get("consecutive_json_failures", 0))
    rate_threshold = float(llm_cfg.json_failure_rate_threshold)
    consecutive_threshold = int(llm_cfg.json_consecutive_failure_threshold)
    min_samples_for_rate = min(10, window_size)

    fallback_cmd = str(llm_cfg.fallback_command_template or "").strip()
    if (not success) and fallback_cmd:
        should_trigger = (
            consecutive_failures >= consecutive_threshold
            or (len(recent) >= min_samples_for_rate and failure_rate >= rate_threshold)
        )
        if should_trigger:
            call_index = int(metrics.get("total_calls", 0))
            cooldown = max(1, int(llm_cfg.json_fallback_cooldown_calls))
            existing_until = int(metrics.get("fallback_active_until_call", 0))
            new_until = max(existing_until, call_index + cooldown)
            metrics["fallback_active_until_call"] = new_until
            metrics["fallback_triggers"] = int(metrics.get("fallback_triggers", 0)) + 1
            logger.warning(
                "JSON reliability guard activated fallback command (call=%s until=%s, failure_rate=%.2f, consecutive=%s)",
                call_index,
                new_until,
                failure_rate,
                consecutive_failures,
            )

    _save_json_metrics(metrics_path, metrics)


def _validate_payload_shape(data: object, rules_cfg: RulesConfig) -> bool:
    if not isinstance(data, dict):
        return False

    required = {"doc_type", "vendor_or_sender", "date", "tags", "confidence", "reason"}
    if not required.issubset(set(data.keys())):
        return False

    doc_type = _normalize_doc_type_name(str(data.get("doc_type", "")))
    if doc_type not in rules_cfg.allowed_doc_types:
        return False

    vendor = data.get("vendor_or_sender")
    date = data.get("date")
    reason = data.get("reason")
    if not isinstance(vendor, str) or not vendor.strip():
        return False
    if not isinstance(date, str):
        return False
    if not isinstance(reason, str) or not reason.strip():
        return False

    tags = data.get("tags")
    if not isinstance(tags, list):
        return False
    if any(not isinstance(tag, str) for tag in tags):
        return False

    try:
        conf = float(data.get("confidence"))
    except (TypeError, ValueError):
        return False
    if conf < 0.0 or conf > 1.0:
        return False

    suggested = data.get("suggested_doc_type", "")
    if suggested is not None and not isinstance(suggested, str):
        return False
    suggested_conf = data.get("suggested_doc_type_confidence", 0.0)
    try:
        s_conf = float(suggested_conf)
    except (TypeError, ValueError):
        return False
    if s_conf < 0.0 or s_conf > 1.0:
        return False

    return True


def _coerce_confidence(value: object) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    # Some model replies use percent scale (e.g. 95 instead of 0.95).
    if conf > 1.0 and conf <= 100.0:
        conf = conf / 100.0
    return max(0.0, min(1.0, conf))


def _coerce_payload_shape(payload: dict, rules_cfg: RulesConfig) -> dict:
    out = dict(payload)

    raw_doc_type = _normalize_doc_type_name(str(out.get("doc_type", "")))
    suggested = _normalize_doc_type_name(str(out.get("suggested_doc_type", "")))

    if raw_doc_type not in rules_cfg.allowed_doc_types:
        # Keep unknown as authoritative doc_type but preserve a strong model hint.
        if not suggested and raw_doc_type:
            suggested = raw_doc_type
        raw_doc_type = rules_cfg.unknown_doc_type

    out["doc_type"] = raw_doc_type if raw_doc_type in rules_cfg.allowed_doc_types else rules_cfg.unknown_doc_type

    vendor = str(out.get("vendor_or_sender", "")).strip()
    out["vendor_or_sender"] = vendor or "unknown"

    out["date"] = str(out.get("date", "")).strip()

    tags_raw = out.get("tags", [])
    tags: list[str] = []
    if isinstance(tags_raw, list):
        tags = [str(tag).strip() for tag in tags_raw if str(tag).strip()]
    elif isinstance(tags_raw, str):
        tags = [tok.strip() for tok in tags_raw.split(",") if tok.strip()]
    out["tags"] = tags[:8]

    out["confidence"] = _coerce_confidence(out.get("confidence", 0.0))

    reason = str(out.get("reason", "")).strip()
    out["reason"] = reason[:220] if reason else "llm"

    # Keep suggested_doc_type even when it points to an existing allowed category.
    # This supports review-time "likely category" hints without forcing auto-file.
    if suggested == out["doc_type"]:
        suggested = ""
    out["suggested_doc_type"] = suggested
    out["suggested_doc_type_confidence"] = _coerce_confidence(out.get("suggested_doc_type_confidence", 0.0))

    return out


def _decode_valid_payload(json_block: str, rules_cfg: RulesConfig) -> dict | None:
    try:
        payload = json.loads(json_block)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    payload = _coerce_payload_shape(payload, rules_cfg)
    if not _validate_payload_shape(payload, rules_cfg):
        return None
    return payload


def _recover_type_only(raw_text: str, rules_cfg: RulesConfig) -> str:
    token = _normalize_doc_type_name(raw_text)
    if token in rules_cfg.allowed_doc_types:
        return token

    low = str(raw_text or "").lower()
    choices = sorted(set(rules_cfg.allowed_doc_types), key=len, reverse=True)
    for choice in choices:
        if not choice:
            continue
        pattern = rf"(?<![a-z0-9_]){re.escape(choice)}(?![a-z0-9_])"
        if re.search(pattern, low):
            return choice
    return ""


@dataclass
class ClassificationResult:
    doc_type: str
    vendor_or_sender: str
    date: str
    tags: list[str]
    confidence: float
    reason: str
    suggested_doc_type: str = ""
    suggested_doc_type_confidence: float = 0.0


def _is_high_signal_override_phrase(phrase: str) -> bool:
    candidate = re.sub(r"\s+", " ", str(phrase).strip().lower())
    if len(candidate) < 3:
        return False
    if not re.fullmatch(r"[a-z0-9._ -]{3,80}", candidate):
        return False
    if candidate in _LOW_SIGNAL_OVERRIDE_WORDS:
        return False

    words = candidate.split(" ")
    if len(words) >= 2:
        return True

    word = words[0]
    return word in _ALLOWED_SINGLE_WORD_OVERRIDES


def _try_parse_relaxed_json(candidate: str) -> str | None:
    text = str(candidate or "").strip()
    if not text:
        return None

    repairs = [text]
    repairs.append(text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'"))

    # Common LLM formatting issue: trailing commas before closing braces/brackets.
    repairs.append(re.sub(r",\s*([}\]])", r"\1", repairs[-1]))

    # Last-chance conversion for pseudo-JSON with single quotes.
    sq = repairs[-1]
    sq = re.sub(r"(?<!\\)'([^'\\]*(?:\\.[^'\\]*)*)'", r'"\1"', sq)
    repairs.append(sq)

    for item in repairs:
        try:
            obj = json.loads(item)
            return json.dumps(obj, ensure_ascii=True)
        except Exception:
            continue
    return None


def _sanitize_json_block(text: str) -> str | None:
    # First try a simple object capture.
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        candidate = match.group(0)
        parsed = _try_parse_relaxed_json(candidate)
        if parsed:
            return parsed

    # Fallback: scan for balanced JSON objects and return the first parseable one.
    depth = 0
    start = -1
    for idx, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start : idx + 1]
                    parsed = _try_parse_relaxed_json(candidate)
                    if parsed:
                        return parsed
                    else:
                        start = -1
                        continue
    return None


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


def _contains_override_phrase(text: str, phrase: str) -> bool:
    token = re.sub(r"\s+", " ", str(phrase).strip().lower())
    if not token:
        return False
    escaped = re.escape(token).replace(r"\ ", r"\s+")
    pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _build_ruleset_guidance(rules_cfg: RulesConfig) -> str:
    overrides = list(rules_cfg.keyword_overrides.items())[:12]
    negatives = list(rules_cfg.negative_type_keywords.items())[:12]

    lines: list[str] = []
    if overrides:
        lines.append("Keyword overrides (fallback-only hints):")
        for phrase, target in overrides:
            if not phrase or not target:
                continue
            lines.append(f"- {phrase} => {target}")
    if negatives:
        lines.append("Negative type guards:")
        for dtype, words in negatives:
            if not dtype or not words:
                continue
            word_list = ", ".join(str(w) for w in words[:8])
            lines.append(f"- {dtype}: {word_list}")
    return "\n".join(lines)


def _extract_evidence_terms(text: str, limit: int = 18) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9._-]{2,}", text.lower())
    freq: dict[str, int] = {}
    for token in tokens:
        if token.isdigit() or len(token) < 3:
            continue
        if token in {
            "the", "and", "for", "with", "from", "that", "this", "your", "you", "are", "was", "were",
            "have", "has", "will", "can", "not", "all", "any", "our", "out", "per", "www", "http", "https",
            "com", "ltd", "document", "page", "pdf",
        }:
            continue
        freq[token] = freq.get(token, 0) + 1

    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [k for k, _v in ranked[:limit]]


def _result_from_json(data: dict, rules_cfg: RulesConfig) -> ClassificationResult:
    doc_type = str(data.get("doc_type", rules_cfg.unknown_doc_type)).lower().strip()
    if doc_type not in rules_cfg.allowed_doc_types:
        doc_type = rules_cfg.unknown_doc_type

    suggested_doc_type = _normalize_doc_type_name(str(data.get("suggested_doc_type", "")))
    if suggested_doc_type == doc_type:
        suggested_doc_type = ""

    try:
        suggested_doc_type_confidence = float(data.get("suggested_doc_type_confidence", 0.0))
    except (TypeError, ValueError):
        suggested_doc_type_confidence = 0.0
    suggested_doc_type_confidence = max(0.0, min(1.0, suggested_doc_type_confidence))

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    tags = data.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    return ClassificationResult(
        doc_type=doc_type,
        vendor_or_sender=str(data.get("vendor_or_sender", "unknown")).strip() or "unknown",
        date=str(data.get("date", "")).strip(),
        tags=[str(t).strip() for t in tags if str(t).strip()],
        confidence=confidence,
        reason=str(data.get("reason", "llm")).strip() or "llm",
        suggested_doc_type=suggested_doc_type,
        suggested_doc_type_confidence=suggested_doc_type_confidence,
    )


def _keyword_fallback(text: str, rules_cfg: RulesConfig) -> ClassificationResult:
    low = text.lower()
    matched_signal = ""
    if any(k in low for k in ["council tax", "local authority", "council bill"]):
        doc_type = "council_tax"
        matched_signal = "council"
    elif any(k in low for k in ["bank statement", "sort code", "account number", "statement period"]):
        doc_type = "bank_statement"
        matched_signal = "bank_statement"
    elif any(k in low for k in ["pension", "retirement", "annuity", "state pension"]):
        doc_type = "pension"
        matched_signal = "pension"
    elif any(k in low for k in ["utility", "electricity", "gas", "water", "broadband"]):
        doc_type = "utility_bill"
        matched_signal = "utility"
    elif any(k in low for k in ["insurance", "policy number", "premium", "allianz", "travel cover", "accidental damage", "gadget cover"]):
        doc_type = "insurance"
        matched_signal = "insurance"
    elif any(k in low for k in ["invoice", "amount due", "bill to"]):
        doc_type = "invoice"
        matched_signal = "invoice"
    elif any(k in low for k in ["receipt", "total", "paid"]):
        doc_type = "receipt"
        matched_signal = "receipt"
    elif any(k in low for k in ["lab", "clinic", "patient"]):
        doc_type = "medical"
        matched_signal = "medical"
    elif any(k in low for k in ["payslip", "pay period", "national insurance"]):
        doc_type = "payslip"
        matched_signal = "payslip"
    elif any(k in low for k in ["tax", "irs", "hmrc"]):
        doc_type = "tax"
        matched_signal = "tax"
    elif any(k in low for k in ["contract", "agreement", "terms and conditions"]):
        doc_type = "legal"
        matched_signal = "legal"
    else:
        doc_type = rules_cfg.unknown_doc_type

    if doc_type not in rules_cfg.allowed_doc_types:
        doc_type = rules_cfg.unknown_doc_type

    confidence = 0.45
    suggested_doc_type = ""
    suggested_doc_type_confidence = 0.0
    if doc_type != rules_cfg.unknown_doc_type:
        # Keep review-safe confidence, but provide a useful hint for UI/review workflows.
        confidence = 0.52
        suggested_doc_type = doc_type
        suggested_doc_type_confidence = 0.80

    return ClassificationResult(
        doc_type=doc_type,
        vendor_or_sender="unknown",
        date="",
        tags=["fallback"] + ([f"fallback_signal:{matched_signal}"] if matched_signal else []),
        confidence=confidence,
        reason="keyword_fallback",
        suggested_doc_type=suggested_doc_type,
        suggested_doc_type_confidence=suggested_doc_type_confidence,
    )


def _apply_rule_overrides(
    text: str,
    result: ClassificationResult,
    rules_cfg: RulesConfig,
    *,
    allow_positive_overrides: bool = True,
) -> ClassificationResult:
    low = text.lower()

    if allow_positive_overrides:
        # Positive override: force target category when phrase matches.
        for phrase, forced_type in rules_cfg.keyword_overrides.items():
            if not phrase or not _is_high_signal_override_phrase(phrase):
                continue
            if forced_type not in rules_cfg.allowed_doc_types or forced_type == rules_cfg.unknown_doc_type:
                continue
            if not _contains_override_phrase(low, phrase):
                continue

            result.doc_type = forced_type
            result.confidence = max(result.confidence, 0.95)
            result.reason = f"keyword_override:{phrase}"
            break

    # Negative guard: downgrade risky misclassifications for review.
    blocked_terms = rules_cfg.negative_type_keywords.get(result.doc_type, [])
    if blocked_terms and any(term in low for term in blocked_terms):
        result.doc_type = rules_cfg.unknown_doc_type
        result.confidence = min(result.confidence, 0.35)
        result.reason = "negative_type_guard"

    return result


def _public_summary_to_type(summary: str, rules_cfg: RulesConfig) -> str:
    low = summary.lower()
    if any(k in low for k in ["pension", "retirement", "superannuation"]):
        out = "pension"
    elif any(k in low for k in ["insurance", "policy", "underwriter"]):
        out = "insurance"
    elif any(k in low for k in ["invoice", "billing", "bill to", "accounts payable"]):
        out = "invoice"
    elif any(k in low for k in ["bank", "statement", "sort code"]):
        out = "bank_statement"
    elif any(k in low for k in ["tax", "hmrc", "irs"]):
        out = "tax"
    elif any(k in low for k in ["medical", "hospital", "clinic", "nhs"]):
        out = "medical"
    elif any(k in low for k in ["receipt", "purchase", "merchant"]):
        out = "receipt"
    elif any(k in low for k in ["contract", "legal", "agreement"]):
        out = "legal"
    else:
        out = rules_cfg.unknown_doc_type

    return out if out in rules_cfg.allowed_doc_types else rules_cfg.unknown_doc_type


def _lookup_public_acronym_type(acronym: str, rules_cfg: RulesConfig) -> str:
    key = acronym.lower()
    if key in _PUBLIC_ACRONYM_CACHE:
        return _PUBLIC_ACRONYM_CACHE[key]

    try:
        # Token-only lookup to avoid sending private document text.
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(acronym)}"
        req = Request(url, headers={"User-Agent": "scanner-filer/1.0"})
        with urlopen(req, timeout=2.5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        summary = str(payload.get("extract", "")).strip()
        guessed = _public_summary_to_type(summary, rules_cfg)
        _PUBLIC_ACRONYM_CACHE[key] = guessed
        return guessed
    except Exception:
        _PUBLIC_ACRONYM_CACHE[key] = rules_cfg.unknown_doc_type
        return rules_cfg.unknown_doc_type


def _apply_public_knowledge_overrides(
    text: str,
    result: ClassificationResult,
    rules_cfg: RulesConfig,
    enabled: bool,
    online_lookup_enabled: bool,
    acronym_overrides: dict[str, str] | None,
) -> ClassificationResult:
    if not enabled:
        return result

    # Respect confident non-unknown outputs; this assist is mainly to resolve unknown/weak cases.
    if result.doc_type != rules_cfg.unknown_doc_type and result.confidence >= 0.8:
        return result

    overrides = {str(k).strip().lower(): str(v).strip().lower() for k, v in (acronym_overrides or {}).items()}
    acronyms = sorted(set(re.findall(r"\b[A-Z][A-Z0-9]{2,10}\b", text)))
    for token in acronyms[:16]:
        token_key = token.lower()
        forced = overrides.get(token_key, "")
        if forced in rules_cfg.allowed_doc_types and forced != rules_cfg.unknown_doc_type:
            result.doc_type = forced
            result.confidence = max(result.confidence, 0.9)
            result.reason = f"public_acronym_override:{token_key}"
            if "public_knowledge" not in result.tags:
                result.tags.append("public_knowledge")
            return result

        if online_lookup_enabled:
            guessed = _lookup_public_acronym_type(token, rules_cfg)
            if guessed in rules_cfg.allowed_doc_types and guessed != rules_cfg.unknown_doc_type:
                result.doc_type = guessed
                result.confidence = max(result.confidence, 0.82)
                result.reason = f"public_online_acronym:{token_key}"
                if "public_knowledge" not in result.tags:
                    result.tags.append("public_knowledge")
                return result

    return result


def classify_text(
    text: str,
    llm_cfg: LLMConfig,
    rules_cfg: RulesConfig,
    *,
    public_knowledge_enabled: bool = False,
    public_online_lookup_enabled: bool = False,
    public_acronym_overrides: dict[str, str] | None = None,
) -> ClassificationResult:
    if not text.strip():
        return ClassificationResult(
            doc_type=rules_cfg.unknown_doc_type,
            vendor_or_sender="unknown",
            date="",
            tags=["no_text"],
            confidence=0.2,
            reason="empty_text",
            suggested_doc_type="",
            suggested_doc_type_confidence=0.0,
        )

    if not llm_cfg.enabled:
        out = _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)
        return _apply_public_knowledge_overrides(
            text,
            out,
            rules_cfg,
            enabled=public_knowledge_enabled,
            online_lookup_enabled=public_online_lookup_enabled,
            acronym_overrides=public_acronym_overrides,
        )

    extra_guidance = llm_cfg.classification_guidance.strip()
    descriptor = llm_cfg.classification_descriptor.strip() or _DEFAULT_CLASSIFICATION_DESCRIPTOR
    if _is_ultra_profile_command(llm_cfg.command_template):
        extra_guidance = (extra_guidance + "\n\n" + _ULTRA_SCENARIO_GUIDANCE).strip() if extra_guidance else _ULTRA_SCENARIO_GUIDANCE
    ruleset_guidance = _build_ruleset_guidance(rules_cfg)
    evidence_terms = _extract_evidence_terms(text[:llm_cfg.max_input_chars], limit=18)
    guidance_block = f"\nAdditional classification guidance:\n{extra_guidance}\n" if extra_guidance else ""
    ruleset_block = f"\nConfigured AI ruleset context:\n{ruleset_guidance}\n" if ruleset_guidance else ""
    evidence_block = (
        "\nExtracted OCR evidence terms (high frequency, context hints):\n"
        + ", ".join(evidence_terms)
        + "\n"
        if evidence_terms
        else ""
    )

    prompt = (
        f"Role descriptor:\n{descriptor}\n\n"
        "You are a strict document classifier. Return JSON only with keys: "
        "doc_type, vendor_or_sender, date, tags, confidence, reason, suggested_doc_type, suggested_doc_type_confidence. "
        f"doc_type must be one of: {', '.join(rules_cfg.allowed_doc_types)}. "
        "date must be YYYY-MM-DD or empty string. confidence is decimal 0..1 (never percentage). "
        "Use at most 4 short tags and keep reason under 12 words. "
        "If confidence is weak or the document should stay in review, you may set doc_type to unknown and set "
        "suggested_doc_type to the most likely allowed category with suggested_doc_type_confidence (0..1). "
        "If the best type is truly not in allowed list, suggested_doc_type may be a short snake_case new category name. "
        "Prioritise semantic document intent over keyword hits. "
        "Behave like a careful filing secretary: classify by what the document is for, not isolated terms. "
        "Do not include markdown.\n"
        f"{guidance_block}\n"
        f"{ruleset_block}\n"
        f"{evidence_block}\n"
        f"Document text:\n{text[:llm_cfg.max_input_chars]}"
    )

    command_template = _select_command_template(llm_cfg)
    started_monotonic = time.monotonic()
    total_timeout_seconds = max(1, int(llm_cfg.timeout_seconds or 0))

    def _remaining_timeout() -> int:
        elapsed = max(0.0, time.monotonic() - started_monotonic)
        return max(0, int(total_timeout_seconds - elapsed))

    def _decode_payload_from_streams(stdout_text: str | None, stderr_text: str | None) -> dict | None:
        stdout_text = stdout_text or ""
        stderr_text = stderr_text or ""

        # Prefer stdout payload first. Some llama.cpp builds print large diagnostics to stderr.
        candidates: list[str] = []
        if stdout_text.strip():
            candidates.append(stdout_text)
        if stderr_text.strip():
            candidates.append(stderr_text)
        if stdout_text.strip() and stderr_text.strip():
            candidates.append(f"{stdout_text}\n{stderr_text}")

        for candidate in candidates:
            json_block = _sanitize_json_block(candidate)
            if not json_block:
                continue
            payload = _decode_valid_payload(json_block, rules_cfg)
            if payload is not None:
                return payload
        return None

    def _run_llm_once(prompt_text: str) -> subprocess.CompletedProcess[str] | None:
        remaining_timeout = _remaining_timeout()
        if remaining_timeout <= 0:
            logger.warning("LLM classification budget exhausted before retry; using fallback")
            return None

        try:
            if "{prompt}" in command_template:
                args = shlex.split(command_template)
                replaced: list[str] = []
                for token in args:
                    if "{prompt}" in token:
                        replaced.append(token.replace("{prompt}", prompt_text))
                    else:
                        replaced.append(token)
                return subprocess.run(
                    replaced,
                    capture_output=True,
                    text=True,
                    timeout=remaining_timeout,
                )

            args = shlex.split(command_template)
            return subprocess.run(
                args,
                input=prompt_text,
                capture_output=True,
                text=True,
                timeout=remaining_timeout,
            )
        except subprocess.TimeoutExpired:
            return None

    logger.info("Running LLM classifier")
    completed = _run_llm_once(prompt)

    if completed is None:
        logger.warning("LLM classifier timeout; using fallback")
        out = _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)
        _record_json_result(llm_cfg, success=False, result_label="timeout")
        return _apply_public_knowledge_overrides(
            text,
            out,
            rules_cfg,
            enabled=public_knowledge_enabled,
            online_lookup_enabled=public_online_lookup_enabled,
            acronym_overrides=public_acronym_overrides,
        )

    payload = _decode_payload_from_streams(completed.stdout, completed.stderr)
    if completed.returncode != 0 and payload is None:
        logger.warning("LLM classifier failed: %s", (completed.stderr or "").strip())
        out = _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)
        _record_json_result(llm_cfg, success=False, result_label="nonzero_exit")
        return _apply_public_knowledge_overrides(
            text,
            out,
            rules_cfg,
            enabled=public_knowledge_enabled,
            online_lookup_enabled=public_online_lookup_enabled,
            acronym_overrides=public_acronym_overrides,
        )

    if payload is None:
        retry_prompt = (
            "Return exactly one JSON object and nothing else. "
            "No markdown. No explanation. "
            "confidence must be decimal 0..1 (never 95-style percentages). "
            "Keep reason under 10 words and max 3 tags. "
            f"Allowed doc_type values: {', '.join(rules_cfg.allowed_doc_types)}.\n"
            f"Document text:\n{text[: max(900, llm_cfg.max_input_chars // 3)]}"
        )
        logger.warning("LLM output had no JSON; retrying with strict JSON prompt")
        retried = _run_llm_once(retry_prompt)
        if retried is not None and retried.returncode == 0:
            payload = _decode_payload_from_streams(retried.stdout, retried.stderr)

    if payload is None:
        tiny_prompt = (
            "JSON only. Keys: doc_type,vendor_or_sender,date,tags,confidence,reason. "
            f"Allowed doc_type: {', '.join(rules_cfg.allowed_doc_types)}. "
            "confidence is decimal 0..1 (not percent). Max 3 tags. Short reason."
        )
        logger.warning("LLM output still not parseable; retrying with tiny JSON prompt")
        retried_tiny = _run_llm_once(f"{tiny_prompt}\n\n{text[:600]}")
        if retried_tiny is not None and retried_tiny.returncode == 0:
            payload = _decode_payload_from_streams(retried_tiny.stdout, retried_tiny.stderr)

    if payload is None:
        logger.warning("LLM output had no parseable JSON after retry; using fallback")
        type_only_prompt = (
            "Return only one token from the allowed doc_type list. "
            "No JSON, no explanation, no punctuation. "
            f"Allowed doc_type: {', '.join(rules_cfg.allowed_doc_types)}.\n"
            "If uncertain return unknown."
        )
        recovered_type = ""
        recovered_run = _run_llm_once(f"{type_only_prompt}\n\n{text[:700]}")
        if recovered_run is not None and recovered_run.returncode == 0:
            recovered_out = (recovered_run.stdout or "").strip()
            if not recovered_out:
                recovered_out = (recovered_run.stderr or "").strip()
            recovered_type = _recover_type_only(recovered_out, rules_cfg)

        if recovered_type:
            recovered = ClassificationResult(
                doc_type=recovered_type,
                vendor_or_sender="unknown",
                date="",
                tags=["llm_type_only"],
                confidence=0.62 if recovered_type != rules_cfg.unknown_doc_type else 0.45,
                reason="llm_type_only_recovery",
                suggested_doc_type="",
                suggested_doc_type_confidence=0.0,
            )
            recovered = _apply_rule_overrides(text, recovered, rules_cfg, allow_positive_overrides=False)
            _record_json_result(llm_cfg, success=False, result_label="json_invalid_type_only")
            return _apply_public_knowledge_overrides(
                text,
                recovered,
                rules_cfg,
                enabled=public_knowledge_enabled,
                online_lookup_enabled=public_online_lookup_enabled,
                acronym_overrides=public_acronym_overrides,
            )

        out = _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)
        _record_json_result(llm_cfg, success=False, result_label="json_invalid")
        return _apply_public_knowledge_overrides(
            text,
            out,
            rules_cfg,
            enabled=public_knowledge_enabled,
            online_lookup_enabled=public_online_lookup_enabled,
            acronym_overrides=public_acronym_overrides,
        )

    try:
        data = payload
        out = _result_from_json(data, rules_cfg)

        # High-confidence self-verification keeps the model in charge instead of brittle rules.
        if out.doc_type != rules_cfg.unknown_doc_type and out.confidence >= 0.88:
            verify_prompt = (
                "You are validating a prior filing decision. Return JSON only with keys: "
                "doc_type, vendor_or_sender, date, tags, confidence, reason. "
                f"Allowed doc_type values: {', '.join(rules_cfg.allowed_doc_types)}. "
                "If initial doc_type is wrong, correct it; otherwise keep it. "
                "Keep reason under 12 words.\n\n"
                f"Initial decision: {json.dumps(data, ensure_ascii=True)}\n"
                f"Evidence terms: {', '.join(evidence_terms)}\n"
                f"Document excerpt:\n{text[: max(1000, llm_cfg.max_input_chars // 2)]}"
            )
            verify_run = _run_llm_once(verify_prompt)
            if verify_run is not None and verify_run.returncode == 0:
                verify_out = (verify_run.stdout or "")
                if verify_run.stderr:
                    verify_out = f"{verify_out}\n{verify_run.stderr}"
                verify_json = _sanitize_json_block(verify_out)
                if verify_json:
                    try:
                        verify_data = json.loads(verify_json)
                        verified = _result_from_json(verify_data, rules_cfg)
                        # Keep the original confident category when verifier only offers unknown.
                        if (
                            out.doc_type != rules_cfg.unknown_doc_type
                            and out.confidence >= 0.88
                            and verified.doc_type == rules_cfg.unknown_doc_type
                        ):
                            verified = out
                        if (
                            verified.doc_type in rules_cfg.allowed_doc_types
                            and verified.doc_type != out.doc_type
                            and verified.confidence >= max(0.78, out.confidence - 0.08)
                        ):
                            logger.info(
                                "LLM verification adjusted doc_type from %s to %s",
                                out.doc_type,
                                verified.doc_type,
                            )
                            out = verified
                    except Exception:
                        pass

        out = _apply_rule_overrides(text, out, rules_cfg, allow_positive_overrides=False)
        _record_json_result(llm_cfg, success=True, result_label="json_ok")
        return _apply_public_knowledge_overrides(
            text,
            out,
            rules_cfg,
            enabled=public_knowledge_enabled,
            online_lookup_enabled=public_online_lookup_enabled,
            acronym_overrides=public_acronym_overrides,
        )
    except Exception:
        logger.warning("LLM JSON parsing failed; using fallback")
        out = _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)
        _record_json_result(llm_cfg, success=False, result_label="json_exception")
        return _apply_public_knowledge_overrides(
            text,
            out,
            rules_cfg,
            enabled=public_knowledge_enabled,
            online_lookup_enabled=public_online_lookup_enabled,
            acronym_overrides=public_acronym_overrides,
        )
