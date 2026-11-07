from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_agent


class LocalAgentEmptyTurnTests(unittest.TestCase):
    def test_one_empty_model_turn_recovers_without_repeating_completed_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, conversation_json TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO jobs (id, conversation_json) VALUES ('job-1', '')")
            job = {
                "id": "job-1",
                "workspace": str(workspace),
                "execution_context_json": "{}",
                "prompt": "Write the requested fixture file and report completion.",
                "title": "Write fixture",
                "execution_policy": "WORKSPACE_WRITE",
                "backend": "local",
                "conversation_json": "",
            }
            responses = [
                {
                    "choices": [{"finish_reason": "tool_calls", "message": {
                        "role": "assistant", "content": None,
                        "tool_calls": [{"id": "write-1", "type": "function", "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "result.txt", "content": "done\n"}),
                        }}],
                    }}],
                },
                {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None}}]},
                {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Completed."}}]},
            ]
            seen_messages = []

            def model(messages, **_kwargs):
                seen_messages.append(list(messages))
                return responses.pop(0)

            with patch.object(local_agent, "wait_for_model"), \
                 patch.object(local_agent, "execute_tool", wraps=local_agent.execute_tool) as execute_tool, \
                 patch.object(local_agent, "call_model", side_effect=model):
                result = local_agent.run_local_job(
                    conn,
                    job,
                    attempt_id=1,
                    log_path=root / "worker.jsonl",
                    mode="new",
                )

            self.assertEqual(result, 0)
            self.assertEqual((workspace / "result.txt").read_text(encoding="utf-8"), "done\n")
            self.assertEqual(execute_tool.call_count, 1)
            self.assertIn("Inspect the current workspace state", seen_messages[2][-1]["content"])
            self.assertIn("empty_turn_recovery_required", (root / "worker.jsonl").read_text(encoding="utf-8"))

    def test_second_empty_model_turn_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, conversation_json TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO jobs (id, conversation_json) VALUES ('job-2', '')")
            job = {
                "id": "job-2", "workspace": str(workspace), "execution_context_json": "{}",
                "prompt": "Finish task.", "title": "Task", "execution_policy": "READ_ONLY",
                "backend": "local", "conversation_json": "",
            }
            empty = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None}}]}
            with patch.object(local_agent, "wait_for_model"), \
                 patch.object(local_agent, "call_model", return_value=empty):
                with self.assertRaisesRegex(local_agent.LocalAgentError, "without a final answer"):
                    local_agent.run_local_job(conn, job, 1, root / "worker.jsonl", "new")


if __name__ == "__main__":
    unittest.main()
