from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scanner_filer.classifier import classify_text
from scanner_filer.config import LLMConfig, RulesConfig


class ClassifierTimeoutBudgetTests(unittest.TestCase):
    def _llm_cfg(self) -> LLMConfig:
        state_dir = Path("/tmp/scanner-test-state")
        return LLMConfig(
            enabled=True,
            command_template="fake-llm --prompt {prompt}",
            fallback_command_template="",
            timeout_seconds=2,
            max_input_chars=1000,
            min_confidence_autofile=0.75,
            json_metrics_path=state_dir / "llm_json_metrics.json",
            json_failure_window_size=10,
            json_failure_rate_threshold=0.35,
            json_consecutive_failure_threshold=3,
            json_fallback_cooldown_calls=10,
            classification_descriptor="",
            classification_guidance="",
            category_suggestion_enabled=True,
            auto_create_suggested_categories=False,
            auto_create_min_confidence=0.93,
            retry_on_failure_enabled=False,
            retry_profile="deep",
            retry_command_template="",
        )

    def _rules_cfg(self) -> RulesConfig:
        return RulesConfig(
            allowed_doc_types=["invoice", "unknown"],
            unknown_doc_type="unknown",
            keyword_overrides={},
            negative_type_keywords={},
            per_type_min_confidence={},
        )

    def test_retries_share_single_timeout_budget(self) -> None:
        calls: list[float] = []
        tick = {"value": 0.0}

        def fake_monotonic() -> float:
            current = tick["value"]
            tick["value"] += 0.9
            return current

        def fake_run(args, **kwargs):
            calls.append(float(kwargs.get("timeout", 0)))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="not-json", stderr="")

        with patch("scanner_filer.classifier.time.monotonic", side_effect=fake_monotonic), patch(
            "scanner_filer.classifier.subprocess.run", side_effect=fake_run
        ):
            result = classify_text("Invoice for plumbing work", self._llm_cfg(), self._rules_cfg())

        self.assertEqual(result.reason, "keyword_fallback")
        self.assertGreaterEqual(len(calls), 1)
        self.assertLess(len(calls), 4, f"expected budgeted retries, got {calls}")
        if len(calls) > 1:
            self.assertLess(calls[-1], calls[0], f"expected decreasing timeouts, got {calls}")


if __name__ == "__main__":
    unittest.main()
