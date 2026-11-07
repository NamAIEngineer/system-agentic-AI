from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from modules.team_ledger import TeamLedgerStore, TeamRunner, _validate_worker_result, create_ledger


class TeamLedgerRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-ledger-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_long_worker_objective_preserves_artifact_instructions(self):
        path = self.root / "ledger.json"
        objective = ("preamble " * 900) + " write the declared report to evidence.md."
        ledger = create_ledger(
            goal="prompt-boundary",
            workspace=str(self.root),
            run_id="prompt-boundary-test",
            path=path,
            raw_tasks=[{
                "task_id": "prompt-boundary-task",
                "title": "Prompt boundary",
                "objective": objective,
                "scope": "evidence.md",
                "required_artifacts": ["evidence.md"],
                "depends_on": [],
                "execution_policy": "WORKSPACE_WRITE",
                "acceptance_criteria": ["report exists"],
            }],
        )
        self.assertIn("evidence.md", ledger["tasks"][0]["objective"])
        self.assertEqual(len(ledger["tasks"][0]["objective"]), len(objective))

    def test_running_without_persisted_provider_identity_is_classified_lost(self):
        path = self.root / "ledger.json"
        ledger = create_ledger(goal="resume", workspace=str(self.root), run_id="resume-test", path=path)
        ledger["tasks"][0]["state"] = "RUNNING"
        ledger["tasks"][0]["worker_attempts"] = [{
            "attempt_id": "resume-test-architect-attempt-1",
            "state": "PREPARED",
            "started_at": "2026-10-27T00:00:00+00:00",
        }]
        ledger["tasks"][1]["state"] = "COMPLETED"
        ledger["tasks"][1]["worker_attempts"] = [{"attempt_id": "resume-test-qa-attempt-1", "state": "COMPLETED"}]
        TeamLedgerStore(path).save(ledger)

        result = TeamRunner(path, dispatch=lambda *args, **kwargs: self.fail("must not submit during recovery"), wait_seconds=0).run()

        first, second = result["tasks"][:2]
        self.assertEqual(first["state"], "FAILED")
        self.assertIn("FAILED_LOST", first["error"])
        self.assertEqual(first["worker_attempts"][0]["state"], "FAILED_LOST")
        self.assertEqual(len(second["worker_attempts"]), 1)
        self.assertEqual(second["state"], "COMPLETED")
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(any(event["event"] == "task_recovery_failed" for event in result["events"]))

    def test_running_provider_job_completion_is_adopted_without_new_attempt(self):
        path = self.root / "ledger.json"
        ledger = create_ledger(goal="resume", workspace=str(self.root), run_id="resume-adopt", path=path)
        task = ledger["tasks"][0]
        artifact = self.root / task["required_artifacts"][0]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        task["state"] = "RUNNING"
        task["worker_attempts"] = [{
            "attempt_id": "resume-adopt-architect-attempt-1",
            "state": "PREPARED",
            "provider": "local-qwen",
            "artifact_snapshot": {task["required_artifacts"][0]: {"exists": False}},
            "started_at": "2026-10-27T00:00:00+00:00",
            "task_spec": {
                "task_id": task["task_id"], "title": task["title"], "workspace": str(self.root),
                "objective": "resume", "role": "worker", "required_capabilities": ["task.submit"],
                "context": {}, "constraints": [], "acceptance_criteria": [],
                "execution_policy": "WORKSPACE_WRITE", "provider_policy": {"allowed_providers": ["local-qwen"]},
                "execution_context": {"host_role": "orchestrator", "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH"},
            },
        }]
        TeamLedgerStore(path).save(ledger)
        with patch("modules.team_ledger.reconcile_task", return_value={"execution_status": "COMPLETED", "provider": "local-qwen", "queue_job_id": task["task_id"]}):
            artifact.write_text("recovered evidence\n", encoding="utf-8")
            result = TeamRunner(path, dispatch=lambda *args, **kwargs: self.fail("must adopt existing job"), wait_seconds=0).run()
        recovered = result["tasks"][0]
        self.assertEqual(recovered["state"], "COMPLETED")
        self.assertEqual(len(recovered["worker_attempts"]), 1)
        self.assertEqual(recovered["worker_attempts"][0]["state"], "COMPLETED")

    def test_completed_provider_without_artifact_is_classified_no_op(self):
        path = self.root / "ledger.json"
        ledger = create_ledger(goal="no-op", workspace=str(self.root), run_id="no-op-test", path=path,
                               raw_tasks=[{
                                   "task_id": "no-op-task", "title": "No-op probe", "role": "worker",
                                   "objective": "Write the declared evidence file.", "scope": "evidence.txt",
                                   "required_artifacts": ["evidence.txt"], "depends_on": [],
                                   "execution_policy": "WORKSPACE_WRITE", "acceptance_criteria": ["file exists"],
                               }])

        def completed_without_write(*args, **kwargs):
            return {"status": "COMPLETED", "execution_status": "COMPLETED", "provider": "local-qwen",
                    "queue_job_id": "no-op-job", "summary": "claimed completion"}

        result = TeamRunner(path, dispatch=completed_without_write, wait_seconds=0).run()
        task = result["tasks"][0]
        self.assertEqual(task["state"], "NO-OP")
        self.assertEqual(task["worker_attempts"][0]["state"], "NO-OP")
        self.assertFalse(task["worker_attempts"][0]["acceptance"]["accepted"])
        self.assertEqual(result["status"], "FAILED")

    def test_refused_provider_is_classified_refused(self):
        path = self.root / "ledger.json"
        ledger = create_ledger(goal="refused", workspace=str(self.root), run_id="refused-test", path=path,
                               raw_tasks=[{
                                   "task_id": "refused-task", "title": "Refusal probe", "role": "worker",
                                   "objective": "Write the declared evidence file.", "scope": "evidence.txt",
                                   "required_artifacts": ["evidence.txt"], "depends_on": [],
                                   "execution_policy": "WORKSPACE_WRITE", "acceptance_criteria": ["file exists"],
                               }])

        def refused(*args, **kwargs):
            return {"status": "REFUSED", "execution_status": "REFUSED", "reason": "policy refusal",
                    "provider": None, "errors": [{"code": "REFUSED", "message": "policy refusal"}]}

        result = TeamRunner(path, dispatch=refused, wait_seconds=0).run()
        task = result["tasks"][0]
        self.assertEqual(task["state"], "REFUSED")
        self.assertEqual(task["worker_attempts"][0]["state"], "REFUSED")
        self.assertEqual(result["status"], "FAILED")

    def test_logged_artifact_write_survives_no_progress_provider_failure(self):
        path = self.root / "ledger.json"
        artifact = self.root / "evidence.txt"
        ledger = create_ledger(
            goal="artifact recovery", workspace=str(self.root), run_id="artifact-recovery-test",
            path=path, raw_tasks=[{
                "task_id": "artifact-task", "title": "Artifact recovery", "role": "worker",
                "objective": "Write the declared evidence file.", "scope": "evidence.txt",
                "required_artifacts": ["evidence.txt"], "depends_on": [],
                "execution_policy": "WORKSPACE_WRITE", "acceptance_criteria": ["file exists"],
            }])
        artifact.write_text("recovered\n", encoding="utf-8")
        task = ledger["tasks"][0]
        task["state"] = "RUNNING"
        task["worker_attempts"] = [{
            "attempt_id": "artifact-task-attempt-1", "state": "RUNNING", "provider": "local-qwen",
            "artifact_snapshot": {"evidence.txt": {"exists": False}},
            "started_at": "2026-10-27T00:00:00+00:00",
            "task_spec": {
                "task_id": "artifact-task", "title": "Artifact recovery", "workspace": str(self.root),
                "objective": "recover", "role": "worker", "required_capabilities": ["task.submit"],
                "context": {}, "constraints": [], "acceptance_criteria": [],
                "execution_policy": "WORKSPACE_WRITE", "provider_policy": {"allowed_providers": ["local-qwen"]},
                "execution_context": {"host_role": "orchestrator", "dispatch_depth": 1,
                                      "direct_or_dispatch": "DISPATCH"},
            },
        }]
        from modules import team_ledger
        log_path = team_ledger.QUEUE_LOGS_ROOT / "artifact-task-1.jsonl"
        log_path.write_text(
            json.dumps({"runtime": "local_tool_result", "tool": "write_file",
                        "result": json.dumps({"written": "evidence.txt"})}) + "\n"
            + json.dumps({"runtime": "no_progress_guard_failed_closed"}) + "\n",
            encoding="utf-8")
        self.addCleanup(log_path.unlink, missing_ok=True)
        TeamLedgerStore(path).save(ledger)
        with patch("modules.team_ledger.reconcile_task", return_value={
            "execution_status": "FAILED", "provider": "local-qwen", "queue_job_id": "artifact-task",
        }):
            result = TeamRunner(path, dispatch=lambda *args, **kwargs: self.fail("must not resubmit"), wait_seconds=0).run()
        self.assertEqual(result["tasks"][0]["state"], "COMPLETED")
        self.assertEqual(result["status"], "COMPLETED")

    def test_grounding_requires_every_declared_source_ref(self):
        task = {
            "artifact_kind": "copy",
            "grounding_source_refs": ["README.md", "package.json", "src/server.mjs"],
            "required_artifacts": ["copy.json"],
            "workspace": str(self.root),
            "grounding_required": True,
        }
        artifact = self.root / "copy.json"
        artifact.write_text(json.dumps({
            "source_refs": ["README.md"], "draft_text": "bounded", "audience": "UNKNOWN",
            "claims": ["UNKNOWN"], "DRAFT_ONLY": True, "rehearsal": True,
            "liveEligible": False, "externalSideEffects": False, "unknownRetryAllowed": False,
        }), encoding="utf-8")
        result = _validate_worker_result(task, {"execution_status": "COMPLETED"})
        self.assertFalse(result["accepted"])
        self.assertIn("package.json", result["reason"])
        self.assertIn("src/server.mjs", result["reason"])

    def test_provider_timeout_keeps_accepted_task_running_for_reconcile(self):
        path = self.root / "ledger.json"
        ledger = create_ledger(goal="timeout", workspace=str(self.root), run_id="timeout-test", path=path,
                               raw_tasks=[{
                                   "task_id": "timeout-task", "title": "Timeout probe", "role": "worker",
                                   "objective": "Write the declared evidence file.", "scope": "evidence.txt",
                                   "required_artifacts": ["evidence.txt"], "depends_on": [],
                                   "execution_policy": "WORKSPACE_WRITE", "acceptance_criteria": ["file exists"],
                               }])

        def still_running(*args, **kwargs):
            return {"status": "UNKNOWN", "execution_status": "UNKNOWN", "provider": "local-qwen",
                    "queue_job_id": "timeout-task", "errors": [{"code": "EXECUTION_STATUS_TIMEOUT",
                    "message": "Task may still be running; reconcile by task ID; do not retry."}]}

        result = TeamRunner(path, dispatch=still_running, wait_seconds=0).run()
        task = result["tasks"][0]
        self.assertEqual(task["state"], "RUNNING")
        self.assertEqual(task["worker_attempts"][0]["state"], "RUNNING")
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(len(task["worker_attempts"]), 1)

    def test_provider_timeout_keeps_dependant_pending_for_reconcile(self):
        path = self.root / "ledger.json"
        ledger = create_ledger(goal="dependent timeout", workspace=str(self.root), run_id="dependent-timeout-test", path=path,
                               raw_tasks=[
                                   {
                                       "task_id": "upstream", "title": "Upstream probe", "role": "worker",
                                       "objective": "Write the upstream evidence file.", "scope": "upstream.txt",
                                       "required_artifacts": ["upstream.txt"], "depends_on": [],
                                       "execution_policy": "WORKSPACE_WRITE", "acceptance_criteria": ["file exists"],
                                   },
                                   {
                                       "task_id": "downstream", "title": "Downstream probe", "role": "worker",
                                       "objective": "Write the downstream evidence file.", "scope": "downstream.txt",
                                       "required_artifacts": ["downstream.txt"], "depends_on": ["upstream"],
                                       "execution_policy": "WORKSPACE_WRITE", "acceptance_criteria": ["file exists"],
                                   },
                               ])

        def still_running(*args, **kwargs):
            return {"status": "UNKNOWN", "execution_status": "UNKNOWN", "provider": "local-qwen",
                    "queue_job_id": "upstream-job", "errors": [{"code": "EXECUTION_STATUS_TIMEOUT",
                    "message": "Task may still be running; reconcile by task ID; do not retry."}]}

        result = TeamRunner(path, dispatch=still_running, wait_seconds=0).run()
        tasks = {task["task_id"]: task for task in result["tasks"]}
        self.assertEqual(tasks["upstream"]["state"], "RUNNING")
        self.assertEqual(tasks["downstream"]["state"], "PENDING")
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(len(tasks["downstream"]["worker_attempts"]), 0)

    def test_recovery_without_artifact_snapshot_is_failed_lost(self):
        path = self.root / "ledger.json"
        ledger = create_ledger(goal="lost snapshot", workspace=str(self.root), run_id="lost-snapshot-test", path=path)
        task = ledger["tasks"][0]
        artifact = self.root / task["required_artifacts"][0]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("pre-existing evidence\n", encoding="utf-8")
        task["state"] = "RUNNING"
        task["worker_attempts"] = [{
            "attempt_id": "lost-snapshot-test-attempt-1", "state": "PREPARED", "provider": "local-qwen",
            "started_at": "2026-10-27T00:00:00+00:00", "task_spec": {
                "task_id": task["task_id"], "title": task["title"], "workspace": str(self.root),
                "objective": "resume", "role": "worker", "required_capabilities": ["task.submit"],
                "context": {}, "constraints": [], "acceptance_criteria": [],
                "execution_policy": "WORKSPACE_WRITE", "provider_policy": {"allowed_providers": ["local-qwen"]},
                "execution_context": {"host_role": "orchestrator", "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH"},
            },
        }]
        TeamLedgerStore(path).save(ledger)
        with patch("modules.team_ledger.reconcile_task", return_value={"execution_status": "COMPLETED", "provider": "local-qwen"}):
            result = TeamRunner(path, dispatch=lambda *args, **kwargs: self.fail("must not submit during recovery"), wait_seconds=0).run()
        self.assertEqual(result["tasks"][0]["state"], "FAILED_LOST")
        self.assertIn("snapshot", result["tasks"][0]["error"])

    def test_recovery_with_partial_artifact_snapshot_is_failed_lost(self):
        path = self.root / "ledger.json"
        ledger = create_ledger(goal="partial snapshot", workspace=str(self.root), run_id="partial-snapshot-test", path=path)
        task = ledger["tasks"][0]
        artifact = self.root / task["required_artifacts"][0]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("pre-existing evidence\n", encoding="utf-8")
        task["state"] = "RUNNING"
        task["worker_attempts"] = [{
            "attempt_id": "partial-snapshot-test-attempt-1", "state": "PREPARED", "provider": "local-qwen",
            "artifact_snapshot": {}, "started_at": "2026-10-27T00:00:00+00:00", "task_spec": {
                "task_id": task["task_id"], "title": task["title"], "workspace": str(self.root),
                "objective": "resume", "role": "worker", "required_capabilities": ["task.submit"],
                "context": {}, "constraints": [], "acceptance_criteria": [],
                "execution_policy": "WORKSPACE_WRITE", "provider_policy": {"allowed_providers": ["local-qwen"]},
                "execution_context": {"host_role": "orchestrator", "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH"},
            },
        }]
        TeamLedgerStore(path).save(ledger)
        with patch("modules.team_ledger.reconcile_task", return_value={"execution_status": "COMPLETED", "provider": "local-qwen"}):
            result = TeamRunner(path, dispatch=lambda *args, **kwargs: self.fail("must not submit during recovery"), wait_seconds=0).run()
        self.assertEqual(result["tasks"][0]["state"], "FAILED_LOST")
        self.assertIn("complete pre-dispatch artifact snapshot", result["tasks"][0]["error"])

    def test_queued_provider_job_is_adopted_without_new_attempt(self):
        path = self.root / "ledger.json"
        ledger = create_ledger(goal="queued recovery", workspace=str(self.root), run_id="queued-recovery-test", path=path)
        task = ledger["tasks"][0]
        attempt = {
            "attempt_id": "queued-recovery-test-architect-attempt-1",
            "state": "FAILED_LOST",
            "provider": None,
            "worker_id": None,
            "artifact_snapshot": {task["required_artifacts"][0]: {"exists": False}},
            "started_at": "2026-10-27T00:00:00+00:00",
            "task_spec": {
                "task_id": task["task_id"], "title": task["title"], "workspace": str(self.root),
                "objective": "resume", "role": "worker", "required_capabilities": ["task.submit"],
                "context": {}, "constraints": [], "acceptance_criteria": [],
                "execution_policy": "WORKSPACE_WRITE", "provider_policy": {"allowed_providers": ["local-qwen"]},
                "execution_context": {"host_role": "orchestrator", "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH"},
            },
        }
        task["state"] = "FAILED"
        task["error"] = "FAILED_LOST: provider identity or task spec was not persisted"
        task["worker_attempts"] = [attempt]
        TeamLedgerStore(path).save(ledger)
        with patch("modules.team_ledger.adapter_for") as adapter_for_mock:
            adapter_for_mock.return_value.status.return_value.status = "SUBMITTED"
            runner = TeamRunner(path, dispatch=lambda *args, **kwargs: self.fail("must not submit during adoption"), wait_seconds=0)
            recovered = runner.prepare_resume()
        self.assertEqual(recovered["tasks"][0]["state"], "RUNNING")
        self.assertEqual(recovered["tasks"][0]["worker_attempts"][0]["provider"], "local-qwen")
        self.assertEqual(len(recovered["tasks"][0]["worker_attempts"]), 1)

    def test_stale_dependency_blocker_reopens_and_runs_without_duplicate_upstream(self):
        path = self.root / "ledger.json"
        ledger = create_ledger(
            goal="dependency recovery", workspace=str(self.root), run_id="dependency-recovery-test",
            path=path, raw_tasks=[
                {
                    "task_id": "integration", "title": "Integration", "role": "integrator",
                    "objective": "Write integration evidence.", "scope": "integration.txt",
                    "required_artifacts": ["integration.txt"], "depends_on": [],
                    "execution_policy": "WORKSPACE_WRITE", "acceptance_criteria": ["file exists"],
                },
                {
                    "task_id": "verifier", "title": "Verifier", "role": "verifier",
                    "objective": "Write verification evidence.", "scope": "verification.txt",
                    "required_artifacts": ["verification.txt"], "depends_on": ["integration"],
                    "execution_policy": "WORKSPACE_WRITE", "acceptance_criteria": ["file exists"],
                },
            ])
        (self.root / "integration.txt").write_text("integrated\n", encoding="utf-8")
        integration, verifier = ledger["tasks"]
        integration.update({"state": "COMPLETED", "result": {"status": "COMPLETED"},
                            "worker_attempts": [{"attempt_id": "integration-attempt-1", "state": "COMPLETED"}]})
        verifier.update({"state": "BLOCKED", "error": "dependency did not complete"})
        ledger["status"] = "FAILED"
        TeamLedgerStore(path).save(ledger)

        dispatches = []

        def complete_verifier(task, **kwargs):
            dispatches.append(task.task_id)
            (self.root / "verification.txt").write_text("verified\n", encoding="utf-8")
            return {"status": "COMPLETED", "execution_status": "COMPLETED",
                    "provider": "local-qwen", "queue_job_id": "verifier-job",
                    "summary": "verified"}

        result = TeamRunner(path, dispatch=complete_verifier, wait_seconds=0).run()
        tasks = {task["task_id"]: task for task in result["tasks"]}
        self.assertEqual(dispatches, ["verifier"])
        self.assertEqual(tasks["integration"]["state"], "COMPLETED")
        self.assertEqual(tasks["verifier"]["state"], "COMPLETED")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertTrue(any(event["event"] == "task_unblocked_after_dependency_recovery"
                            and event["task_id"] == "verifier" for event in result["events"]))

    def test_intentional_blocked_task_stays_blocked(self):
        path = self.root / "ledger.json"
        ledger = create_ledger(
            goal="intentional blocker", workspace=str(self.root), run_id="intentional-blocker-test",
            path=path, raw_tasks=[{
                "task_id": "blocked", "title": "Blocked", "role": "worker",
                "objective": "No work.", "scope": "blocked.txt", "required_artifacts": ["blocked.txt"],
                "depends_on": ["upstream"], "execution_policy": "WORKSPACE_WRITE",
                "acceptance_criteria": ["never runs"],
            }, {
                "task_id": "upstream", "title": "Upstream", "role": "worker",
                "objective": "No work.", "scope": "upstream.txt", "required_artifacts": ["upstream.txt"],
                "depends_on": [], "execution_policy": "WORKSPACE_WRITE",
                "acceptance_criteria": ["never runs"],
            }])
        ledger["tasks"][0].update({"state": "BLOCKED", "error": "human approval required"})
        ledger["tasks"][1].update({"state": "COMPLETED"})
        TeamLedgerStore(path).save(ledger)
        result = TeamRunner(path, dispatch=lambda *args, **kwargs: self.fail("must not dispatch")).run()
        blocked = next(task for task in result["tasks"] if task["task_id"] == "blocked")
        self.assertEqual(blocked["state"], "BLOCKED")
        self.assertEqual(blocked["error"], "human approval required")


if __name__ == "__main__":
    unittest.main()
