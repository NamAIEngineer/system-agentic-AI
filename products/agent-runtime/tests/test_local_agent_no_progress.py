from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_agent


def job_for(workspace: Path, job_id: str) -> dict[str, str]:
    return {
        "id": job_id,
        "workspace": str(workspace),
        "execution_context_json": "{}",
        "prompt": "Write the requested fixture and verify it.",
        "title": "No-progress guard",
        "execution_policy": "WORKSPACE_WRITE",
        "backend": "local",
        "conversation_json": "",
    }


class LocalAgentNoProgressTests(unittest.TestCase):
    def test_declared_write_completes_before_repeated_tool_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            context = {
                "artifact_completion_policy": "accept-after-write-file-empty-turn",
                "artifact_paths": ["copy.json"],
                "artifact_snapshot": {"copy.json": {"exists": False}},
            }
            payload = {
                "source_refs": ["README.md"], "draft_text": "draft", "audience": "UNKNOWN",
                "claims": ["UNKNOWN"], "DRAFT_ONLY": True, "rehearsal": True,
                "liveEligible": False, "externalSideEffects": False, "unknownRetryAllowed": False,
            }
            result = local_agent.execute_tool(
                workspace,
                "write_file",
                json.dumps({"path": "copy.json", "content": json.dumps(payload)}),
                mutations_allowed=True,
                workspace_context={"assignedWorkspace": str(workspace), "workspaceAuthority": "SYSTEM_AGENTIC_QUEUE"},
                artifact_context={"artifact_kind": "copy", "artifact_paths": ["copy.json"]},
            )
            self.assertTrue(local_agent.artifact_write_result_ready(workspace, context, result))

    def test_argument_alias_gets_protocol_recovery_without_execution(self) -> None:
        function = {"name": "read_file", "argument": '{"path":"README.md"}'}
        message = local_agent.tool_protocol_recovery_message(function, None)
        self.assertIsNotNone(message)
        self.assertIn("function.arguments", message)
        self.assertIn("function.argument is invalid", message)

    def test_signature_ignores_call_ids_and_argument_order(self) -> None:
        first = [{"id": "call-a", "function": {"name": "read_file", "arguments": '{"path":"x","mode":"r"}'}}]
        second = [{"id": "call-b", "function": {"name": "read_file", "arguments": '{"mode":"r", "path": "x"}'}}]
        self.assertEqual(local_agent.tool_call_signature(first), local_agent.tool_call_signature(second))

    def test_repeating_tool_cycle_length_detects_only_repeated_suffix(self) -> None:
        a = (("write_file", '{"path":"x"}'),)
        b = (("read_file", '{"path":"x"}'),)
        self.assertIsNone(local_agent.repeating_tool_cycle_length([a, b, a]))
        self.assertEqual(local_agent.repeating_tool_cycle_length([a, b, a, b]), 2)

    def test_first_repeat_is_suppressed_without_duplicate_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, conversation_json TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO jobs (id, conversation_json) VALUES ('guard-1', '')")
            call = lambda call_id: {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": call_id, "type": "function", "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "result.txt", "content": "once\n"}),
                    },
                }],
            }
            responses = [
                {"choices": [{"finish_reason": "tool_calls", "message": call("write-a")}]},
                {"choices": [{"finish_reason": "tool_calls", "message": call("write-b")}]},
                {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Completed."}}]},
            ]

            with patch.object(local_agent, "wait_for_model"), patch.object(local_agent, "call_model", side_effect=responses):
                result = local_agent.run_local_job(conn, job_for(workspace, "guard-1"), 1, root / "worker.jsonl", "new")

            self.assertEqual(result, 0)
            self.assertEqual((workspace / "result.txt").read_text(encoding="utf-8"), "once\n")
            log = (root / "worker.jsonl").read_text(encoding="utf-8")
            self.assertIn("no_progress_guard_suppressed_repeat", log)
            self.assertNotIn("no_progress_guard_failed_closed", log)

    def test_second_repeat_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, conversation_json TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO jobs (id, conversation_json) VALUES ('guard-2', '')")
            call = lambda call_id: {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": call_id, "type": "function", "function": {
                        "name": "read_file", "arguments": json.dumps({"path": "missing.txt"}),
                    },
                }],
            }
            responses = [
                {"choices": [{"finish_reason": "tool_calls", "message": call("read-a")}]},
                {"choices": [{"finish_reason": "tool_calls", "message": call("read-b")}]},
                {"choices": [{"finish_reason": "tool_calls", "message": call("read-c")}]},
            ]

            with patch.object(local_agent, "wait_for_model"), patch.object(local_agent, "call_model", side_effect=responses):
                with self.assertRaisesRegex(local_agent.LocalAgentError, "no-progress guard stopped execution"):
                    local_agent.run_local_job(conn, job_for(workspace, "guard-2"), 1, root / "worker.jsonl", "new")

            self.assertIn("no_progress_guard_failed_closed", (root / "worker.jsonl").read_text(encoding="utf-8"))

    def test_schema_rejection_allows_one_bounded_recovery_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, conversation_json TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO jobs (id, conversation_json) VALUES ('guard-schema', '')")
            context = {
                "artifact_kind": "copy",
                "artifact_paths": ["copy.json"],
            }
            job = job_for(workspace, "guard-schema")
            job["execution_context_json"] = json.dumps(context)
            invalid_content = json.dumps({
                "source_refs": ["README.md"],
                "draft_text": "draft",
                "audience": "UNKNOWN",
                "claim": "typo",
                "DRAFT_ONLY": True,
                "rehearsal": True,
                "liveEligible": False,
                "externalSideEffects": False,
                "unknownRetryAllowed": False,
            })

            def call(call_id: str) -> dict:
                return {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": call_id, "type": "function", "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "copy.json", "content": invalid_content}),
                        },
                    }],
                }

            responses = [
                {"choices": [{"finish_reason": "tool_calls", "message": call("schema-a")}]},
                {"choices": [{"finish_reason": "tool_calls", "message": call("schema-b")}]},
                {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Reported the schema blocker."}}]},
            ]

            with patch.object(local_agent, "wait_for_model"), patch.object(local_agent, "call_model", side_effect=responses):
                result = local_agent.run_local_job(conn, job, 1, root / "worker.jsonl", "new")

            self.assertEqual(result, 0)
            self.assertFalse((workspace / "copy.json").exists())
            log = (root / "worker.jsonl").read_text(encoding="utf-8")
            self.assertIn("tool_recovery_instruction", log)
            self.assertIn("no_progress_guard_suppressed_repeat", log)
            self.assertNotIn("no_progress_guard_failed_closed", log)

    def test_nonconsecutive_repeat_is_suppressed_then_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, conversation_json TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO jobs (id, conversation_json) VALUES ('guard-cycle', '')")

            def call(call_id: str, name: str, path: str = "result.txt") -> dict:
                args = {"path": path}
                if name == "write_file":
                    args["content"] = "once\n"
                return {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": call_id, "type": "function", "function": {
                            "name": name, "arguments": json.dumps(args),
                        },
                    }],
                }

            responses = [
                {"choices": [{"finish_reason": "tool_calls", "message": call("write-a", "write_file")}]},
                {"choices": [{"finish_reason": "tool_calls", "message": call("read-a", "read_file")}]},
                {"choices": [{"finish_reason": "tool_calls", "message": call("write-b", "write_file")}]},
                {"choices": [{"finish_reason": "tool_calls", "message": call("read-b", "read_file")}]},
                {"choices": [{"finish_reason": "tool_calls", "message": call("write-c", "write_file")}]},
            ]

            with patch.object(local_agent, "wait_for_model"), patch.object(local_agent, "call_model", side_effect=responses):
                with self.assertRaisesRegex(local_agent.LocalAgentError, "no-progress guard stopped execution"):
                    local_agent.run_local_job(conn, job_for(workspace, "guard-cycle"), 1, root / "worker.jsonl", "new")

            log = (root / "worker.jsonl").read_text(encoding="utf-8")
            self.assertIn("no_progress_guard_suppressed_cycle", log)
            self.assertIn("no_progress_guard_failed_closed", log)
            # The first write/read pair executes. Repeated signatures are
            # suppressed before execution; the third write fails closed.
            self.assertEqual(log.count('"runtime": "local_tool_call"'), 2)


if __name__ == "__main__":
    unittest.main()
