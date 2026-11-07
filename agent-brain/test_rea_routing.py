from __future__ import annotations

import json
import unittest
from pathlib import Path

from decision_plane import DecisionPlane


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "agent-brain" / "test-fixtures" / "routing-fixtures.json"


def _answers():
    return {"route": {"choice": "DIRECT", "confidence": 0.99}, "module": {"choice": "none", "confidence": 0.99}, "execution_class": {"choice": "READ_ONLY", "confidence": 0.99}}


class ReaRoutingBenchmark(unittest.TestCase):
    def test_routing_fixture_families(self):
        fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
        fake_rea = lambda **_kwargs: [{"id": "rea", "execution_ready": True, "capabilities": ["REVERSE_ENGINEERING"]}]
        plane = DecisionPlane(quyet=lambda *_args, **_kwargs: {"answers": _answers()}, modules=fake_rea)
        counts = {family: 0 for family in fixtures}
        for family, prompts in fixtures.items():
            for prompt in prompts:
                result = plane.decide(prompt)
                counts[family] += 1
                if family == "reverse_engineering":
                    self.assertEqual(result["route_lane"], "REA", prompt)
                    self.assertEqual(result["choices"]["route"], "DISPATCH", prompt)
                    self.assertEqual(result["choices"]["module"], "rea", prompt)
                    self.assertEqual(result["capability"], "REVERSE_ENGINEERING", prompt)
                    self.assertEqual(result["provider"], "rea", prompt)
                elif family == "coding":
                    self.assertEqual(result["route_lane"], "CODING", prompt)
                    self.assertNotEqual(result.get("provider"), "rea", prompt)
                elif family == "computer_use":
                    self.assertEqual(result["route_lane"], "ORCA", prompt)
                    self.assertNotEqual(result.get("provider"), "rea", prompt)
                elif family == "review":
                    self.assertEqual(result["route_lane"], "GSTACK", prompt)
                    self.assertNotEqual(result.get("provider"), "rea", prompt)
                else:
                    self.assertEqual(result["route_lane"], "HOST", prompt)
                    self.assertNotEqual(result.get("provider"), "rea", prompt)
        self.assertEqual(counts, {"reverse_engineering": 10, "coding": 10, "computer_use": 5, "review": 5, "ambiguous": 5})


if __name__ == "__main__":
    unittest.main()
