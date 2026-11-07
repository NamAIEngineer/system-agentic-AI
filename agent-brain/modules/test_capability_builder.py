from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modules.capability_builder import CapabilityBuilder
from modules.capability_gap import CapabilityBlueprint, CapabilityGapResolver


def bp_for(gap):
    raw = json.loads(Path(gap.blueprint_ref).read_text(encoding="utf-8"))
    tuple_keys = {"input_contract", "output_contract", "execution_semantics", "side_effects",
                  "required_tools", "required_provider_properties", "state_requirements",
                  "artifact_requirements", "verification_contract", "acceptance_criteria", "dependencies"}
    return CapabilityBlueprint(**{key: tuple(value) if key in tuple_keys else value for key, value in raw.items()})


class CapabilityBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="capability-builder-")
        self.root = Path(self.tmp.name)
        self.gap = CapabilityGapResolver(state_root=self.root / "state", inventory=[]).resolve(
            source_goal_run_id="builder-test", original_goal="Summarize these CSV table rows by category and total value.")
        self.blueprint = bp_for(self.gap)
        self.builder = CapabilityBuilder(state_root=self.root / "state", workspace=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_builds_verified_isolated_candidate_and_is_idempotent(self):
        with patch.object(self.builder, "_reuse_found", return_value=False), patch.object(self.builder, "_select_provider", return_value=({"provider_id": "test", "eligible_providers": ["test"], "selection_reason": "test"}, {})):
            candidate = self.builder.build(self.gap, self.blueprint)
        self.assertEqual(candidate.candidate_status, "VERIFIED")
        self.assertEqual(candidate.verification_status, "VALID")
        self.assertFalse(candidate.manifest["trusted"])
        self.assertTrue(Path(candidate.candidate_root, "candidate.py").is_file())
        with patch.object(self.builder, "_reuse_found", return_value=False):
            again = self.builder.build(self.gap, self.blueprint)
        self.assertEqual(again.candidate_id, candidate.candidate_id)
        self.assertEqual(again.candidate_status, "VERIFIED")

    def test_unsupported_and_safety_refuse(self):
        unsupported = self.blueprint.__class__(**{**self.blueprint.__dict__, "buildability_status": "UNSUPPORTED"})
        result = self.builder.build(self.gap, unsupported)
        self.assertEqual(result["status"], "NOT_BUILDABLE")
        blocked_gap = self.gap.__class__(**{**self.gap.__dict__, "classification": "SAFETY_BLOCKED", "buildability_status": "BLOCKED_BY_SAFETY"})
        self.assertEqual(self.builder.build(blocked_gap, self.blueprint)["status"], "SAFETY_BLOCKED")

    def test_invalid_blueprint_refuses_before_workspace(self):
        invalid = self.blueprint.__class__(**{**self.blueprint.__dict__, "capability_id": "bad"})
        result = self.builder.build(self.gap, invalid)
        self.assertEqual(result["status"], "INVALID_BLUEPRINT")


if __name__ == "__main__":
    unittest.main()
