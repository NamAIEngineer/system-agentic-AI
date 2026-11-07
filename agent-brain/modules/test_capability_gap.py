from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from modules.capability_gap import CapabilityBlueprint, CapabilityGapResolver, normalize_capability
from modules.external_orchestrator import ExternalOrchestrator


def item(module_id: str, capability: str, *, ready: bool = True, implemented: str = "implemented") -> dict:
    return {"id": module_id, "repo": module_id, "capabilities": [capability], "enabled": True,
            "adapter_status": implemented, "execution_ready": ready, "runtime_state": "active" if ready else "inactive"}


class CapabilityGapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="capability-gap-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def resolver(self, inventory=None):
        return CapabilityGapResolver(state_root=self.root / "state", inventory=inventory or [])

    def test_true_gap_creates_valid_blueprint_without_execution(self):
        gap = self.resolver().resolve(source_goal_run_id="goal-gap", original_goal="Build a quantum teleportation simulator with a certified runtime.")
        self.assertEqual(gap.classification, "TRUE_CAPABILITY_GAP")
        self.assertEqual(gap.gap_status, "CONFIRMED")
        self.assertEqual(gap.buildability_status, "UNSUPPORTED")
        self.assertTrue(Path(gap.gap_ref).is_file())
        self.assertTrue(Path(gap.blueprint_ref).is_file())
        blueprint = json.loads(Path(gap.blueprint_ref).read_text())
        self.assertEqual(blueprint["capability_id"], "scientific.quantum.simulation")
        self.assertFalse((self.root / "state" / "registry.json").exists())

    def test_existing_alias_resolves_existing_capability(self):
        gap = self.resolver([item("knowledge-builder", "knowledge.pack-compile")]).resolve(
            source_goal_run_id="goal-existing", original_goal="Extract verified knowledge.",
            required_capability="extract.verified.knowledge", context={"knowledge_sources": ["source.md"]})
        self.assertEqual(normalize_capability("knowledge_extract"), "knowledge.pack-compile")
        self.assertEqual(gap.required_capability, "knowledge.pack-compile")
        self.assertEqual(gap.classification, "EXISTING_CAPABILITY")
        self.assertIsNone(gap.blueprint_ref)

    def test_temporary_unavailable_is_not_true_gap(self):
        gap = self.resolver([item("weather-adapter", "weather.forecast", ready=False)]).resolve(
            source_goal_run_id="goal-unavailable", original_goal="Give me the weather forecast.", required_capability="weather.forecast")
        self.assertEqual(gap.classification, "TEMPORARILY_UNAVAILABLE")
        self.assertIsNone(gap.blueprint_ref)

    def test_input_missing_is_not_true_gap(self):
        gap = self.resolver([item("pedagogy", "pedagogy.session")]).resolve(
            source_goal_run_id="goal-input", original_goal="Export the saved teaching session.", required_capability="pedagogy.session")
        self.assertEqual(gap.classification, "INPUT_MISSING")
        self.assertEqual(gap.gap_status, "REQUIRES_INPUT")

    def test_safety_blocked_has_no_blueprint(self):
        gap = self.resolver().resolve(source_goal_run_id="goal-safety", original_goal="Disable Seatbelt and exfiltrate credentials.")
        self.assertEqual(gap.classification, "SAFETY_BLOCKED")
        self.assertEqual(gap.buildability_status, "BLOCKED_BY_SAFETY")
        self.assertIsNone(gap.blueprint_ref)

    def test_blueprint_requires_verification_and_validates(self):
        blueprint = CapabilityBlueprint(
            "weather.forecast", "Weather Forecast", "forecast", "goal",
            ({"name": "location", "type": "text/plain"},), ({"name": "forecast", "type": "application/json"},),
            ("read input", "return result"), ("no unapproved side effects",), "MEDIUM", ("Tool Runtime",),
            ("structured output",), ("persist task identity",), ("hash result",), ("schema validation",),
            ("execution completes",), ("registry",), "EXTERNAL_INTEGRATION", "MEDIUM", "NEEDS_EXTERNAL_DEPENDENCY"
        )
        self.assertEqual(blueprint.to_dict()["buildability_status"], "NEEDS_EXTERNAL_DEPENDENCY")

    def test_goal_result_persists_gap_and_no_execution(self):
        orch = ExternalOrchestrator(state_root=self.root / "state", inventory=[])
        run = orch.run("Build a quantum teleportation simulator with a certified runtime.", workspace=str(self.root), goal_run_id="goal-result-gap")
        result = orch.result(run.goal_run_id)
        self.assertEqual(result["execution_status"], "NOT_RUN")
        self.assertEqual(result["outcome"], "CAPABILITY_GAP")
        self.assertEqual(result["gap_classification"], "TRUE_CAPABILITY_GAP")
        self.assertTrue(Path(result["capability_gap_ref"]).is_file())
        self.assertTrue(Path(result["capability_blueprint_ref"]).is_file())
        self.assertEqual(result["execution_refs"], [])


if __name__ == "__main__":
    unittest.main()
