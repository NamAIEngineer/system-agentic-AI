from __future__ import annotations

from importlib.machinery import SourceFileLoader
from pathlib import Path
import unittest
from unittest.mock import patch
import json
import tempfile
from contextlib import redirect_stdout
from io import StringIO
import sys


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "system-agentic-bootstrap"
bootstrap = SourceFileLoader("system_agentic_bootstrap", str(SCRIPT)).load_module()


class BootstrapRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "auto_triggers": ["đội ai"],
            "auto_submit": {
                "enabled": True,
                "team_for_action_goals": True,
                "resume_active_run_on_continue": True,
                "execution_policy": "WORKSPACE_WRITE",
            },
        }

    def test_action_prompt_submits_deterministic_goal(self):
        event = {"hook_event_name": "UserPromptSubmit", "session_id": "session-1"}
        with patch.object(bootstrap, "_post_json", return_value={"goal_run_id": "hook-goal", "queued": True}) as post:
            result = bootstrap._auto_submit(
                "Implement the bounded team workflow for this workspace",
                "/tmp",
                event,
                "UserPromptSubmit",
                self.config,
                [],
            )
        self.assertEqual(result["status"], "SUBMITTED")
        payload = post.call_args.args[1]
        self.assertEqual(payload["goal_run_id"], bootstrap._goal_id("/tmp", payload["goal"], event))
        self.assertEqual(payload["execution_policy"], "WORKSPACE_WRITE")

    def test_status_request_without_question_mark_stays_on_host(self):
        result = bootstrap._auto_route(
            "Provide a status update for this workspace",
            "UserPromptSubmit",
            self.config,
        )
        self.assertIsNone(result)

    def test_status_dashboard_update_remains_action_route(self):
        result = bootstrap._auto_route(
            "Update the status dashboard for this workspace",
            "UserPromptSubmit",
            self.config,
        )
        self.assertEqual(result, ("goal_action", True))

    def test_same_session_prompt_has_stable_goal_id(self):
        event = {"session_id": "session-1"}
        first = bootstrap._goal_id("/tmp", "Implement this goal", event)
        second = bootstrap._goal_id("/tmp", "Implement this goal", event)
        self.assertEqual(first, second)

    def test_sensitive_prompt_is_blocked_before_hstry_or_submit(self):
        with patch.object(bootstrap, "_post_json") as post:
            result = bootstrap._auto_submit(
                "Update token: TEST_TOKEN_PLACEHOLDER",
                "/tmp",
                {"hook_event_name": "UserPromptSubmit"},
                "UserPromptSubmit",
                self.config,
                [],
            )
        self.assertEqual(result["status"], "BLOCKED")
        post.assert_not_called()

    def test_resume_requires_exactly_one_active_run(self):
        active = [
            {"goal_run_id": "goal-a", "workspace": str(Path("/tmp").resolve()), "current_state": "EXECUTING"},
            {"goal_run_id": "goal-b", "workspace": str(Path("/tmp").resolve()), "current_state": "READY"},
        ]
        result = bootstrap._auto_submit(
            "tiếp tục làm",
            "/tmp",
            {"hook_event_name": "UserPromptSubmit"},
            "UserPromptSubmit",
            self.config,
            active,
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "multiple_active_runs_require_host_selection")

    def test_active_runs_hide_stale_team_blockers_after_accepted_recovery(self):
        with tempfile.TemporaryDirectory(prefix="bootstrap-active-runs-") as root:
            path = Path(root) / "accepted.json"
            path.write_text(json.dumps({
                "goal_run_id": "accepted-team",
                "current_state": "COMPLETED",
                "decision": "TEAM",
                "workspace": "/tmp/workspace",
                "final_status": "COMPLETED",
                "acceptance_passed": True,
                "team_status": "COMPLETED",
                "blockers": [
                    {"class": "team_recovery", "reason": "historical recovery"},
                    {"class": "real", "reason": "current blocker"},
                ],
                "transitions": [],
            }), encoding="utf-8")
            with patch.object(bootstrap, "Path", Path):
                original = bootstrap.os.environ.get("SYSTEM_AGENTIC_ORCHESTRATOR_STATE_ROOT")
                bootstrap.os.environ["SYSTEM_AGENTIC_ORCHESTRATOR_STATE_ROOT"] = root
                try:
                    runs = bootstrap._active_runs()
                finally:
                    if original is None:
                        bootstrap.os.environ.pop("SYSTEM_AGENTIC_ORCHESTRATOR_STATE_ROOT", None)
                    else:
                        bootstrap.os.environ["SYSTEM_AGENTIC_ORCHESTRATOR_STATE_ROOT"] = original
        self.assertEqual(runs[0]["blocker_count"], 1)

    def test_hook_output_is_codex_supported_envelope(self):
        output = StringIO()
        original_argv = sys.argv
        try:
            sys.argv = [str(SCRIPT), "--workspace", "/tmp", "--json"]
            with patch.object(bootstrap, "_event", return_value={"hook_event_name": "UserPromptSubmit"}), \
                 patch.object(bootstrap, "_team_config", return_value={}), \
                 patch.object(bootstrap, "_active_runs", return_value=[]), \
                 patch.object(bootstrap, "_auto_submit", return_value={"status": "SKIPPED"}), \
                 redirect_stdout(output):
                self.assertEqual(bootstrap.main(), 0)
        finally:
            sys.argv = original_argv
        envelope = json.loads(output.getvalue())
        self.assertEqual(set(envelope), {"hookSpecificOutput"})
        self.assertEqual(set(envelope["hookSpecificOutput"]), {"hookEventName", "additionalContext"})
        self.assertEqual(envelope["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        context = json.loads(envelope["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(context["event_type"], "bounded_session_bootstrap")


if __name__ == "__main__":
    unittest.main()
