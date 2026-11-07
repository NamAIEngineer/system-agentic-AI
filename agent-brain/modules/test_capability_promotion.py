from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os

from modules.capability_builder import CapabilityBuilder, CandidateImplementation
from modules.capability_gap import CapabilityBlueprint, CapabilityGapResolver
from modules.capability_promotion import CapabilityPromotion
from modules.runtime import module_inventory


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="promotion-")
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.env = patch.dict(os.environ, {"SYSTEM_AGENTIC_TRUSTED_REGISTRY": str(self.root / "overlay.json"), "SYSTEM_AGENTIC_TRUSTED_ROOT": str(self.root / "trusted")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.goal = "Summarize these CSV table rows by category and total value."
        self.gap = CapabilityGapResolver(state_root=self.state, inventory=[]).resolve(source_goal_run_id="promotion-test", original_goal=self.goal)
        raw = json.loads(Path(self.gap.blueprint_ref).read_text())
        lists = {"input_contract", "output_contract", "execution_semantics", "side_effects", "required_tools", "required_provider_properties", "state_requirements", "artifact_requirements", "verification_contract", "acceptance_criteria", "dependencies"}
        self.blueprint = CapabilityBlueprint(**{k: tuple(v) if k in lists else v for k, v in raw.items()})
        builder = CapabilityBuilder(state_root=self.state, workspace=self.root)
        with patch.object(builder, "_reuse_found", return_value=False), patch.object(builder, "_select_provider", return_value=({"provider_id": "test", "eligible_providers": ["test"], "selection_reason": "test"}, {})):
            built = builder.build(self.gap, self.blueprint)
        self.candidate = built if isinstance(built, CandidateImplementation) else CandidateImplementation(**built)
        self.promotion = CapabilityPromotion(state_root=self.state)
        self.addCleanup(lambda: self.promotion.rollback(self.promotion.trusted_root / "data-table-summarize", "generated-data-table-summarize"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_approve_and_idempotent_decision(self):
        decision = self.promotion.promote(self.gap, self.blueprint, self.candidate)
        self.assertEqual(decision["decision"] if isinstance(decision, dict) else decision.decision, "APPROVED")
        self.assertTrue(any("data.table.summarize" in m.get("capabilities", []) for m in module_inventory()))
        again = self.promotion.promote(self.gap, self.blueprint, self.candidate)
        self.assertEqual(again["decision"], "APPROVED")

    def test_stale_candidate_is_rejected(self):
        path = Path(self.candidate.candidate_root) / "candidate.py"
        path.write_text(path.read_text() + "\n# changed\n")
        self.assertEqual(self.promotion.promote(self.gap, self.blueprint, self.candidate)["decision"], "STALE_CANDIDATE")

    def test_invalid_candidate_is_rejected(self):
        bad = CandidateImplementation(**{**self.candidate.to_dict(), "candidate_status": "GENERATED"})
        self.assertEqual(self.promotion.promote(self.gap, self.blueprint, bad)["decision"], "REJECTED")


if __name__ == "__main__":
    unittest.main()
