from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from providers.runtime import ProviderTask
from modules.browser_adapter import execute_browser_module
from modules.browser_extension_transport import ExtensionTransportError, execute_extension_browser


class ExtensionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.context = {"transport": "extension", "authorized_origins": ["https://example.test"],
                        "actions": [{"action": "click", "role": "button", "name": "Save"}],
                        "expect_text": "Saved", "allow_external_mutation": True}

    def test_permission_checks_precede_connection_or_browser_access(self):
        with patch("modules.browser_extension_transport._connection") as connection:
            with self.assertRaisesRegex(ExtensionTransportError, "WORKSPACE_WRITE"):
                execute_extension_browser(ProviderTask(self.workspace, "read-only"), self.context)
            self.context["allow_external_mutation"] = False
            with self.assertRaisesRegex(ExtensionTransportError, "allow_external_mutation"):
                execute_extension_browser(ProviderTask(self.workspace, "not authorized", execution_policy="WORKSPACE_WRITE"), self.context)
            connection.assert_not_called()

    def test_invalid_postcondition_prevents_any_write(self):
        self.context.pop("expect_text")
        with self.assertRaisesRegex(ExtensionTransportError, "postcondition"):
            execute_extension_browser(ProviderTask(self.workspace, "missing assertion", execution_policy="WORKSPACE_WRITE"), self.context)

    def test_indeterminate_mutation_preserves_unknown_and_recovery_reference(self):
        with patch("modules.browser_extension_transport.execute_extension_browser",
                   side_effect=ExtensionTransportError("timeout; never replay", status="UNCERTAIN", request_id="receipt-1")):
            result = execute_browser_module(ProviderTask(self.workspace, "timeout", context={"browser": self.context}))
        self.assertEqual(result["execution_status"], "UNKNOWN")
        self.assertEqual(result["evidence"][0]["receipt_id"], "receipt-1")

    def test_partial_batch_keeps_success_receipts_and_stops(self):
        observed = {"url": "https://example.test/page", "elements": [{"role": "button", "name": "Save", "ref": "node-1"}],
                    "lease": "lease", "epoch": 1, "pageId": "page"}
        with patch("modules.browser_extension_transport._connection", return_value={}), patch(
            "modules.browser_extension_transport._rpc", side_effect=[observed, {"id": "saved-1", "status": "COMPLETED"},
                                                                     ExtensionTransportError("lost subsequent observation")]) as rpc:
            result = execute_browser_module(ProviderTask(self.workspace, "partial", execution_policy="WORKSPACE_WRITE", context={"browser": self.context}))
        self.assertEqual(result["execution_status"], "FAILED")
        self.assertEqual(result["domain_outcome"], "BROWSER_PARTIAL")
        self.assertEqual(result["evidence"][0]["receipt_id"], "saved-1")
        self.assertEqual(rpc.call_count, 3)

    def test_ambiguous_target_never_dispatches_action(self):
        observed = {"url": "https://example.test/page", "elements": [{"role": "button", "name": "Save"}] * 2}
        with patch("modules.browser_extension_transport._connection", return_value={}), patch(
            "modules.browser_extension_transport._rpc", return_value=observed) as rpc:
            with self.assertRaisesRegex(ExtensionTransportError, "unique"):
                execute_extension_browser(ProviderTask(self.workspace, "ambiguous", execution_policy="WORKSPACE_WRITE"), self.context)
            self.assertEqual(rpc.call_count, 1)


if __name__ == "__main__":
    unittest.main()
