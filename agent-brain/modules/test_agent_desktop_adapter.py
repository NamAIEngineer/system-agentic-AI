from __future__ import annotations

import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from providers.runtime import ProviderTask
from modules.agent_desktop_adapter import (
    AgentDesktopError,
    AgentDesktopMcpSession,
    agent_desktop_health,
    execute,
)
from modules.runtime import load_module_registry, module_candidates, module_inventory


FAKE_SERVER = r'''#!/usr/bin/env python3
import json, sys
tools = [
 {"name":"desktop_snapshot","inputSchema":{"type":"object"}},
 {"name":"desktop_find","inputSchema":{"type":"object"}},
 {"name":"skills_get","inputSchema":{"type":"object"}},
 {"name":"desktop_version","inputSchema":{"type":"object"}},
 {"name":"desktop_status","inputSchema":{"type":"object"}},
 {"name":"desktop_click","inputSchema":{"type":"object"}},
 {"name":"desktop_set_value","inputSchema":{"type":"object"}},
 {"name":"trace_export","inputSchema":{"type":"object"}},
]
def read():
 line=sys.stdin.buffer.readline()
 if not line: raise EOFError()
 return json.loads(line)
def send(x):
 b=json.dumps(x,separators=(',',':')).encode(); sys.stdout.buffer.write(b+b'\n'); sys.stdout.buffer.flush()
while True:
 if len(sys.argv)>1 and sys.argv[1]=='version':
  print(json.dumps({'version':'1.2.0','ok':True})); break
 try: q=read()
 except Exception: break
 if 'id' not in q: continue
 method=q.get('method'); p=q.get('params',{})
 if method=='initialize': result={"protocolVersion":"2024-11-05","serverInfo":{"name":"agent-desktop","version":"1.2.0"}}
 elif method=='tools/list': result={"tools":tools}
 elif method=='tools/call':
  name=p.get('name')
  if name=='skills_get': result={"content":[{"type":"text","text":"skill"}],"structuredContent":{"name":p.get('arguments',{}).get('name'),"complete":True}}
  elif name in ('desktop_version','desktop_status'): result={"structuredContent":{"complete":True,"version":"1.2.0","status":"READY"}}
  elif name=='desktop_snapshot':
   if p.get('arguments',{}).get('app')=='MissingApp':
    result={"isError":True,"resultType":"complete","structuredContent":{"ok":False,"error":{"code":"WINDOW_NOT_FOUND","message":"missing","disposition":{"delivery":"not_delivered","retry":"safe"}}}}
   else:
    result={"structuredContent":{"complete":True,"refs":[{"ref_id":"node-1"}],"data":{"tree":{"role":"window","children":[{"role":"button","name":"1","native_id":{"kind":"ax_identifier","value":"One"},"value":"1" if globals().get('clicked',False) else "","ref_id":"node-1"}]}}}}
  elif name=='desktop_click':
   clicked=True
   result={"structuredContent":{"complete":True,"delivery":"background"}}
  elif name=='desktop_set_value': result={"structuredContent":{"complete":True,"delivery":"background","value":p.get('arguments',{}).get('value')}}
  elif name=='trace_export': result={"structuredContent":{"complete":True}}
  else: result={"structuredContent":{"complete":True}}
 else: result={}
 send({"jsonrpc":"2.0","id":q['id'],"result":result})
'''


class AgentDesktopAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake = self.root / "agent-desktop"
        self.fake.write_text(FAKE_SERVER, encoding="utf-8")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def task(self, workspace: Path, *, actions=None, policy="READ_ONLY", auth=None) -> ProviderTask:
        desktop = {"executable": str(self.fake), "actions": actions or [], "timeout_ms": 3000}
        if auth is not None:
            desktop["action_authorization"] = auth
        return ProviderTask(workspace, "Observe native desktop", execution_policy=policy, context={"desktop": desktop})

    def test_real_mcp_framing_negotiates_tools_and_cleans_up(self) -> None:
        with AgentDesktopMcpSession([str(self.fake), "mcp", "--session", "test"], cwd=self.root, start_timeout=3) as session:
            self.assertIn("desktop_snapshot", session.tools)
            self.assertEqual(session.call("desktop_snapshot", {}, 2)["structuredContent"]["complete"], True)
        self.assertIsNotNone(session.process.poll())

    def test_two_projects_share_one_adapter_without_copying_implementation(self) -> None:
        first, second = self.root / "project-a", self.root / "project-b"
        first.mkdir(); second.mkdir()
        for workspace in (first, second):
            result = execute(self.task(workspace))
            self.assertEqual(result["execution_status"], "COMPLETED")
            self.assertEqual(result["module"], "agent-desktop")

    def test_action_requires_write_policy_and_explicit_authorization(self) -> None:
        workspace = self.root / "project"; workspace.mkdir()
        action = {"tool": "desktop_click", "arguments": {"ref_id": "node-1"}, "verify": {"expect": {"structuredContent": {"complete": True}}}}
        with self.assertRaisesRegex(AgentDesktopError, "WORKSPACE_WRITE"):
            execute(self.task(workspace, actions=[action]))
        result = execute(self.task(workspace, actions=[action], policy="WORKSPACE_WRITE", auth="explicit-task"))
        self.assertEqual(result["execution_status"], "COMPLETED")
        self.assertEqual(result["domain_outcome"], "VERIFIED")

    def test_missing_ref_is_stale_and_never_blind_retried(self) -> None:
        workspace = self.root / "project"; workspace.mkdir()
        action = {"tool": "desktop_click", "arguments": {"ref_id": "not-in-snapshot"}}
        result = execute(self.task(workspace, actions=[action], policy="WORKSPACE_WRITE", auth="explicit-task"))
        self.assertEqual(result["execution_status"], "BLOCKED")
        self.assertEqual(result["errors"][0]["code"], "STALE_REF")
        self.assertEqual(len([x for x in result["evidence"] if x.get("type") == "action"]), 0)

    def test_unique_selector_resolves_ref_from_latest_snapshot(self) -> None:
        workspace = self.root / "project"; workspace.mkdir()
        action = {"tool": "desktop_click", "selector": {"name": "1", "role": "button",
                  "native_id": {"kind": "ax_identifier", "value": "One"}},
                  "verify": {"expect": {"structuredContent": {"complete": True}}}}
        result = execute(self.task(workspace, actions=[action], policy="WORKSPACE_WRITE", auth="explicit-task"))
        self.assertEqual(result["execution_status"], "COMPLETED")
        resolutions = [x for x in result["evidence"] if x.get("type") == "selector_resolution"]
        self.assertEqual(resolutions[0]["ref_id"], "node-1")
        self.assertEqual([x for x in result["evidence"] if x.get("type") == "action"][0]["result"]["data"]["complete"], True)

    def test_expect_change_requires_semantic_readback(self) -> None:
        workspace = self.root / "project"; workspace.mkdir()
        action = {"tool": "desktop_click", "selector": {"name": "1", "role": "button"},
                  "verify": {"expect_change": True, "expect": {"structuredContent": {"complete": True}}}}
        result = execute(self.task(workspace, actions=[action], policy="WORKSPACE_WRITE", auth="explicit-task"))
        self.assertEqual(result["execution_status"], "COMPLETED")
        assertion = [x for x in result["evidence"] if x.get("type") == "assertion"][0]
        self.assertTrue(assertion["passed"])
        self.assertTrue(assertion["expect_change"])

    def test_selector_without_match_blocks_before_action(self) -> None:
        workspace = self.root / "project"; workspace.mkdir()
        action = {"tool": "desktop_click", "selector": {"name": "missing", "role": "button"}}
        result = execute(self.task(workspace, actions=[action], policy="WORKSPACE_WRITE", auth="explicit-task"))
        self.assertEqual(result["execution_status"], "BLOCKED")
        self.assertEqual(result["errors"][0]["code"], "AMBIGUOUS_TARGET")
        self.assertEqual(len([x for x in result["evidence"] if x.get("type") == "action"]), 0)

    def test_ambiguous_selector_blocks_without_retry(self) -> None:
        workspace = self.root / "project"; workspace.mkdir()
        class AmbiguousSession(AgentDesktopMcpSession):
            def call(self, tool, arguments, timeout):
                result = super().call(tool, arguments, timeout)
                if tool == "desktop_snapshot":
                    tree = result["structuredContent"]["data"]["tree"]
                    tree["children"].append({"role": "button", "name": "1", "native_id": {"kind": "ax_identifier", "value": "One-2"}, "ref_id": "node-2"})
                return result
        action = {"tool": "desktop_click", "selector": {"name": "1", "role": "button"}}
        result = execute(self.task(workspace, actions=[action], policy="WORKSPACE_WRITE", auth="explicit-task"), session_factory=AmbiguousSession)
        self.assertEqual(result["execution_status"], "BLOCKED")
        self.assertEqual(result["errors"][0]["code"], "AMBIGUOUS_TARGET")
        self.assertEqual(len([x for x in result["evidence"] if x.get("type") == "action"]), 0)

    def test_tool_error_preserves_upstream_envelope_and_disposition(self) -> None:
        workspace = self.root / "project"; workspace.mkdir()
        task = self.task(workspace)
        task = __import__("dataclasses").replace(task, context={**task.context, "desktop": {**task.context["desktop"], "target": {"app": "MissingApp"}}})
        result = execute(task)
        self.assertEqual(result["execution_status"], "FAILED")
        self.assertEqual(result["errors"][0]["code"], "WINDOW_NOT_FOUND")
        self.assertEqual(result["errors"][0]["error"]["disposition"]["retry"], "safe")
        self.assertFalse(result["errors"][0]["ok"])

    def test_module_dispatch_calls_same_adapter_with_project_workspace(self) -> None:
        from dataclasses import replace
        from providers.dispatch import dispatch_task, task_from_spec
        from modules.runtime import prepare_module_task
        workspace = self.root / "project"; workspace.mkdir()
        task = self.task(workspace)
        prepared = prepare_module_task(task, "agent-desktop", "desktop.automation")
        prepared = replace(prepared, execution_context={"host_role": "orchestrator", "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH"})
        result = dispatch_task(prepared, module_id="agent-desktop", module_capability="desktop.automation")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["execution_context"].get("workspace"), None)
        spec_task = task_from_spec({"workspace": str(workspace), "objective": "Observe", "targetModuleId": "agent-desktop",
                                    "requestedCapability": "desktop.automation", "context": {"desktop": task.context["desktop"]},
                                    "execution_context": {"origin": "test", "host_role": "orchestrator", "dispatch_depth": 1,
                                                           "direct_or_dispatch": "DISPATCH"}})
        self.assertEqual(spec_task.workspace.resolve(), workspace.resolve())

    def test_cancellation_is_terminal_without_starting_actions(self) -> None:
        workspace = self.root / "project"; workspace.mkdir()
        task = self.task(workspace)
        task = __import__("dataclasses").replace(task, context={**task.context, "cancelled": True})
        self.assertEqual(execute(task)["execution_status"], "CANCELLED")

    def test_health_and_registry_report_absent_binary_without_breaking_other_modules(self) -> None:
        health = agent_desktop_health({"executable": str(self.root / "missing")})
        self.assertEqual(health["status"], "UNAVAILABLE")
        ids = {item["id"] for item in load_module_registry()["modules"]}
        self.assertIn("agent-desktop", ids)
        candidates = module_candidates(capabilities={"desktop.automation"})
        self.assertEqual([item["id"] for item in candidates], ["agent-desktop"])
        inventory = {item["id"]: item for item in module_inventory()}
        self.assertEqual(inventory["agent-desktop"]["execution_ready"], inventory["agent-desktop"]["status"] == "READY")


if __name__ == "__main__":
    unittest.main()
