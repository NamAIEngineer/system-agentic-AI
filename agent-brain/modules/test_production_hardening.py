from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from modules.external_orchestrator import GoalRun, GoalRunStore
from modules.runtime import generated_module_entry, load_module_registry
from modules.orchestrator_service import OrchestratorService, ServiceConfig


class ProductionHardeningTests(unittest.TestCase):
    def test_unknown_goal_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "bad.json"
            path.write_text(json.dumps({"schema_version": 99, "goal_run_id": "g"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsupported GoalRun schema"):
                GoalRun.from_dict(json.loads(path.read_text()))

    def test_tampered_trusted_package_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            package = root / "trusted"; package.mkdir()
            (package / "candidate.py").write_text("print('ok')\n", encoding="utf-8")
            (package / "fixture.json").write_text("[]\n", encoding="utf-8")
            entry = generated_module_entry("fixture.safe", str(package), promotion_id="p1", candidate_id="c1", blueprint_ref="bp", package_hash="bad")
            overlay = root / "overlay.json"
            overlay.write_text(json.dumps({"schema_version": 1, "modules": [entry]}), encoding="utf-8")
            with patch.dict(os.environ, {"SYSTEM_AGENTIC_TRUSTED_REGISTRY": str(overlay)}):
                with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                    load_module_registry()

    def test_missing_trusted_package_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            package = root / "missing"
            entry = {"schema_version": 1, "id": "generated-x", "workspace_path": str(package),
                     "generated_origin": "promoted", "promotion_id": "p", "capabilities": ["x"]}
            overlay = root / "overlay.json"
            overlay.write_text(json.dumps({"schema_version": 1, "modules": [entry]}), encoding="utf-8")
            with patch.dict(os.environ, {"SYSTEM_AGENTIC_TRUSTED_REGISTRY": str(overlay)}):
                with self.assertRaisesRegex(ValueError, "package missing"):
                    load_module_registry()

    def test_doctor_reports_corrupt_registry_without_repair(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            state = root / "state"
            state.mkdir()
            overlay = root / "overlay.json"
            overlay.write_text("{not-json", encoding="utf-8")
            with patch.dict(os.environ, {"SYSTEM_AGENTIC_TRUSTED_REGISTRY": str(overlay)}):
                doctor = OrchestratorService(ServiceConfig(state, port=0)).doctor()
            self.assertEqual(doctor["status"], "FAIL")
            self.assertEqual(doctor["checks"]["registry"], "FAIL")
            self.assertTrue(overlay.exists())


if __name__ == "__main__":
    unittest.main()
