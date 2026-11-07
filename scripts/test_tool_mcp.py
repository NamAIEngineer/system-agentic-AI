from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "scripts/system-agentic-tool-mcp"


class StdioMcpAdapterTests(unittest.TestCase):
    def test_stdio_requests_use_real_runtime_and_normalize_success_and_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "fixture.txt").write_text("MCP fixture\n", encoding="utf-8")
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "workspace_read", "arguments": {"path": "fixture.txt"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "workspace_read", "arguments": {"path": "../outside"}}},
            ]
            env = os.environ | {"SYSTEM_AGENTIC_TOOL_WORKSPACE": temp, "SYSTEM_AGENTIC_TOOL_EXECUTION_POLICY": "READ_ONLY"}
            process = subprocess.run(
                [sys.executable, str(ADAPTER)], input="\n".join(json.dumps(item) for item in requests) + "\n",
                text=True, capture_output=True, cwd=temp, env=env, timeout=10, check=False,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        by_id = {row["id"]: row for row in responses}
        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "system-agentic-tool-runtime")
        listed_tools = {tool["name"]: tool for tool in by_id[2]["result"]["tools"]}
        self.assertIn("workspace_exec", listed_tools)
        self.assertIn("run_tests", listed_tools)
        self.assertTrue(listed_tools["workspace_read"]["annotations"]["readOnlyHint"])
        self.assertTrue(listed_tools["workspace_exec"]["annotations"]["readOnlyHint"])
        success = json.loads(by_id[3]["result"]["content"][0]["text"])
        refused = json.loads(by_id[4]["result"]["content"][0]["text"])
        self.assertEqual(success["status"], "COMPLETED")
        self.assertEqual(success["request_id"], 3)
        self.assertEqual(success["adapter"], "mcp")
        self.assertEqual(success["execution_policy"], "READ_ONLY")
        self.assertEqual(success["output"], "MCP fixture\n")
        self.assertTrue(by_id[4]["result"]["isError"])
        self.assertEqual(refused["status"], "REFUSED")

    def test_elevated_server_configuration_fails_before_stdio_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            process = subprocess.run(
                [sys.executable, str(ADAPTER)], input="", text=True, capture_output=True,
                cwd=temp, env=os.environ | {"SYSTEM_AGENTIC_TOOL_WORKSPACE": temp, "SYSTEM_AGENTIC_TOOL_EXECUTION_POLICY": "ELEVATED"},
                timeout=10, check=False,
            )
        self.assertEqual(process.returncode, 2)
        self.assertIn("ELEVATED_REQUIRES_APPROVAL", process.stderr)


if __name__ == "__main__":
    unittest.main()
