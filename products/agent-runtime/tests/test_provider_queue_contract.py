from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import subprocess
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "agent-brain"))
sys.path.insert(0, str(ROOT / "products" / "agent-runtime"))

import agentic_runtime
import local_agent


class ProviderQueueContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._host_role = patch.dict(os.environ, {"SYSTEM_AGENTIC_EXECUTION_ROLE": "interactive-host"})
        self._host_role.start()

    def tearDown(self) -> None:
        self._host_role.stop()

    def test_successful_worker_exit_wins_over_shutdown_flag(self) -> None:
        """A graceful worker result must not be relabeled as FAILED_LOST."""
        self.assertEqual(agentic_runtime._job_terminal_state(0, True), ("completed", "COMPLETED"))
        self.assertEqual(agentic_runtime._job_terminal_state(1, True), ("failed", "FAILED_LOST"))
        self.assertEqual(agentic_runtime._job_terminal_state(1, False), ("failed", "FAILED"))

    def test_workbench_reference_routing_does_not_match_smoke_token_underscores(self) -> None:
        self.assertFalse(local_agent.task_needs_system_knowledge(
            "Local Qwen dispatch smoke",
            "Return exactly SYSTEM_AGENTIC_LOCAL_QWEN_OK.",
        ))
        self.assertFalse(local_agent.task_needs_system_knowledge(
            "Local Qwen dispatch smoke",
            "Execution boundary: do not invoke system-agentic dispatch.",
        ))
        self.assertTrue(local_agent.task_needs_system_knowledge(
            "System-Agentic status diagnosis",
            "Inspect the workbench reference routing.",
        ))

    def test_local_command_output_uses_recoverable_caveman_shrink_and_keeps_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lines = [f"INFO attempt={n} stage=compile path=src/module_{n % 7}.py duration_ms={n + 12}" for n in range(700)]
            lines.append("ERROR security finding path=tests/test_auth.py:88 expected=403 actual=200 severity=critical")
            source = "\n".join(lines) + "\n"
            compact = "… 700 routine lines elided\n" + lines[-1] + "\n"
            trace = {
                "optimization_used": True, "capability": "agent.output-shrink",
                "input_size_bytes": len(source.encode()), "output_size_bytes": len(compact.encode()),
                "reduction_ratio": 1 - len(compact.encode()) / len(source.encode()),
                "latency_ms": 1.0, "fallback_used": False, "recovery_verified": True,
            }
            with patch.object(local_agent, "shutil_which", return_value="/fixture/sandbox-exec"), \
                 patch.object(local_agent.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout=source, stderr="")), \
                 patch.object(local_agent, "optimize_text", return_value=(compact, trace)):
                result = json.loads(local_agent.run_command(root, {"argv": ["fixture-test"]}))
            self.assertEqual(result["exit_code"], 1)
            self.assertTrue(result["optimization"]["optimization_used"])
            self.assertTrue(result["optimization"]["recovery_verified"])
            self.assertIn("tests/test_auth.py:88", result["output"])
            self.assertIn("expected=403 actual=200 severity=critical", result["output"])
            self.assertLess(result["optimization"]["output_size_bytes"], result["optimization"]["input_size_bytes"])

    def test_small_command_output_bypasses_caveman(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(local_agent, "shutil_which", return_value="/fixture/sandbox-exec"), \
                 patch.object(local_agent.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")):
                result = json.loads(local_agent.run_command(root, {"argv": ["fixture-test"]}))
            self.assertEqual(result["output"], "ok\n")
            self.assertFalse(result["optimization"]["optimization_used"])
            self.assertEqual(result["optimization"]["reason"], "below_size_threshold")

    def test_command_output_limit_keeps_json_and_exit_status_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = "routine output\n" * 2_000
            trace = {"optimization_used": False, "capability": "agent.output-shrink", "input_size_bytes": len(source), "output_size_bytes": len(source), "reduction_ratio": 0, "latency_ms": 1, "fallback_used": True}
            with patch.object(local_agent, "shutil_which", return_value="/fixture/sandbox-exec"), \
                 patch.object(local_agent.subprocess, "run", return_value=subprocess.CompletedProcess([], 9, stdout=source, stderr="")), \
                 patch.object(local_agent, "optimize_text", return_value=(source, trace)):
                raw = local_agent.execute_tool(root, "run_command", json.dumps({"argv": ["fixture-test"]}))
            result = json.loads(raw)
            self.assertEqual(result["exit_code"], 9)
            self.assertIn("truncated at the configured limit", result["output"])

    def test_execution_policy_controls_local_mutation_tools_without_changing_local_default(self) -> None:
        self.assertFalse(local_agent.task_mutations_allowed("inspect", "list files", "READ_ONLY"))
        self.assertTrue(local_agent.task_mutations_allowed("fix", "edit the requested file", "WORKSPACE_WRITE"))
        self.assertFalse(local_agent.task_mutations_allowed("inspect", "Do not modify files.", "WORKSPACE_WRITE"))
        self.assertTrue(local_agent.task_mutations_allowed("legacy", "old task", "LEGACY_UNRESTRICTED"))

    def test_local_queue_default_remains_workspace_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            with patch.object(agentic_runtime, "APP_SUPPORT", root / "queue"), contextlib.redirect_stdout(io.StringIO()):
                agentic_runtime.submit(Namespace(
                    workspace=str(workspace), prompt="fix files if needed", file=None,
                    task_id="local-default-policy", title="local default", backend="local",
                ))
                conn = agentic_runtime.database(root / "queue")
                row = conn.execute("SELECT execution_policy FROM jobs WHERE id='local-default-policy'").fetchone()
                self.assertEqual(row[0], "WORKSPACE_WRITE")
                conn.close()

    def test_caller_task_id_is_persisted_and_status_has_one_json_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "one.txt").write_text("one", encoding="utf-8")
            with patch.object(agentic_runtime, "APP_SUPPORT", root / "queue"), \
                 patch.object(agentic_runtime, "discover_codex", return_value="/bin/codex"), \
                 patch.object(agentic_runtime, "codex_login_ready", return_value=True), \
                 patch.object(agentic_runtime, "provider_inventory", return_value={"runtime": {"queue_worker_loaded": True, "codex_home": "/fixture/codex-home"}}), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                agentic_runtime.submit(Namespace(
                    workspace=str(workspace), prompt="list fixture files", file=None,
                    task_id="fixture-task-01", title="fixture", backend="codex",
                    execution_policy="WORKSPACE_WRITE",
                    execution_context=json.dumps({
                        "origin": "interactive-host", "host_role": "orchestrator",
                        "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH",
                        "framing_result": "filesystem execution requested",
                    }),
                ))
                receipt = json.loads(output.getvalue())
                conn = agentic_runtime.database(root / "queue")
                row = conn.execute("SELECT id,status,backend,result_summary,execution_policy,execution_context_json FROM jobs WHERE id=?", (receipt["id"],)).fetchone()
                self.assertEqual(tuple(row[:5]), ("fixture-task-01", "queued", "codex", None, "WORKSPACE_WRITE"))
                self.assertEqual(json.loads(row["execution_context_json"])["host_role"], "orchestrator")

                conn.execute("UPDATE jobs SET status='completed',result_summary='one.txt' WHERE id=?", (receipt["id"],))
                conn.commit()
                status_output = io.StringIO()
                with contextlib.redirect_stdout(status_output):
                    agentic_runtime.show_jobs(Namespace(id=receipt["id"], json_output=True))
                status = json.loads(status_output.getvalue())
                self.assertEqual(status["id"], "fixture-task-01")
                self.assertEqual(status["result_summary"], "one.txt")
                self.assertEqual(status["status"], "completed")
                self.assertEqual(status["execution_policy"], "WORKSPACE_WRITE")
                self.assertEqual(status["execution_context"]["dispatch_depth"], 1)
                conn.close()

    def test_elevated_policy_is_refused_before_database_or_queue_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            queue = root / "queue"
            with patch.object(agentic_runtime, "APP_SUPPORT", queue):
                with self.assertRaisesRegex(ValueError, "ELEVATED_REQUIRES_APPROVAL"):
                    agentic_runtime.submit(Namespace(
                        workspace=str(workspace), prompt="perform an external write", file=None,
                        task_id="elevated-task", title="elevated", backend="codex",
                        execution_policy="ELEVATED", execution_context="{}",
                    ))
            self.assertFalse(queue.exists())

    def test_worker_environment_cannot_bypass_provider_layer_via_runtime_submit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            queue = root / "queue"
            with patch.object(agentic_runtime, "APP_SUPPORT", queue), patch.dict(os.environ, {"SYSTEM_AGENTIC_EXECUTION_ROLE": "worker"}):
                with self.assertRaisesRegex(ValueError, "RECURSION_GUARD"):
                    agentic_runtime.submit(Namespace(
                        workspace=str(workspace), prompt="child task", file=None,
                        task_id="worker-child", title="child", backend="codex",
                        execution_policy="READ_ONLY", execution_context="{}",
                    ))
            self.assertFalse(queue.exists())

    def test_codex_process_uses_declared_sandbox_and_sets_worker_context(self) -> None:
        class CompletedProcess:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO()

            def poll(self):
                return 0

            def wait(self):
                return 0

        for policy, sandbox in (("READ_ONLY", "read-only"), ("WORKSPACE_WRITE", "workspace-write")):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                workspace = root / "workspace"
                workspace.mkdir()
                queue = root / "queue"
                with patch.object(agentic_runtime, "APP_SUPPORT", queue):
                    conn = agentic_runtime.database(queue)
                    conn.execute(
                        "INSERT INTO jobs(id,title,workspace,prompt,status,created_at,updated_at,backend,execution_policy,execution_context_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        ("sandbox-task", "sandbox", str(workspace), "inspect fixture", "running", agentic_runtime.now(), agentic_runtime.now(), "codex", policy, json.dumps({"host_role": "orchestrator", "dispatch_depth": 1})),
                    )
                    conn.commit()
                    row = conn.execute("SELECT * FROM jobs WHERE id='sandbox-task'").fetchone()
                    process = CompletedProcess()
                    with patch.object(agentic_runtime.subprocess, "Popen", return_value=process) as popen:
                        agentic_runtime.process_job(conn, row, "/bin/codex")
                    command = popen.call_args.args[0]
                    self.assertEqual(command[command.index("--sandbox") + 1], sandbox)
                    env = popen.call_args.kwargs["env"]
                    self.assertEqual(env["SYSTEM_AGENTIC_EXECUTION_ROLE"], "worker")
                    self.assertEqual(env["SYSTEM_AGENTIC_DISPATCH_DEPTH"], "1")
                    conn.close()

    def test_legacy_codex_task_cannot_be_resumed_without_verified_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            queue = root / "queue"
            with patch.object(agentic_runtime, "APP_SUPPORT", queue):
                conn = agentic_runtime.database(queue)
                stamp = agentic_runtime.now()
                conn.execute(
                    "INSERT INTO jobs(id,title,workspace,prompt,status,created_at,updated_at,backend,last_session_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("legacy-task", "legacy", str(root), "old prompt", "failed", stamp, stamp, "codex", "legacy-session"),
                )
                conn.commit()
                with self.assertRaisesRegex(ValueError, "sandbox policy được xác minh"):
                    agentic_runtime.resume(Namespace(id="legacy-task", new_session=False))
                self.assertEqual(conn.execute("SELECT status,execution_policy FROM jobs WHERE id='legacy-task'").fetchone()[:], ("failed", "LEGACY_UNRESTRICTED"))
                conn.close()

    def test_invalid_task_id_is_rejected_before_queue_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            with patch.object(agentic_runtime, "APP_SUPPORT", root / "queue"):
                with self.assertRaisesRegex(ValueError, "Task ID"):
                    agentic_runtime.submit(Namespace(
                        workspace=str(workspace), prompt="inspect", file=None,
                        task_id="../../bad", title="fixture", backend="local",
                        execution_policy="READ_ONLY", execution_context="{}",
                    ))
                conn = agentic_runtime.database(root / "queue")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
                conn.close()


if __name__ == "__main__":
    unittest.main()
