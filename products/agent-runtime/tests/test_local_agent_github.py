import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_agent


class GitHubReadToolTests(unittest.TestCase):
    def test_rejects_non_repository_input_before_process_start(self):
        with patch.object(local_agent.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                local_agent.github_read({"repository": "https://github.com/org/repo"})
            run.assert_not_called()

    def test_list_only_returns_allowlisted_fields(self):
        responses = [
            type("Result", (), {"returncode": 0, "stdout": "logged in", "stderr": ""})(),
            type("Result", (), {"returncode": 0, "stdout": json.dumps([{"number": 7, "title": "Fix bug", "state": "OPEN", "url": "https://github.com/org/repo/issues/7", "updatedAt": "2026-10-14", "author": {"login": "private"}, "token": "must not escape"}]), "stderr": ""})(),
        ]
        with patch.object(local_agent, "shutil_which", return_value="/usr/bin/gh"), patch.object(local_agent.subprocess, "run", side_effect=responses) as run:
            payload = json.loads(local_agent.github_read({"repository": "org/repo", "state": "open", "limit": 3}))
        self.assertEqual(payload[0]["number"], 7)
        self.assertNotIn("author", payload[0])
        self.assertNotIn("token", payload[0])
        self.assertIn("issue", run.call_args_list[1].args[0])
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], 20)

    def test_issue_body_is_bounded(self):
        responses = [
            type("Result", (), {"returncode": 0, "stdout": "logged in", "stderr": ""})(),
            type("Result", (), {"returncode": 0, "stdout": json.dumps({"number": 2, "title": "Example", "body": "x" * 10_000, "unexpected": "drop"}), "stderr": ""})(),
        ]
        with patch.object(local_agent, "shutil_which", return_value="/usr/bin/gh"), patch.object(local_agent.subprocess, "run", side_effect=responses):
            payload = json.loads(local_agent.github_read({"repository": "org/repo"}, issue_number=2))
        self.assertEqual(len(payload["body"]), 8_000)
        self.assertNotIn("unexpected", payload)

    def test_auth_failure_does_not_return_cli_output(self):
        response = type("Result", (), {"returncode": 1, "stdout": "secret-token", "stderr": "secret-token"})()
        with patch.object(local_agent, "shutil_which", return_value="/usr/bin/gh"), patch.object(local_agent.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(local_agent.LocalAgentError, "credential output was withheld") as error:
                local_agent.github_read({"repository": "org/repo"})
        self.assertNotIn("secret-token", str(error.exception))

    def test_view_requires_issue_number_instead_of_falling_back_to_list(self):
        with patch.object(local_agent.subprocess, "run") as run:
            result = local_agent.execute_tool(Path.cwd(), "github_view_issue", json.dumps({"repository": "org/repo"}))
        self.assertIn("number is required", json.loads(result)["error"])
        run.assert_not_called()

    def test_hybrid_refuses_authenticated_github_tool(self):
        result = local_agent.execute_tool(Path.cwd(), "github_list_issues", json.dumps({"repository": "org/repo"}), hybrid_enabled=True)
        self.assertIn("local-only", json.loads(result)["error"])


class TaskScopedKnowledgeToolTests(unittest.TestCase):
    def test_generic_task_does_not_receive_system_knowledge_tools(self):
        self.assertFalse(local_agent.task_needs_system_knowledge("SQLite migration", "Plan a local table migration with rollback."))

    def test_explicit_workbench_task_receives_fixed_reference_tools(self):
        self.assertTrue(local_agent.task_needs_system_knowledge("System Agentic AI", "Review the workbench knowledge map."))

    def test_knowledge_tool_is_denied_when_task_scope_does_not_include_it(self):
        result = local_agent.execute_tool(
            Path.cwd(), "search_system_knowledge", json.dumps({"query": "migration"}), knowledge_enabled=False,
        )
        self.assertIn("only for tasks", json.loads(result)["error"])

    def test_explicit_read_only_constraint_removes_mutation_tools(self):
        self.assertTrue(local_agent.task_is_read_only("SQLite plan", "Give a migration plan. Do not edit files."))
        self.assertFalse(local_agent.task_is_read_only("Fix median", "Edit math_utils.py and run the verifier."))

    def test_read_only_runtime_description_keeps_declared_artifact_writes(self):
        prompt = (
            "Implement the smallest read-only Account Agent projection. "
            "Write only the two declared artifacts; run focused tests."
        )
        self.assertFalse(local_agent.task_is_read_only("Account Agent registry", prompt))
        self.assertTrue(local_agent.task_mutations_allowed("Account Agent registry", prompt, "WORKSPACE_WRITE"))

    def test_read_only_report_description_keeps_declared_report_write(self):
        prompt = "Produce a bounded read-only Mission Control report. Write only the declared report artifact."
        self.assertFalse(local_agent.task_is_read_only("Gap audit", prompt))

    def test_requested_artifact_write_keeps_workspace_write_policy(self):
        prompt = (
            "Create the requested artifact as the first action. Do not edit product source. "
            "Write only .system-agentic-goals/run/team/runtime-audit.md."
        )
        self.assertFalse(local_agent.task_is_read_only("Bounded runtime audit", prompt))
        self.assertTrue(local_agent.task_mutations_allowed("Bounded runtime audit", prompt, "WORKSPACE_WRITE"))

    def test_dotted_hidden_artifact_path_keeps_workspace_write_policy(self):
        prompt = (
            "Inspect bounded runtime state. Write only "
            ".system-agentic-goals/run/team/feed-audit.md. Do not edit product source."
        )
        self.assertFalse(local_agent.task_is_read_only("Feed audit", prompt))
        self.assertTrue(local_agent.task_mutations_allowed("Feed audit", prompt, "WORKSPACE_WRITE"))

    def test_report_destination_after_prose_keeps_workspace_write_policy(self):
        prompt = (
            "Use bounded read-only observations. Write FACT/INFERENCE/UNKNOWN "
            "evidence plus one next action to "
            ".system-agentic-goals/run/team/feed-authority-audit.md. Do not edit product source."
        )
        self.assertFalse(local_agent.task_is_read_only("Auto-GTM authority audit", prompt))
        self.assertTrue(local_agent.task_mutations_allowed("Auto-GTM authority audit", prompt, "WORKSPACE_WRITE"))

    def test_read_only_verifier_may_write_declared_write_file_artifact(self):
        prompt = (
            "Verifier is read-only with respect to product/canonical state. "
            "Call write_file exactly once as the first and only tool call, writing "
            ".system-agentic-goals/run/team/verification.md, then answer Status: DONE. "
            "Do not edit product source."
        )
        self.assertFalse(local_agent.task_is_read_only("Independent bounded verification", prompt))
        self.assertTrue(local_agent.task_mutations_allowed("Independent bounded verification", prompt, "WORKSPACE_WRITE"))

    def test_declared_receipt_write_keeps_workspace_write_policy(self):
        prompt = (
            "Immediately write the declared receipt using the workspace-relative path "
            ".system-agentic-goals/run/team/runtime-gates.md, then inspect bounded facts. "
            "Do not edit product source."
        )
        self.assertFalse(local_agent.task_is_read_only("Runtime gates", prompt))
        self.assertTrue(local_agent.task_mutations_allowed("Runtime gates", prompt, "WORKSPACE_WRITE"))

    def test_read_only_task_cannot_call_write_or_command_tools(self):
        for name, arguments in (("write_file", {"path": "x", "content": "x"}), ("run_command", {"argv": ["echo", "x"]})):
            result = local_agent.execute_tool(Path.cwd(), name, json.dumps(arguments), mutations_allowed=False)
            self.assertIn("disabled by this task", json.loads(result)["error"])

    def test_integrator_path_write_keeps_workspace_write_policy(self):
        prompt = (
            "Read only the prior accepted artifact. Write "
            ".system-agentic-goals/run/team/integration-round-2.json with exact compact JSON."
        )
        self.assertFalse(local_agent.task_is_read_only("Integrate repair round 2", prompt))
        self.assertTrue(local_agent.task_mutations_allowed("Integrate repair round 2", prompt, "WORKSPACE_WRITE"))


if __name__ == "__main__":
    unittest.main()
