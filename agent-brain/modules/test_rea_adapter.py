from __future__ import annotations

import json
import os
import signal
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from modules.rea_adapter import ReaMcpError, _McpSession, _tool_request, execute, rea_health
from providers.runtime import ProviderTask


FIXTURE = Path(__file__).resolve().parents[1] / "test-fixtures" / "reverse-engineering" / "electron-like"
BINARY = Path(__file__).resolve().parents[1] / "test-fixtures" / "reverse-engineering" / "bin" / "add"


def task(target: Path, target_type: str) -> ProviderTask:
    return ProviderTask(
        target,
        "Inspect an explicitly authorized local fixture.",
        role="analyst",
        required_capabilities=("task.submit", "REVERSE_ENGINEERING"),
        context={"target_path": str(target), "target_type": target_type, "target_authorized": True},
        execution_policy="READ_ONLY",
        execution_context={"host_role": "orchestrator", "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH"},
    )


class ReaAdapterTests(unittest.TestCase):
    def test_health_discovers_real_mcp_tools_and_granular_engines(self):
        health = rea_health()
        self.assertEqual(health["status"], "READY")
        self.assertEqual(health["mcp_status"], "READY")
        self.assertGreaterEqual(health["tool_count"], 1)
        self.assertIn("analyze_javascript_application", health["tools"])
        self.assertIn("native_external_engines", health["capabilities"])
        for capability in ("android_package", "desktop", "managed", "firmware", "web_static", "web_runtime", "web_network_capture"):
            self.assertIn(capability, health["capabilities"])
            self.assertIn(health["capabilities"][capability], {"READY", "UNAVAILABLE"})

    def test_javascript_fixture_returns_evidence_provenance_and_limitations(self):
        result = execute(task(FIXTURE, "javascript_application"), wait_timeout_seconds=30)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(len(result.evidence), 1)
        evidence = result.evidence[0]
        self.assertEqual(evidence["provider"], "rea")
        self.assertEqual(evidence["capability"], "REVERSE_ENGINEERING")
        self.assertEqual(evidence["target"], str(FIXTURE.resolve()))
        self.assertEqual(evidence["tool"], "analyze_javascript_application")
        self.assertIn("provenance", evidence)
        self.assertIn("limitations", evidence)
        self.assertIn("findings", evidence)

    def test_binary_fixture_uses_read_only_objdump_operation(self):
        result = execute(task(BINARY, "binary"), wait_timeout_seconds=30)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.evidence[0]["tool"], "inspect_with_objdump")

    def test_explicit_artifact_types_map_to_read_only_mcp_tools(self):
        cases = {
            "android_application": "inspect_android_package",
            "desktop_application": "inspect_desktop_application",
            "managed_assembly": "inspect_managed_artifact",
            "firmware": "inspect_firmware_regions",
            "web_network_capture": "inspect_web_network_capture",
            "network_capture": "inspect_packet_capture",
            "native_exports": "inspect_native_exports",
            "binary_layout": "inspect_binary_layout",
        }
        for target_type, expected_tool in cases.items():
            with self.subTest(target_type=target_type):
                tool_name, arguments = _tool_request(FIXTURE, target_type, task(FIXTURE, target_type))
                self.assertEqual(tool_name, expected_tool)
                self.assertTrue(arguments)

    def test_runtime_web_mapping_requires_loopback_endpoint_and_target_id(self):
        runtime_task = task(FIXTURE, "web_page")
        runtime_task = replace(runtime_task, context={
            **runtime_task.context, "cdp_endpoint": "http://127.0.0.1:9222", "target_id": "page-1",
        })
        tool_name, arguments = _tool_request(FIXTURE, "web_page", runtime_task)
        self.assertEqual(tool_name, "inspect_web_page")
        self.assertEqual(arguments["target_id"], "page-1")
        with self.assertRaisesRegex(ReaMcpError, "loopback"):
            _tool_request(FIXTURE, "web_page", replace(runtime_task, context={
                **runtime_task.context, "cdp_endpoint": "https://example.com",
            }))

    def test_missing_authorization_and_write_policy_fail_closed(self):
        no_auth = ProviderTask(FIXTURE, "Inspect", role="analyst", context={"target_path": str(FIXTURE)}, execution_policy="READ_ONLY")
        with self.assertRaisesRegex(ReaMcpError, "target_authorized"):
            execute(no_auth)
        with self.assertRaisesRegex(ReaMcpError, "read-only"):
            execute(replace(task(FIXTURE, "javascript_application"), execution_policy="WORKSPACE_WRITE"))

    def test_killed_mcp_session_is_isolated_and_fresh_session_recovers(self):
        session = _McpSession(start_timeout=15, tool_timeout=15)
        try:
            session.initialize()
            pid = session.process.pid
            os.kill(pid, signal.SIGKILL)
            with self.assertRaises(ReaMcpError):
                session.list_tools()
        finally:
            session.close()
        recovered = rea_health()
        self.assertEqual(recovered["status"], "READY")
        self.assertEqual(recovered["mcp_status"], "READY")


if __name__ == "__main__":
    unittest.main()
