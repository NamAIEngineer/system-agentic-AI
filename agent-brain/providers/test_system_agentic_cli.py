from __future__ import annotations

import contextlib
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
LOADER = SourceFileLoader("system_agentic_cli", str(ROOT / "scripts" / "system-agentic"))
SPEC = importlib.util.spec_from_loader("system_agentic_cli", LOADER)
assert SPEC
system_agentic_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(system_agentic_cli)


class SystemAgenticHostDispatchTests(unittest.TestCase):
    def test_task_spec_reuses_existing_contract_with_policy_and_host_context(self) -> None:
        task = system_agentic_cli.task_from_spec({
            "workspace": str(ROOT),
            "objective": "Inspect files.",
            "role": "worker",
            "required_capabilities": ["task.submit"],
            "execution_policy": "READ_ONLY",
            "execution_context": {
                "origin": "interactive-host", "host_role": "orchestrator", "dispatch_depth": 1,
                "parent_task_id": None, "direct_or_dispatch": "DISPATCH", "framing_result": "filesystem request",
            },
        })
        self.assertEqual(task.execution_policy, "READ_ONLY")
        self.assertEqual(task.execution_context["host_role"], "orchestrator")
        with self.assertRaisesRegex(ValueError, "depth 1"):
            system_agentic_cli.task_from_spec({
                "workspace": str(ROOT), "objective": "recursive", "role": "worker",
                "execution_context": {"host_role": "orchestrator", "dispatch_depth": 2, "direct_or_dispatch": "DISPATCH"},
            })

    def test_worker_environment_refuses_recursion_before_reading_task_spec(self) -> None:
        output = io.StringIO()
        with patch.dict(os.environ, {"SYSTEM_AGENTIC_EXECUTION_ROLE": "worker"}), contextlib.redirect_stdout(output):
            code = system_agentic_cli.dispatch(Namespace(task_spec="/does/not/exist.json", wait_seconds=0))
        result = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "REFUSED")
        self.assertIn("RECURSION_GUARD", result["reason"])
        self.assertFalse(result["dispatched"])

    def test_elevated_spec_is_refused_before_provider_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            spec_path = Path(temp) / "task.json"
            spec_path.write_text(json.dumps({
                "task_id": "elevated-test",
                "workspace": temp,
                "objective": "external write",
                "role": "worker",
                "execution_policy": "ELEVATED",
                "execution_context": {"host_role": "orchestrator", "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH"},
            }), encoding="utf-8")
            output = io.StringIO()
            with patch.dict(os.environ, {"SYSTEM_AGENTIC_EXECUTION_ROLE": "orchestrator"}), \
                 patch("providers.dispatch.select_provider") as select, contextlib.redirect_stdout(output):
                code = system_agentic_cli.dispatch(Namespace(task_spec=str(spec_path), wait_seconds=0))
        result = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "REFUSED")
        self.assertEqual(result["execution_policy"], "ELEVATED")
        select.assert_not_called()

    def test_status_wait_polls_the_same_id_without_execute(self) -> None:
        from providers.runtime import ProviderResult

        class StatusOnlyAdapter:
            def __init__(self):
                self.calls = []

            def status(self, task):
                self.calls.append(task.task_id)
                state = "RUNNING" if len(self.calls) == 1 else "COMPLETED"
                return ProviderResult(
                    task_id=task.task_id, provider_id="codex", account_id="fixture-account",
                    status=state, summary="files listed" if state == "COMPLETED" else None,
                    artifacts=(), evidence=(), errors=(), started_at=None, finished_at=None,
                    queue_job_id=task.task_id, role="worker", required_capabilities=("task.submit",),
                    selection_reason="status reconciliation",
                )

        adapter = StatusOnlyAdapter()
        output = io.StringIO()
        with patch.object(system_agentic_cli, "adapter_for", return_value=adapter), \
             patch.object(system_agentic_cli.time, "sleep"), contextlib.redirect_stdout(output):
            code = system_agentic_cli.task_status(Namespace(task_id="same-task", provider="codex", wait_seconds=5))
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(adapter.calls, ["same-task", "same-task"])
        self.assertEqual(result["status"], "COMPLETED")
        self.assertFalse(result["wait_timed_out"])

    def test_module_dispatch_hands_structured_task_to_provider_selection_without_live_execution(self) -> None:
        from providers.runtime import ProviderSelection

        class QueueBoundary:
            provider_id = "codex"

            def execute(self, task, *, wait_timeout_seconds=600):
                self.task = task
                raise RuntimeError("test stopped before queue execution")

        with tempfile.TemporaryDirectory() as temp:
            spec_path = Path(temp) / "task.json"
            spec_path.write_text(json.dumps({
                "task_id": "gstack-route-test",
                "workspace": temp,
                "objective": "Review this fixture for defects.",
                "role": "worker",
                "required_capabilities": ["task.submit"],
                "execution_policy": "READ_ONLY",
                "execution_context": {
                    "origin": "interactive-host", "host_role": "orchestrator", "dispatch_depth": 1,
                    "parent_task_id": None, "direct_or_dispatch": "DISPATCH", "framing_result": "review request",
                },
            }), encoding="utf-8")
            adapter = QueueBoundary()
            selection = ProviderSelection("codex", None, "module requires codex", ("codex",))
            output = io.StringIO()
            with patch.dict(os.environ, {"SYSTEM_AGENTIC_EXECUTION_ROLE": "orchestrator"}), \
                 patch("providers.dispatch.select_provider", return_value=(adapter, selection)) as select, contextlib.redirect_stdout(output):
                code = system_agentic_cli.dispatch(
                    Namespace(task_spec=str(spec_path), wait_seconds=0),
                    module_id="gstack",
                    module_capability="software.code-review",
                )

        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "FAILED")
        select.assert_called_once()
        routed_task = select.call_args.args[0]
        self.assertEqual(routed_task.provider_policy["allowed_providers"], ["codex"])
        self.assertEqual(routed_task.execution_context["module"]["id"], "gstack")
        self.assertEqual(adapter.task.context["module"]["invocation"]["skill_id"], "review")
        self.assertEqual(adapter.task.context["module"]["invocation"]["command"], "/review")
        self.assertNotEqual(result["status"], "COMPLETED")

    def test_pedagogy_dispatch_is_local_and_does_not_require_a_provider(self) -> None:
        from modules.runtime import ModuleResult

        with tempfile.TemporaryDirectory() as temp:
            spec_path = Path(temp) / "pedagogy-task.json"
            spec_path.write_text(json.dumps({
                "task_id": "pedagogy-local-route-test",
                "workspace": temp,
                "objective": "I want to practise explaining fractions.",
                "role": "worker",
                "required_capabilities": ["task.submit"],
                "execution_policy": "WORKSPACE_WRITE",
                "execution_context": {
                    "origin": "interactive-host", "host_role": "orchestrator", "dispatch_depth": 1,
                    "parent_task_id": None, "direct_or_dispatch": "DISPATCH", "framing_result": "teaching rehearsal",
                },
                "context": {"pedagogy_request": {"action": "create", "case_id": "fraction-magnitude"}},
            }), encoding="utf-8")
            output = io.StringIO()
            with patch.dict(os.environ, {"SYSTEM_AGENTIC_EXECUTION_ROLE": "orchestrator"}), \
                 patch("providers.dispatch.select_provider", side_effect=AssertionError("Pedagogy is deterministic local execution")) as select, \
                 patch("providers.dispatch.execute_pedagogy_module", return_value=ModuleResult(
                     "pedagogy-local-route-test", "pedagogy", "COMPLETED", "pedagogy.rehearse", "IN_PROGRESS",
                     {"runtime": {"endpoint": "http://127.0.0.1:12345", "managed": True, "pid": 2345}},
                 )), contextlib.redirect_stdout(output):
                code = system_agentic_cli.dispatch(
                    Namespace(task_spec=str(spec_path), wait_seconds=0),
                    module_id="pedagogy", module_capability="pedagogy.rehearse",
                )
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["provider"], None)
        self.assertEqual(result["status"], "COMPLETED")
        workflow = result["execution_context"]["module"]["pedagogy_workflow"]
        self.assertEqual(workflow["service_url"], "http://127.0.0.1:12345")
        self.assertTrue(workflow["runtime"]["managed"])
        select.assert_not_called()


if __name__ == "__main__":
    unittest.main()
