from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from providers.runtime import ProviderTask
from modules.browser_adapter import BrowserAdapterError, browser_health, execute_browser_module


class BrowserAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="browser-adapter-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_external_service_mutation_is_blocked_by_default(self) -> None:
        task = ProviderTask(
            self.root, "external mutation", execution_policy="READ_ONLY",
            context={"browser": {"url": "http://127.0.0.1:1/", "external_service": True,
                                 "ownership": "external-lane-owned",
                                 "actions": [{"action": "click", "selector": "button"}]}},
        )
        with self.assertRaisesRegex(BrowserAdapterError, "external service mutation"):
            execute_browser_module(task)

    def test_browser_runtime_is_discoverable(self) -> None:
        self.assertEqual(browser_health()["status"], "READY")


if __name__ == "__main__":
    unittest.main()
