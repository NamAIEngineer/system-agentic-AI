from __future__ import annotations

import json
import unittest

from optimization import optimize_text


class OptimizationPolicyTests(unittest.TestCase):
    def test_small_output_bypasses_shrinker(self) -> None:
        called = False

        def shrinker(_text: str):
            nonlocal called
            called = True
            return "compact", {"recovery_handle": "mock"}

        original = "short output"
        result, trace = optimize_text(original, min_bytes=10_000, shrinker=shrinker)
        self.assertEqual(result, original)
        self.assertFalse(called)
        self.assertFalse(trace["optimization_used"])
        self.assertEqual(trace["reason"], "below_size_threshold")

    def test_json_is_bypassed_as_structured_fidelity_sensitive_content(self) -> None:
        original = json.dumps({"status": "INVALID", "errors": ["anchor missing"], "padding": "x" * 20_000})
        result, trace = optimize_text(original, min_bytes=1, shrinker=lambda text: ("lossy", {}))
        self.assertEqual(result, original)
        self.assertEqual(trace["reason"], "fidelity_policy_bypass")

    def test_large_terminal_output_keeps_important_finding_and_numbers(self) -> None:
        lines = [f"INFO attempt={n} stage=unit path=src/a.py duration_ms={n + 10}" for n in range(900)]
        lines.append("ERROR security finding path=src/auth.py:41 expected=403 actual=200 severity=critical")
        original = "\n".join(lines) + "\n"
        result, trace = optimize_text(
            original, min_bytes=1,
            shrinker=lambda _text: ("… 900 routine lines elided", {"basis": "fixture", "tokens_before": 100, "tokens_after": 10}),
        )
        self.assertIn("src/auth.py:41", result)
        self.assertIn("expected=403 actual=200", result)
        self.assertIn("severity=critical", result)
        self.assertLess(trace["output_size_bytes"], trace["input_size_bytes"])
        self.assertTrue(trace["optimization_used"])

    def test_failure_returns_exact_original_and_is_traced(self) -> None:
        original = "ERROR check failed path=src/a.py:4\n" + ("routine output\n" * 500)

        def failing(_text: str):
            raise OSError("simulated unavailable")

        result, trace = optimize_text(original, min_bytes=1, shrinker=failing)
        self.assertEqual(result, original)
        self.assertTrue(trace["fallback_used"])
        self.assertFalse(trace["optimization_used"])

    def test_code_diff_and_traceback_bypass(self) -> None:
        for original in (
            "Traceback (most recent call last):\n" + "line\n" * 900,
            "diff --git a/a.py b/a.py\n" + "+x\n" * 900,
        ):
            result, trace = optimize_text(original, min_bytes=1, shrinker=lambda _text: ("x", {}))
            self.assertEqual(result, original)
            self.assertEqual(trace["reason"], "fidelity_policy_bypass")

    def test_secret_like_terminal_output_bypasses_optimizer(self) -> None:
        original = "API_KEY=sk_example-secret-value\n" + ("routine output\n" * 900)
        result, trace = optimize_text(original, min_bytes=1, shrinker=lambda _text: ("redacted", {}))
        self.assertEqual(result, original)
        self.assertEqual(trace["reason"], "fidelity_policy_bypass")

    def test_unverified_shrinker_handle_is_never_exposed_as_recoverable(self) -> None:
        original = "routine output\n" * 900
        result, trace = optimize_text(
            original, min_bytes=1,
            shrinker=lambda _text: ("compact output", {"recovery_handle": "fabricated_handle"}),
        )
        self.assertTrue(trace["optimization_used"])
        self.assertFalse(trace["recovery_verified"])
        self.assertNotIn("recovery_handle", trace)
        self.assertLess(len(result.encode()), len(original.encode()))


if __name__ == "__main__":
    unittest.main()
