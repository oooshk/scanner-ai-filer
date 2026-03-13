from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import LLMConfig, RulesConfig

logger = logging.getLogger(__name__)

_PUBLIC_ACRONYM_CACHE: dict[str, str] = {}
_ALLOWED_SINGLE_WORD_OVERRIDES = {"dvsa", "hmrc", "nhs", "v5c", "vin", "mot"}
_LOW_SIGNAL_OVERRIDE_WORDS = {
    "act",
    "age",
    "after",
    "affect",
    "address",
    "car",
    "income",
    "may",
    "parts",
    "take",
    "total",
}

_DEFAULT_CLASSIFICATION_DESCRIPTOR = (
    "You are an AI document filing assistant. Classify each document by meaning and context, "
    "not by isolated words."
)


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
    if word in _ALLOWED_SINGLE_WORD_OVERRIDES:
        return True
    if len(word) < 5:
        return False
    return word not in _LOW_SIGNAL_OVERRIDE_WORDS


def _sanitize_json_block(text: str) -> str | None:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    return match.group(0)


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


def _keyword_fallback(text: str, rules_cfg: RulesConfig) -> ClassificationResult:
    low = text.lower()
    if any(k in low for k in ["council tax", "local authority", "council bill"]):
        doc_type = "council_tax"
    elif any(k in low for k in ["bank statement", "sort code", "account number", "statement period"]):
        doc_type = "bank_statement"
    elif any(k in low for k in ["pension", "retirement", "annuity", "state pension"]):
        doc_type = "pension"
    elif any(k in low for k in ["utility", "electricity", "gas", "water", "broadband"]):
        doc_type = "utility_bill"
    elif any(k in low for k in ["insurance", "policy number", "premium"]):
        doc_type = "insurance"
    elif any(k in low for k in ["invoice", "amount due", "bill to"]):
        doc_type = "invoice"
    elif any(k in low for k in ["receipt", "total", "paid"]):
        doc_type = "receipt"
    elif any(k in low for k in ["lab", "clinic", "patient"]):
        doc_type = "medical"
    elif any(k in low for k in ["payslip", "pay period", "national insurance"]):
        doc_type = "payslip"
    elif any(k in low for k in ["tax", "irs", "hmrc"]):
        doc_type = "tax"
    elif any(k in low for k in ["contract", "agreement", "terms and conditions"]):
        doc_type = "legal"
    else:
        doc_type = rules_cfg.unknown_doc_type

    if doc_type not in rules_cfg.allowed_doc_types:
        doc_type = rules_cfg.unknown_doc_type

    return ClassificationResult(
        doc_type=doc_type,
        vendor_or_sender="unknown",
        date="",
        tags=["fallback"],
        confidence=0.45,
        reason="keyword_fallback",
        suggested_doc_type="",
        suggested_doc_type_confidence=0.0,
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
    ruleset_guidance = _build_ruleset_guidance(rules_cfg)
    guidance_block = f"\nAdditional classification guidance:\n{extra_guidance}\n" if extra_guidance else ""
    ruleset_block = f"\nConfigured AI ruleset context:\n{ruleset_guidance}\n" if ruleset_guidance else ""

    prompt = (
        f"Role descriptor:\n{descriptor}\n\n"
        "You are a strict document classifier. Return JSON only with keys: "
        "doc_type, vendor_or_sender, date, tags, confidence, reason, suggested_doc_type, suggested_doc_type_confidence. "
        f"doc_type must be one of: {', '.join(rules_cfg.allowed_doc_types)}. "
        "date must be YYYY-MM-DD or empty string. confidence is 0..1. "
        "If the best type is not in allowed list, set doc_type to unknown and optionally set suggested_doc_type "
        "to a short snake_case category name and suggested_doc_type_confidence (0..1). "
        "Prioritise semantic document intent over keyword hits. "
        "Do not include markdown.\n"
        f"{guidance_block}\n"
        f"{ruleset_block}\n"
        f"Document text:\n{text[:llm_cfg.max_input_chars]}"
    )

    logger.info("Running LLM classifier")
    try:
        if "{prompt}" in llm_cfg.command_template:
            # Use argument mode with placeholder replacement to avoid shell-quoting breakage.
            args = shlex.split(llm_cfg.command_template)
            replaced: list[str] = []
            for token in args:
                if "{prompt}" in token:
                    replaced.append(token.replace("{prompt}", prompt))
                else:
                    replaced.append(token)
            completed = subprocess.run(
                replaced,
                capture_output=True,
                text=True,
                timeout=llm_cfg.timeout_seconds,
            )
        else:
            # Prefer stdin mode (for example, `ollama run <model>`) to avoid shell-quoting issues.
            args = shlex.split(llm_cfg.command_template)
            completed = subprocess.run(
                args,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=llm_cfg.timeout_seconds,
            )
    except subprocess.TimeoutExpired:
        logger.warning("LLM classifier timeout; using fallback")
        out = _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)
        return _apply_public_knowledge_overrides(
            text,
            out,
            rules_cfg,
            enabled=public_knowledge_enabled,
            online_lookup_enabled=public_online_lookup_enabled,
            acronym_overrides=public_acronym_overrides,
        )

    if completed.returncode != 0:
        logger.warning("LLM classifier failed: %s", completed.stderr.strip())
        out = _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)
        return _apply_public_knowledge_overrides(
            text,
            out,
            rules_cfg,
            enabled=public_knowledge_enabled,
            online_lookup_enabled=public_online_lookup_enabled,
            acronym_overrides=public_acronym_overrides,
        )

    json_block = _sanitize_json_block(completed.stdout)
    if not json_block:
        logger.warning("LLM output had no JSON; using fallback")
        out = _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)
        return _apply_public_knowledge_overrides(
            text,
            out,
            rules_cfg,
            enabled=public_knowledge_enabled,
            online_lookup_enabled=public_online_lookup_enabled,
            acronym_overrides=public_acronym_overrides,
        )

    try:
        data = json.loads(json_block)
        doc_type = str(data.get("doc_type", rules_cfg.unknown_doc_type)).lower().strip()
        if doc_type not in rules_cfg.allowed_doc_types:
            doc_type = rules_cfg.unknown_doc_type

        suggested_doc_type = _normalize_doc_type_name(str(data.get("suggested_doc_type", "")))
        if suggested_doc_type in rules_cfg.allowed_doc_types:
            suggested_doc_type = ""

        try:
            suggested_doc_type_confidence = float(data.get("suggested_doc_type_confidence", 0.0))
        except (TypeError, ValueError):
            suggested_doc_type_confidence = 0.0
        suggested_doc_type_confidence = max(0.0, min(1.0, suggested_doc_type_confidence))

        confidence = float(data.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))

        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = []

        out = ClassificationResult(
            doc_type=doc_type,
            vendor_or_sender=str(data.get("vendor_or_sender", "unknown")).strip() or "unknown",
            date=str(data.get("date", "")).strip(),
            tags=[str(t).strip() for t in tags if str(t).strip()],
            confidence=confidence,
            reason=str(data.get("reason", "llm")).strip() or "llm",
            suggested_doc_type=suggested_doc_type,
            suggested_doc_type_confidence=suggested_doc_type_confidence,
        )
        out = _apply_rule_overrides(text, out, rules_cfg, allow_positive_overrides=False)
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
        return _apply_public_knowledge_overrides(
            text,
            out,
            rules_cfg,
            enabled=public_knowledge_enabled,
            online_lookup_enabled=public_online_lookup_enabled,
            acronym_overrides=public_acronym_overrides,
        )
