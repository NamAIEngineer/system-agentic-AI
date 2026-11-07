from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "products" / "agent-runtime"))
import agentic_runtime


class ToolExecutionQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.queue = self.root / "queue"
        self.patch = patch.object(agentic_runtime, "APP_SUPPORT", self.queue)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.role_patch = patch.dict(os.environ, {"SYSTEM_AGENTIC_EXECUTION_ROLE": "interactive-host"})
        self.role_patch.start()
        self.addCleanup(self.role_patch.stop)

    def tearDown(self):
        self.temp.cleanup()

    def worker_ready(self, *, busy=False):
        conn = agentic_runtime.database(self.queue)
        conn.execute("INSERT OR REPLACE INTO worker_state(singleton,heartbeat,pid,busy) VALUES(1,?,?,?)",
                     (time.time(), os.getpid(), int(busy)))
        conn.commit()
        conn.close()

    def test_typed_request_is_claimed_and_completed_without_provider_job(self):
        self.worker_ready()
        output = {}

        def submit():
            output["result"] = agentic_runtime.enqueue_tool_execution(
                str(self.workspace), "WORKSPACE_WRITE", "run_tests", {"argv": ["python3", "-m", "unittest"]}, 5,
            )

        submitter = threading.Thread(target=submit)
        submitter.start()
        conn = agentic_runtime.database(self.queue)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and conn.execute("SELECT count(*) FROM tool_jobs").fetchone()[0] == 0:
            time.sleep(0.02)
        claimed = agentic_runtime.claim(conn)
        self.assertEqual(claimed["kind"], "tool")
        saved_request = json.loads(claimed["request_json"])
        self.assertEqual(saved_request["execution_policy"], "WORKSPACE_WRITE")
        fake_executor = type("FakeExecutor", (), {"execute_command": staticmethod(lambda request, should_cancel: {
            "execution_status": "COMPLETED", "exit_code": 8, "stdout": "assertion failed\n",
            "stderr": "", "test_outcome": "FAILED",
        })})
        with patch("importlib.reload", return_value=fake_executor), patch("importlib.import_module", return_value=fake_executor):
            agentic_runtime.process_tool_job(conn, claimed)
        submitter.join(timeout=3)
        self.assertFalse(submitter.is_alive())
        self.assertEqual(output["result"]["execution_status"], "COMPLETED")
        self.assertEqual(output["result"]["test_outcome"], "FAILED")
        self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT status FROM tool_jobs").fetchone()[0], "completed")
        conn.close()

    def test_busy_unavailable_elevated_and_recursive_requests_fail_before_enqueue(self):
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            agentic_runtime.enqueue_tool_execution(str(self.workspace), "READ_ONLY", "workspace_exec", {"argv": ["true"]}, 1)
        self.worker_ready(busy=True)
        with self.assertRaisesRegex(RuntimeError, "busy"):
            agentic_runtime.enqueue_tool_execution(str(self.workspace), "READ_ONLY", "workspace_exec", {"argv": ["true"]}, 1)
        with self.assertRaisesRegex(ValueError, "ELEVATED_REQUIRES_APPROVAL"):
            agentic_runtime.enqueue_tool_execution(str(self.workspace), "ELEVATED", "workspace_exec", {"argv": ["true"]}, 1)
        with patch.dict(os.environ, {"SYSTEM_AGENTIC_EXECUTION_ROLE": "worker"}):
            with self.assertRaisesRegex(ValueError, "RECURSION_GUARD"):
                agentic_runtime.enqueue_tool_execution(str(self.workspace), "READ_ONLY", "workspace_exec", {"argv": ["true"]}, 1)
        with patch.dict(os.environ, {"SYSTEM_AGENTIC_EXECUTION_ROLE": "tool-executor"}):
            with self.assertRaisesRegex(ValueError, "RECURSION_GUARD"):
                agentic_runtime.enqueue_tool_execution(str(self.workspace), "READ_ONLY", "workspace_exec", {"argv": ["true"]}, 1)
            with self.assertRaisesRegex(ValueError, "RECURSION_GUARD"):
                agentic_runtime.submit(Namespace())
        conn = agentic_runtime.database(self.queue)
        self.assertEqual(conn.execute("SELECT count(*) FROM tool_jobs").fetchone()[0], 0)
        conn.close()

    def test_malformed_and_unsupported_request_cannot_execute(self):
        self.worker_ready()
        with self.assertRaisesRegex(ValueError, "unsupported tool job kind"):
            agentic_runtime.enqueue_tool_execution(str(self.workspace), "READ_ONLY", "shell", {"argv": ["true"]}, 1)
        from tool_executor import execute_command
        with self.assertRaisesRegex(ValueError, "unsupported typed command"):
            execute_command({"tool": "agent_task", "workspace": str(self.workspace),
                             "execution_policy": "READ_ONLY", "arguments": {"argv": ["true"]}})


if __name__ == "__main__":
    unittest.main()
