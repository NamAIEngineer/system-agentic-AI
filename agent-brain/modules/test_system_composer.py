from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modules.system_composer import (
    BlueprintStep,
    SystemBlueprint,
    compile_blueprint,
    design_system,
    run_system,
    validate_blueprint,
)
from modules.runtime import load_module_registry


def ready_inventory() -> list[dict]:
    registry = load_module_registry()["modules"]
    keep = {"pedagogy", "knowledge-builder", "gstack"}
    return [{**item, "execution_ready": item["id"] in keep, "available": item["id"] in keep}
            for item in registry if item["id"] in keep]


class SystemComposerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="system-composer-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.inventory = ready_inventory()
        self.goal = "Xuất phiên luyện dạy phân số thành tài liệu rồi biên soạn knowledge pack có kiểm chứng nguồn."
        self.context = {"pedagogy_request": {"session_id": "00000000-0000-0000-0000-000000000000"}}

    def design(self, goal: str | None = None, **kwargs):
        options = {"workspace": str(self.root), "execution_policy": "WORKSPACE_WRITE",
                   "context": self.context, "inventory": self.inventory, "system_id": "system-test"}
        options.update(kwargs)
        return design_system(goal or self.goal, **options)

    def test_goal_derives_capabilities_without_module_names(self):
        blueprint = self.design()
        self.assertEqual([item["capability"] for item in blueprint.required_capabilities],
                         ["pedagogy.session", "knowledge.pack-compile"])
        self.assertEqual([step.module_id for step in blueprint.steps], ["pedagogy", "knowledge-builder"])

    def test_valid_blueprint_compiles_to_composition_plan(self):
        blueprint = self.design()
        validate_blueprint(blueprint, inventory=self.inventory)
        plan = compile_blueprint(blueprint)
        self.assertEqual(plan.pipeline_id, "system-test")
        self.assertEqual(dict(plan.steps[1].input_bindings[0]),
                         {"from": "session-export:export", "to": "knowledge_sources"})

    def test_binding_is_declared_and_byte_transport_is_runtime_owned(self):
        blueprint = self.design()
        step = blueprint.steps[1]
        self.assertEqual(step.inputs[0]["from"], "session-export:export")
        self.assertEqual(step.inputs[0]["to"], "knowledge_sources")
        self.assertEqual(blueprint.to_dict()["artifact_handoff_requirements"][0]["step"], "validated-knowledge")
        self.assertTrue(blueprint.to_dict()["artifact_handoff_requirements"][0]["byte_preserving"])
        self.assertEqual(blueprint.to_dict()["artifact_handoff_requirements"][0]["transformation"], "none")

    def test_invalid_dependency_is_rejected(self):
        blueprint = self.design()
        invalid = replace(blueprint, steps=(blueprint.steps[0], replace(blueprint.steps[1], depends_on=("missing",))))
        with self.assertRaises(ValueError):
            validate_blueprint(invalid, inventory=self.inventory)

    def test_incompatible_artifact_binding_is_rejected(self):
        blueprint = self.design()
        invalid_input = {"from": "session-export:export", "to": "knowledge_sources", "type": "application/json"}
        invalid = replace(blueprint, steps=(blueprint.steps[0], replace(blueprint.steps[1], inputs=(invalid_input,))))
        with self.assertRaisesRegex(ValueError, "Incompatible"):
            validate_blueprint(invalid, inventory=self.inventory)

    def test_single_capability_does_not_require_composition(self):
        blueprint = self.design("Review this code for correctness and release risks.")
        self.assertEqual(blueprint.blueprint_status, "VALID")
        self.assertFalse(blueprint.composition_required)
        self.assertEqual(len(blueprint.steps), 1)

    def test_missing_capability_is_unresolved_and_does_not_execute(self):
        blueprint = self.design("Build a quantum teleportation simulator with a certified runtime.")
        self.assertEqual(blueprint.blueprint_status, "UNRESOLVED")
        self.assertEqual(blueprint.unresolved_requirements[0]["status"], "UNRESOLVED_CAPABILITY")
        result = run_system(blueprint)
        self.assertEqual(result["system_execution_status"], "NOT_RUN")
        self.assertFalse(result["acceptance_passed"])
        self.assertIsNone(result["compiled_plan"])
        self.assertTrue((self.root / ".system-agentic-systems/system-test/system-result.json").is_file())

    def test_readiness_filter_excludes_disabled_or_unimplemented_modules(self):
        inventory = [{**item, "execution_ready": False} for item in self.inventory]
        blueprint = self.design(inventory=inventory)
        self.assertEqual(blueprint.blueprint_status, "UNRESOLVED")
        self.assertFalse(blueprint.steps)

    def test_safety_rejects_elevated_policy(self):
        with self.assertRaises(ValueError):
            design_system(self.goal, workspace=str(self.root), execution_policy="ELEVATED", inventory=self.inventory)

    def test_status_semantics_and_persistence(self):
        blueprint = self.design()
        fake_plan = compile_blueprint(blueprint)
        fake_result = {"workflow_status": "COMPLETED", "acceptance_passed": True,
                       "trace_path": str(self.root / "trace.json"), "steps": [], "verification": {}}
        with patch("modules.system_composer.run_plan", return_value=fake_result):
            result = run_system(blueprint)
        self.assertEqual(result["blueprint_status"], "VALID")
        self.assertEqual(result["system_execution_status"], "COMPLETED")
        self.assertTrue(result["acceptance_passed"])
        saved = json.loads((self.root / ".system-agentic-systems/system-test/system-result.json").read_text())
        self.assertEqual(saved["compiled_plan"]["pipelineId"], fake_plan.pipeline_id)
        self.assertEqual(saved["persistence"]["durable_resume"], "NOT_SUPPORTED")

    def test_provider_or_runtime_failure_is_failed_and_not_accepted(self):
        blueprint = self.design()
        failed = {"workflow_status": "BLOCKED", "acceptance_passed": False,
                  "failure_reason": "synthetic provider failure", "trace_path": None}
        with patch("modules.system_composer.run_plan", return_value=failed):
            result = run_system(blueprint)
        self.assertEqual(result["blueprint_status"], "VALID")
        self.assertEqual(result["system_execution_status"], "FAILED")
        self.assertFalse(result["acceptance_passed"])


if __name__ == "__main__":
    unittest.main()
