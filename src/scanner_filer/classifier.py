from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass

from .config import LLMConfig, RulesConfig

logger = logging.getLogger(__name__)


@dataclass
class ClassificationResult:
    doc_type: str
    vendor_or_sender: str
    date: str
    tags: list[str]
    confidence: float
    reason: str


def _sanitize_json_block(text: str) -> str | None:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    return match.group(0)


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
    )


def _apply_rule_overrides(text: str, result: ClassificationResult, rules_cfg: RulesConfig) -> ClassificationResult:
    low = text.lower()

    # Positive override: force target category when phrase matches.
    for phrase, forced_type in rules_cfg.keyword_overrides.items():
        if phrase and phrase in low and forced_type in rules_cfg.allowed_doc_types:
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


def classify_text(text: str, llm_cfg: LLMConfig, rules_cfg: RulesConfig) -> ClassificationResult:
    if not text.strip():
        return ClassificationResult(
            doc_type=rules_cfg.unknown_doc_type,
            vendor_or_sender="unknown",
            date="",
            tags=["no_text"],
            confidence=0.2,
            reason="empty_text",
        )

    if not llm_cfg.enabled:
        return _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)

    extra_guidance = llm_cfg.classification_guidance.strip()
    guidance_block = f"\nAdditional classification guidance:\n{extra_guidance}\n" if extra_guidance else ""

    prompt = (
        "You are a strict document classifier. Return JSON only with keys: "
        "doc_type, vendor_or_sender, date, tags, confidence, reason. "
        f"doc_type must be one of: {', '.join(rules_cfg.allowed_doc_types)}. "
        "date must be YYYY-MM-DD or empty string. confidence is 0..1. "
        "Do not include markdown.\n"
        f"{guidance_block}\n"
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
        return _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)

    if completed.returncode != 0:
        logger.warning("LLM classifier failed: %s", completed.stderr.strip())
        return _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)

    json_block = _sanitize_json_block(completed.stdout)
    if not json_block:
        logger.warning("LLM output had no JSON; using fallback")
        return _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)

    try:
        data = json.loads(json_block)
        doc_type = str(data.get("doc_type", rules_cfg.unknown_doc_type)).lower().strip()
        if doc_type not in rules_cfg.allowed_doc_types:
            doc_type = rules_cfg.unknown_doc_type

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
        )
        return _apply_rule_overrides(text, out, rules_cfg)
    except Exception:
        logger.warning("LLM JSON parsing failed; using fallback")
        return _apply_rule_overrides(text, _keyword_fallback(text, rules_cfg), rules_cfg)
