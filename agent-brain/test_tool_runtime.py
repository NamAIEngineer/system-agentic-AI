from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-brain"))
sys.path.insert(0, str(ROOT / "products" / "agent-runtime"))
from tool_runtime import ToolRuntime, ToolRuntimeError  # noqa: E402
import tool_executor  # noqa: E402


class FakeWorkerBackend:
    def __init__(self, result=None, error=None):
        self.result = result or {"execution_status": "COMPLETED", "exit_code": 0, "stdout": "ok\n", "stderr": ""}
        self.error = error
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.result


class ToolRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "sample.txt").write_text("alpha\ncritical finding: sample.py:9\n", encoding="utf-8")
        (self.root / ".env").write_text("fixture_secret=placeholder-value\n", encoding="utf-8")
        self.runtime = ToolRuntime(self.root, "READ_ONLY")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_elevated_fails_closed_and_unknown_policy_is_rejected(self) -> None:
        with self.assertRaisesRegex(ToolRuntimeError, "ELEVATED_REQUIRES_APPROVAL"):
            ToolRuntime(self.root, "ELEVATED")
        with self.assertRaisesRegex(ToolRuntimeError, "execution_policy"):
            ToolRuntime(self.root, "LEGACY")

    def test_schema_validation_and_tool_identity_are_normalized(self) -> None:
        read_only_tools = {tool["name"]: tool for tool in self.runtime.list_tools()}
        self.assertTrue(read_only_tools["workspace_exec"]["annotations"]["readOnlyHint"])
        missing = self.runtime.call("workspace_read", {})
        extra = self.runtime.call("workspace_read", {"path": "src/sample.txt", "raw": True})
        result = self.runtime.call("workspace_read", {"path": "src/sample.txt"})
        self.assertEqual(missing["status"], "REFUSED")
        self.assertEqual(extra["status"], "REFUSED")
        self.assertEqual(result["tool"], "workspace_read")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["execution_status"], "COMPLETED")
        self.assertEqual(result["execution_policy"], "READ_ONLY")
        self.assertEqual(result["input_bytes"], result["raw_size_bytes"])
        self.assertEqual(result["adapter"], "local")
        self.assertIn("critical finding", result["output"])

    def test_workspace_boundary_symlinks_and_secret_files_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (self.root / "escape.txt").symlink_to(outside)
            for path in ("../outside", "escape.txt", ".env", ".git/config"):
                result = self.runtime.call("workspace_read", {"path": path})
                self.assertEqual(result["status"], "REFUSED", path)

    def test_search_is_bounded_workspace_relative_and_returns_line_evidence(self) -> None:
        result = self.runtime.call("workspace_search", {"query": "critical"})
        payload = json.loads(result["output"])
        self.assertEqual(payload["matches"][0]["path"], "src/sample.txt")
        self.assertEqual(payload["matches"][0]["line"], 2)
        self.assertNotIn(".env", result["output"])

    def test_workspace_list_candidate_budget_is_reported_as_truncated(self) -> None:
        for index in range(6):
            (self.root / f"item-{index}.txt").write_text("item", encoding="utf-8")
        with patch("tool_runtime.MAX_TRAVERSAL_FILES", 3), patch("tool_runtime.MAX_TRAVERSAL_SECONDS", 60):
            result = self.runtime.call("workspace_list", {})
        payload = json.loads(result["output"])
        self.assertEqual(payload["visited_files"], 3)
        self.assertLessEqual(len(payload["files"]), 3)
        self.assertTrue(payload["truncated"])

    def test_workspace_search_candidate_budget_is_reported_as_truncated(self) -> None:
        for index in range(6):
            (self.root / f"item-{index}.txt").write_text("no match", encoding="utf-8")
        with patch("tool_runtime.MAX_TRAVERSAL_FILES", 3), patch("tool_runtime.MAX_TRAVERSAL_SECONDS", 60):
            result = self.runtime.call("workspace_search", {"query": "absent"})
        payload = json.loads(result["output"])
        self.assertEqual(payload["visited_files"], 3)
        self.assertEqual(payload["matches"], [])
        self.assertTrue(payload["truncated"])

    def test_workspace_traversal_time_budget_is_reported_as_truncated(self) -> None:
        with patch("tool_runtime.MAX_TRAVERSAL_SECONDS", 0):
            listed = self.runtime.call("workspace_list", {})
            searched = self.runtime.call("workspace_search", {"query": "critical"})
        self.assertTrue(json.loads(listed["output"])["truncated"])
        self.assertTrue(json.loads(searched["output"])["truncated"])

    def test_inline_file_operations_and_worker_command_routing(self) -> None:
        backend = FakeWorkerBackend()
        runtime = ToolRuntime(self.root, "READ_ONLY", worker_backend=backend)
        read = runtime.call("workspace_read", {"path": "src/sample.txt"})
        self.assertEqual(read["status"], "COMPLETED")
        self.assertEqual(backend.requests, [])
        result = runtime.call("workspace_exec", {"argv": ["python3", "-c", "print('ok')"]})
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(backend.requests[0]["execution_policy"], "READ_ONLY")
        self.assertEqual(backend.requests[0]["workspace"], str(self.runtime.workspace))
        self.assertEqual(result["execution_status"], "COMPLETED")
        self.assertEqual(result["command_outcome"], "PASSED")
        writer_tools = {item["name"]: item for item in ToolRuntime(self.root, "WORKSPACE_WRITE").list_tools()}
        self.assertNotIn("annotations", writer_tools["workspace_exec"])
        self.assertNotIn("annotations", writer_tools["run_tests"])

    def test_raw_streams_are_optimized_only_after_backend_returns(self) -> None:
        backend = FakeWorkerBackend({"execution_status": "COMPLETED", "exit_code": 1,
                                     "stdout": "stdout evidence", "stderr": "stderr detail"})
        runtime = ToolRuntime(self.root, worker_backend=backend)
        observed = {}

        def optimize(raw, **kwargs):
            observed["raw"] = raw
            return "compact output", {"optimization_used": False, "fallback_used": False}

        with patch("tool_runtime.optimize_text", side_effect=optimize):
            result = runtime.call("workspace_exec", {"argv": ["python3"]})
        self.assertEqual(observed["raw"], "stdout evidence\n[stderr]\nstderr detail")
        self.assertEqual(result["output"], "compact output")
        self.assertNotIn("stdout", result)

    def test_command_and_test_outcomes_are_separate_from_execution_health(self) -> None:
        backend = FakeWorkerBackend({"execution_status": "COMPLETED", "exit_code": 7,
                                     "stdout": "failed assertion\n", "stderr": "",
                                     "test_outcome": "FAILED"})
        runtime = ToolRuntime(self.root, worker_backend=backend)
        command = runtime.call("workspace_exec", {"argv": ["python3", "-m", "unittest"]})
        tests = runtime.call("run_tests", {"argv": ["python3", "-m", "unittest"]})
        self.assertEqual(command["execution_status"], "COMPLETED")
        self.assertEqual(command["command_outcome"], "FAILED")
        self.assertEqual(tests["execution_status"], "COMPLETED")
        self.assertEqual(tests["test_outcome"], "FAILED")

    def test_execution_backend_metadata_is_preserved_in_normalized_result(self) -> None:
        runtime = ToolRuntime(
            self.root,
            "READ_ONLY",
            worker_backend=FakeWorkerBackend({
                "execution_status": "COMPLETED", "exit_code": 0,
                "stdout": "passed\n", "stderr": "", "test_outcome": "PASSED",
                "backend": "sqlite-launchagent-seatbelt", "queue_job_id": "tool-job-1",
                "workspace": str(self.root), "execution_policy": "READ_ONLY", "duration_ms": 12.5,
                "warnings": [],
            }),
        )
        result = runtime.call("run_tests", {"argv": ["python3", "-m", "unittest"]})
        self.assertEqual(result["backend"], "sqlite-launchagent-seatbelt")
        self.assertEqual(result["queue_job_id"], "tool-job-1")
        self.assertEqual(result["execution_duration_ms"], 12.5)
        self.assertEqual(result["execution_policy"], "READ_ONLY")
        self.assertEqual(result["test_outcome"], "PASSED")

    def test_unavailable_worker_never_falls_back_to_local_execution(self) -> None:
        result = self.runtime.call("workspace_exec", {"argv": ["python3", "-c", "print('unsafe')"]})
        self.assertEqual(result["execution_status"], "FAILED")
        self.assertIn("refusing inline execution", result["error"])

    def test_worker_failure_and_timeout_are_closed(self) -> None:
        for error, expected in ((RuntimeError("worker is busy"), "FAILED"), (TimeoutError("wait expired"), "TIMED_OUT")):
            runtime = ToolRuntime(self.root, worker_backend=FakeWorkerBackend(error=error))
            result = runtime.call("workspace_exec", {"argv": ["python3"]})
            self.assertEqual(result["execution_status"], expected)

    def test_strict_executor_profiles_and_sandbox_failure_semantics(self) -> None:
        class FakeProcess:
            stdout = io.BytesIO(b"stdout evidence\n")
            stderr = io.BytesIO(b"sandbox_apply: Operation not permitted\n")
            returncode = 71
            pid = 99

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        def popen(argv, **kwargs):
            self.assertEqual(argv[0:2], ["/usr/bin/sandbox-exec", "-p"])
            profile = argv[2]
            write_rule = next(line for line in profile.splitlines() if line.startswith("(allow file-write*"))
            self.assertIn("(deny network-outbound)", profile)
            self.assertIn("(deny network-inbound)", profile)
            self.assertIn('(allow sysctl-read (sysctl-name "kern.bootargs" "security.mac.lockdown_mode_state"))', profile)
            self.assertIn('(allow file-read* (literal "/"))', profile)
            self.assertIn("(allow file-map-executable", profile)
            dyld_cache = Path("/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld")
            if dyld_cache.exists():
                self.assertIn(f'(subpath "{dyld_cache.resolve()}")', next(
                    line for line in profile.splitlines() if line.startswith("(allow file-read*")
                ))
            if kwargs["env"]["SYSTEM_AGENTIC_EXECUTION_POLICY"] == "READ_ONLY":
                self.assertNotIn(f'(subpath "{self.root.resolve()}")', write_rule)
            else:
                self.assertIn(f'(subpath {json.dumps(str(self.root.resolve()))})', write_rule)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            return FakeProcess()

        with patch("tool_executor.shutil.which", side_effect=lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else "/usr/bin/python3"), patch("tool_executor.subprocess.Popen", side_effect=popen):
            result = tool_executor.execute_command({"tool": "run_tests", "workspace": str(self.root),
                "execution_policy": "READ_ONLY", "arguments": {"argv": ["python3", "-m", "unittest"]}})
            writer = tool_executor.execute_command({"tool": "workspace_exec", "workspace": str(self.root),
                "execution_policy": "WORKSPACE_WRITE", "arguments": {"argv": ["python3", "-c", "pass"]}})
        self.assertEqual(result["execution_status"], "COMPLETED")
        self.assertEqual(result["exit_code"], 71)
        self.assertEqual(result["test_outcome"], "FAILED")
        self.assertIn("Operation not permitted", result["stderr"])
        self.assertEqual(writer["execution_status"], "COMPLETED")

    def test_strict_executor_refuses_missing_sandbox_and_caps_output(self) -> None:
        request = {"tool": "workspace_exec", "workspace": str(self.root), "execution_policy": "READ_ONLY",
                   "arguments": {"argv": ["python3"]}}
        with patch("tool_executor.shutil.which", side_effect=lambda name: None if name == "sandbox-exec" else "/usr/bin/python3"):
            with self.assertRaisesRegex(RuntimeError, "refusing unsandboxed execution"):
                tool_executor.execute_command(request)
        with patch("tool_executor.MAX_OUTPUT_BYTES", 100):
            captured, exceeded, total = bytearray(), [False], [0]
            tool_executor._capture(io.BytesIO(b"x" * 200), captured, exceeded, total, __import__("threading").Lock())
        self.assertLessEqual(len(captured) + len(tool_executor.TRUNCATION_MARKER.encode()), 100)
        self.assertTrue(exceeded[0])

    def test_signal_terminated_process_is_execution_failure(self) -> None:
        class SignalProcess:
            stdout = io.BytesIO(b"")
            stderr = io.BytesIO(b"")
            returncode = -6
            pid = 123

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        with patch("tool_executor.shutil.which", side_effect=lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else "/usr/bin/python3"), \
             patch("tool_executor.subprocess.Popen", return_value=SignalProcess()):
            result = tool_executor.execute_command({
                "tool": "run_tests", "workspace": str(self.root), "execution_policy": "READ_ONLY",
                "arguments": {"argv": ["python3", "-m", "unittest"]},
            })
        self.assertEqual(result["execution_status"], "FAILED")
        self.assertEqual(result["exit_code"], -6)
        self.assertEqual(result["test_outcome"], "FAILED")
        self.assertIn("signal 6", result["error"])

    def test_large_compaction_exposes_process_local_exact_recovery_reference(self) -> None:
        original = "ERROR critical finding sample.py:9\n" + "routine output\n" * 800
        handle = "fixture_handle_123"
        trace = {"optimization_used": True, "recovery_verified": True, "recovery_handle": handle,
                 "input_size_bytes": len(original.encode()), "output_size_bytes": 48}
        with patch("tool_runtime.optimize_text", return_value=("compact finding sample.py:9", trace)):
            result = self.runtime._normalized("workspace_exec", "FAILED", 1, original)
        reference = result["recovery_reference"]
        self.assertIsNotNone(reference)
        with patch("tool_runtime.retrieve_recovered_text", return_value=original) as retrieve:
            recovered = self.runtime.call("tool_recover", {"recovery_reference": reference})
        retrieve.assert_called_once_with(handle)
        self.assertEqual(recovered["output"], original)
        self.assertEqual(hashlib.sha256(recovered["output"].encode()).hexdigest(), hashlib.sha256(original.encode()).hexdigest())

    def test_unknown_recovery_reference_fails_closed(self) -> None:
        result = self.runtime.call("tool_recover", {"recovery_reference": "unknown"})
        self.assertEqual(result["status"], "REFUSED")

    def test_recovery_digest_mismatch_is_not_returned(self) -> None:
        original = "ERROR critical finding source.py:8\n" + "routine\n" * 500
        reference = "known-reference"
        self.runtime._recoveries[reference] = ("fixture_handle_123", hashlib.sha256(original.encode()).hexdigest())
        with patch("tool_runtime.retrieve_recovered_text", return_value="different raw output"):
            result = self.runtime.call("tool_recover", {"recovery_reference": reference})
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["output"], "")
        self.assertIn("digest mismatch", result["error"])

    def test_timeout_kills_command_group_and_keeps_partial_output(self) -> None:
        class FakeProcess:
            def __init__(self):
                self.stdout = io.BytesIO(b"partial test output")
                self.stderr = io.BytesIO(b"")
                self.returncode = None
                self.pid = 99
                self.waits = 0

            def poll(self):
                return None if self.returncode is None else self.returncode

            def wait(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired("command", timeout)
                self.returncode = -9
                return self.returncode

        process = FakeProcess()
        with patch("tool_executor.shutil.which", side_effect=lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else "/usr/bin/python3"), \
             patch("tool_executor.subprocess.Popen", side_effect=lambda *args, **kwargs: process), \
             patch("tool_executor.os.killpg") as killpg:
            result = tool_executor.execute_command({"tool": "run_tests", "workspace": str(self.root),
                "execution_policy": "READ_ONLY", "arguments": {"argv": ["python3"], "timeout_seconds": 1}})
        self.assertEqual(result["execution_status"], "TIMED_OUT")
        self.assertIn("partial test output", result["stdout"])
        self.assertEqual(result["test_outcome"], "FAILED")
        killpg.assert_called_once_with(99, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
