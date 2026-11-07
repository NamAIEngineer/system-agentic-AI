from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from providers.runtime import ProviderTask
from modules.coding_adapter import execute_coding_module


class CodingAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="coding-adapter-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "fixture.py").write_text("VALUE = 1\n", encoding="utf-8")

    def test_declared_test_failure_returns_receipt_and_preserves_patch(self) -> None:
        result = execute_coding_module(ProviderTask(
            self.root, "controlled failure", execution_policy="WORKSPACE_WRITE",
            context={"coding": {"patch": {"path": "fixture.py", "old": "VALUE = 1", "new": "VALUE = 2"},
                                "test_command": f'{sys.executable} -c "import sys; sys.exit(3)"'}},
        ))
        self.assertEqual(result["execution_status"], "FAILED")
        self.assertEqual(result["domain_outcome"], "CODING_TEST_FAILED")
        self.assertEqual(result["verification"]["exit_code"], 3)
        self.assertEqual((self.root / "fixture.py").read_text(), "VALUE = 2\n")

    def test_missing_command_is_normalized_without_raising(self) -> None:
        result = execute_coding_module(ProviderTask(
            self.root, "missing dependency", execution_policy="WORKSPACE_WRITE",
            context={"coding": {"test_command": str(self.root / "missing-command")}},
        ))
        self.assertEqual(result["execution_status"], "FAILED")
        self.assertEqual(result["errors"][0]["code"], "COMMAND_UNAVAILABLE")
        self.assertIsNone(result["verification"]["exit_code"])


if __name__ == "__main__":
    unittest.main()
