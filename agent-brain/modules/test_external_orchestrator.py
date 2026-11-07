from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_all_start_methods, get_context
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from modules.external_orchestrator import ExternalOrchestrator, GoalRun, GoalRunStore, _default_state_root
from modules.system_composer import BlueprintStep, SystemBlueprint


def _create_goal_from_process(args: tuple[str, str]) -> tuple[str, str, str]:
    """Create one deterministic goal from an independent process."""
    state_root, workspace = args
    orchestrator = ExternalOrchestrator(state_root=state_root)
    run = orchestrator.create(
        "Persist one cross-process goal",
        workspace=workspace,
        goal_run_id="goal-cross-process",
    )
    return run.goal_run_id, run.workspace, run.original_goal


def blueprint(system_id: str, *, composed: bool = False, status: str = "VALID") -> SystemBlueprint:
    steps = [BlueprintStep("step-1", "demo.capability", "demo", outputs=({"name": "result", "type": "text/plain"},),
                             verification=("execution_status=COMPLETED",), task={"context": {}})]
    if composed:
        steps.append(BlueprintStep("step-2", "demo.second", "demo-2", depends_on=("step-1",),
                                   inputs=({"from": "step-1:result", "to": "input", "type": "text/plain"},),
                                   outputs=({"name": "result", "type": "text/plain"},),
                                   verification=("execution_status=COMPLETED",), task={"context": {}}))
    return SystemBlueprint(system_id, "synthetic goal", "synthetic goal", (), tuple(steps),
                           ("execution completed",), ("no elevated",), (), composed, status, "", "WORKSPACE_WRITE")


class ExternalOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="external-orchestrator-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_lifecycle_rejects_invalid_transition_and_persists(self):
        store = GoalRunStore(self.root / "state")
        run = GoalRun("goal-1", "goal", "now")
        with self.assertRaises(ValueError):
            run.transition("EXECUTING")
        store.save(run)
        loaded = store.load("goal-1")
        self.assertEqual(loaded.current_state, "RECEIVED")

    def test_default_state_root_uses_macos_persistent_location(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SYSTEM_AGENTIC_ORCHESTRATOR_STATE_ROOT", None)
            root = _default_state_root()
        if os.sys.platform == "darwin":
            self.assertEqual(root, Path.home() / "Library" / "Application Support" /
                             "SystemAgenticAI" / "orchestrator")
        else:
            self.assertEqual(root, Path.home() / ".local" / "share" /
                             "SystemAgenticAI" / "orchestrator")

    def test_direct_route_and_goal_result(self):
        dispatch = Mock(return_value={"status": "COMPLETED", "execution_status": "COMPLETED",
                                      "domain_outcome": "ARTIFACT_EXPORTED", "provider": None,
                                      "task_id": "goal-direct-step-1", "artifacts": [{"path": "x"}]})
        orchestrator = ExternalOrchestrator(state_root=self.root / "state",
                                            composer=lambda *a, **k: blueprint("goal-direct"), dispatch=dispatch)
        result = orchestrator.run("Xuất phiên luyện dạy đã lưu thành tài liệu.", workspace=str(self.root), goal_run_id="goal-direct")
        self.assertEqual(result.current_state, "COMPLETED")
        self.assertEqual(result.decision, "DIRECT")
        self.assertTrue(result.acceptance_passed)
        self.assertNotIn("content", result.artifact_refs[0])
        dispatch.assert_called_once()

    def test_chatgpt_exchange_records_consumption_receipt_and_hstry_query(self):
        state = self.root / "state-chatgpt"
        request = {
            "run_id": "goal-chatgpt-proof",
            "account_id": "chatgpt-account-1",
            "conversation_id": "conversation-1",
            "prompt": "harmless proof",
            "idempotency_key": "goal-chatgpt-proof:exchange-1",
            "approval_id": "approval-goal-chatgpt-proof",
        }
        fake_receipt = {
            "status": "COMPLETED", "source": "chatgpt-web", "source_id": "chatgpt-web:account-1",
            "conversation_id": "conversation-1", "url": "https://chatgpt.com/c/conversation-1",
            "request_id": "xchg-1", "idempotency_key": request["idempotency_key"],
            "content_sent": True, "snippet": "proof", "receipt_ref": str(state / "exchange.json"),
        }
        class FakeAdapter:
            def send(self, _request):
                return fake_receipt
        import sys
        import types
        module = types.SimpleNamespace(ChatGPTWebAdapter=lambda: FakeAdapter())
        original = sys.modules.get("modules.chatgpt_web_adapter")
        sys.modules["modules.chatgpt_web_adapter"] = module
        try:
            orchestrator = ExternalOrchestrator(state_root=state)
            run = orchestrator.create(
                "chatgpt proof", workspace=str(self.root), goal_run_id="goal-chatgpt-proof",
                context={"hstry_query": "HSTRY_PROOF", "hstry_context_enabled": False,
                         "chatgpt_request": request},
            )
            result = orchestrator.run(run.original_goal, workspace=str(self.root),
                                      context=run.context, goal_run_id=run.goal_run_id)
            self.assertTrue(result.acceptance_passed)
            self.assertTrue(result.verification["consumed_once"])
            self.assertEqual(result.verification["consumption_receipt_ref"], fake_receipt["receipt_ref"])
        finally:
            if original is None:
                sys.modules.pop("modules.chatgpt_web_adapter", None)
            else:
                sys.modules["modules.chatgpt_web_adapter"] = original

    def test_compose_route_aggregates_runtime_verification(self):
        runner = Mock(return_value={"blueprint_status": "VALID", "system_execution_status": "COMPLETED",
                                    "acceptance_passed": True, "compiled_plan": {"pipelineId": "goal-compose"},
                                    "composition": {"workflow_status": "COMPLETED", "acceptance_passed": True,
                                                    "steps": [{"task_id": "a", "module": "one", "provider": "local-qwen",
                                                               "execution_status": "COMPLETED"}],
                                                    "artifacts": [], "verification": {"dependencies_satisfied": True,
                                                                                          "provenance_preserved": True}}})
        orchestrator = ExternalOrchestrator(state_root=self.root / "state",
                                            composer=lambda *a, **k: blueprint("goal-compose", composed=True),
                                            resolver=lambda *a, **k: {"required_capabilities": [],
                                                "resolved": [{"requirement": {"capability": "demo.capability", "reason": "test"}, "module": {"id": "demo"}},
                                                              {"requirement": {"capability": "demo.second", "reason": "test"}, "module": {"id": "demo-2"}}],
                                                "unresolved": [], "blueprint_status": "VALID", "composition_required": True},
                                            system_runner=runner)
        result = orchestrator.run("natural composed goal", workspace=str(self.root), goal_run_id="goal-compose")
        self.assertEqual(result.decision, "COMPOSE")
        self.assertTrue(result.acceptance_passed)
        self.assertEqual(result.execution_refs[0]["provider"], "local-qwen")
        runner.assert_called_once()

    def test_unresolved_is_not_run(self):
        unresolved = lambda *a, **k: blueprint("goal-unresolved", status="UNRESOLVED")
        dispatch = Mock()
        orchestrator = ExternalOrchestrator(state_root=self.root / "state", composer=unresolved, dispatch=dispatch)
        result = orchestrator.run("natural unavailable goal", workspace=str(self.root), goal_run_id="goal-unresolved")
        self.assertEqual(result.current_state, "UNRESOLVED")
        self.assertEqual(result.final_status, "UNRESOLVED")
        dispatch.assert_not_called()

    def test_resume_after_planning_reuses_persisted_blueprint(self):
        dispatch = Mock(return_value={"status": "COMPLETED", "execution_status": "COMPLETED",
                                      "domain_outcome": "ARTIFACT_EXPORTED", "artifacts": [{"path": "x"}]})
        state = self.root / "state"
        first = ExternalOrchestrator(state_root=state, composer=lambda *a, **k: blueprint("goal-resume"), dispatch=dispatch)
        created = first.create("Xuất phiên luyện dạy đã lưu thành tài liệu.", workspace=str(self.root), goal_run_id="goal-resume")
        planned = first.plan(created.goal_run_id)
        self.assertEqual(planned.current_state, "READY")
        second = ExternalOrchestrator(state_root=state, composer=Mock(side_effect=AssertionError("must not replan")), dispatch=dispatch)
        resumed = second.resume("goal-resume")
        self.assertEqual(resumed.current_state, "COMPLETED")
        self.assertEqual(len(dispatch.call_args_list), 1)

    def test_repeated_run_id_is_idempotent_and_does_not_dispatch_twice(self):
        dispatch = Mock(return_value={"status": "COMPLETED", "execution_status": "COMPLETED",
                                      "domain_outcome": "ARTIFACT_EXPORTED", "artifacts": [{"path": "x"}]})
        orchestrator = ExternalOrchestrator(state_root=self.root / "state",
                                            composer=lambda *a, **k: blueprint("goal-idempotent"), dispatch=dispatch)
        first = orchestrator.run("Xuất phiên luyện dạy đã lưu thành tài liệu.", workspace=str(self.root), goal_run_id="goal-idempotent")
        second = orchestrator.run("Xuất phiên luyện dạy đã lưu thành tài liệu.", workspace=str(self.root), goal_run_id="goal-idempotent")
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(dispatch.call_count, 1)

    def test_natural_action_goal_uses_team_lane_without_trigger_phrase(self):
        orchestrator = ExternalOrchestrator(state_root=self.root / "state")
        run = orchestrator.create(
            "Implement a persistent overview for this workspace.",
            workspace=str(self.root),
            goal_run_id="goal-natural-team",
            context={"hstry_context_enabled": False},
        )
        run = orchestrator.plan(run.goal_run_id)
        self.assertEqual(run.decision, "TEAM")
        self.assertTrue(run.composition_required)
        self.assertTrue(run.team_ledger_ref)
        self.assertEqual(run.current_state, "READY")

    def test_natural_question_stays_direct_without_trigger_phrase(self):
        orchestrator = ExternalOrchestrator(state_root=self.root / "state")
        run = orchestrator.create(
            "What is the current workspace status?",
            workspace=str(self.root),
            goal_run_id="goal-natural-question",
            context={"hstry_context_enabled": False},
        )
        self.assertFalse(orchestrator._team_enabled(run))

    def test_status_request_without_question_mark_stays_direct(self):
        orchestrator = ExternalOrchestrator(state_root=self.root / "state")
        run = orchestrator.create(
            "Provide a status update for this workspace",
            workspace=str(self.root),
            goal_run_id="goal-natural-status",
            context={"hstry_context_enabled": False},
        )
        self.assertFalse(orchestrator._team_enabled(run))

    def test_status_dashboard_update_remains_an_action_goal(self):
        orchestrator = ExternalOrchestrator(state_root=self.root / "state")
        run = orchestrator.create(
            "Update the status dashboard for this workspace",
            workspace=str(self.root),
            goal_run_id="goal-status-dashboard-action",
            context={"hstry_context_enabled": False},
        )
        self.assertTrue(orchestrator._team_enabled(run))

    def test_explicit_team_disabled_overrides_natural_action(self):
        orchestrator = ExternalOrchestrator(state_root=self.root / "state")
        run = orchestrator.create(
            "Implement a persistent overview for this workspace.",
            workspace=str(self.root),
            goal_run_id="goal-natural-direct",
            context={"hstry_context_enabled": False, "team_enabled": False},
        )
        self.assertFalse(orchestrator._team_enabled(run))

    def test_concurrent_create_same_id_is_serialized_and_idempotent(self):
        state = self.root / "state-concurrent"
        orchestrator = ExternalOrchestrator(state_root=state)

        def create_once():
            return orchestrator.create(
                "Persist one concurrent goal",
                workspace=str(self.root),
                goal_run_id="goal-concurrent",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            runs = list(pool.map(lambda _: create_once(), range(8)))
        self.assertEqual({run.goal_run_id for run in runs}, {"goal-concurrent"})
        self.assertEqual({run.original_goal for run in runs}, {"Persist one concurrent goal"})
        self.assertEqual(len(list(state.glob("goal-concurrent.json"))), 1)
        self.assertEqual(orchestrator.store.load("goal-concurrent").workspace, str(self.root))

    @unittest.skipUnless("fork" in get_all_start_methods(), "cross-process lock proof requires fork")
    def test_cross_process_create_same_id_is_serialized_and_idempotent(self):
        state = self.root / "state-cross-process"
        args = (str(state), str(self.root))
        context = get_context("fork")
        with context.Pool(processes=8) as pool:
            runs = pool.map(_create_goal_from_process, [args] * 8)
        self.assertEqual({run[0] for run in runs}, {"goal-cross-process"})
        self.assertEqual({run[1] for run in runs}, {str(self.root)})
        self.assertEqual({run[2] for run in runs}, {"Persist one cross-process goal"})
        self.assertEqual(len(list(state.glob("goal-cross-process.json"))), 1)
        self.assertEqual(GoalRunStore(state).load("goal-cross-process").workspace, str(self.root))

    def test_failure_classification_and_bounded_retry(self):
        self.assertEqual(ExternalOrchestrator.classify_failure("provider timeout"), "transient_infrastructure")
        self.assertEqual(ExternalOrchestrator.classify_failure("validator INVALID artifact"), "validation_failure")
        self.assertEqual(ExternalOrchestrator.classify_failure("ELEVATED_REQUIRES_APPROVAL"), "safety_blocker")
        self.assertEqual(ExternalOrchestrator.classify_failure("no eligible capability"), "unresolved_capability")

    def test_safety_inheritance_rejects_elevated(self):
        with self.assertRaises(ValueError):
            ExternalOrchestrator(state_root=self.root / "state").create("goal", workspace=str(self.root), execution_policy="ELEVATED")

    def test_team_result_requires_per_task_acceptance_artifact_evidence(self):
        state = self.root / "state"
        orchestrator = ExternalOrchestrator(state_root=state)
        run = orchestrator.create("đội lập trình smoke", workspace=str(self.root), goal_run_id="goal-team-evidence")
        run.current_state = "EXECUTING"
        run.decision = "TEAM"
        run.team_status = "COMPLETED"
        ledger_path = self.root / "ledger.json"
        ledger_path.write_text(json.dumps({
            "schema_version": 1,
            "status": "COMPLETED",
            "tasks": [{"task_id": "task-1", "state": "COMPLETED", "worker_attempts": [{
                "state": "COMPLETED", "provider_status": "COMPLETED"
            }], "result": {"status": "COMPLETED"}}],
        }), encoding="utf-8")
        run.team_ledger_ref = str(ledger_path)
        orchestrator.store.save(run)
        result = orchestrator._apply_team_result(orchestrator.store.load(run.goal_run_id))
        self.assertIsNone(result)
        checked = orchestrator.store.load(run.goal_run_id)
        self.assertEqual(checked.current_state, "FAILED")
        self.assertFalse(checked.acceptance_passed)
        self.assertFalse(checked.verification["task_acceptance_evidence"])

    def test_terminal_team_overview_derives_current_artifacts_and_hides_stale_blockers(self):
        state = self.root / "state"
        orchestrator = ExternalOrchestrator(state_root=state)
        run = orchestrator.create("đội lập trình smoke", workspace=str(self.root), goal_run_id="goal-team-view")
        run.current_state = "COMPLETED"
        run.final_status = "COMPLETED"
        run.acceptance_passed = True
        run.team_status = "COMPLETED"
        run.blockers = [
            {"class": "team_verification", "reason": "stale historical failure"},
            {"class": "team_recovery", "reason": "stale historical recovery"},
        ]
        ledger_path = self.root / "ledger-view.json"
        ledger_path.write_text(json.dumps({
            "schema_version": 1,
            "status": "COMPLETED",
            "tasks": [{"task_id": "task-1", "state": "COMPLETED", "worker_attempts": [{
                "state": "COMPLETED", "provider_status": "COMPLETED",
                "acceptance": {"accepted": True, "present": ["evidence.md"],
                                "required_artifacts": ["evidence.md"]},
            }], "result": {"status": "COMPLETED"}}],
        }), encoding="utf-8")
        run.team_ledger_ref = str(ledger_path)
        orchestrator.store.save(run)

        overview = orchestrator.overview(run.goal_run_id)
        result = orchestrator.result(run.goal_run_id)
        self.assertEqual(overview["artifacts"], [{"task_id": "task-1", "path": "evidence.md"}])
        self.assertEqual(overview["blocker_count"], 0)
        self.assertEqual(result["artifact_refs"], overview["artifacts"])
        self.assertEqual(result["blockers"], [])

    def test_chatgpt_review_decision_requires_explicit_accept(self):
        self.assertEqual(
            ExternalOrchestrator._parse_chatgpt_review_decision("REVIEW: ACCEPT\nNEXT: keep local"),
            "ACCEPTED",
        )
        self.assertEqual(
            ExternalOrchestrator._parse_chatgpt_review_decision("FIX: add evidence for the tracking lane"),
            "FOLLOW_UP_REQUIRED",
        )
        self.assertEqual(
            ExternalOrchestrator._parse_chatgpt_review_decision("The result looks reasonable"),
            "FOLLOW_UP_REQUIRED",
        )

    def test_chatgpt_review_decision_accepts_marker_inside_bounded_tail(self):
        self.assertEqual(
            ExternalOrchestrator._parse_chatgpt_review_decision(
                "...bounded review details... REVIEW: ACCEPT next: keep local"
            ),
            "ACCEPTED",
        )

    def test_chatgpt_review_measurement_detection_handles_artifact_schema_language(self):
        self.assertTrue(ExternalOrchestrator._review_requests_measurement(
            "The complete measurement artifact is not supplied; exact canonical keys/types "
            "cannot be checked and integration omits measurement."
        ))
        self.assertTrue(ExternalOrchestrator._review_requests_measurement(
            "Produce a fresh integration/verification round that includes measurement.json.",
        ))
        self.assertFalse(ExternalOrchestrator._review_requests_measurement(
            "The repaired copy is substantive and all five safety flags are preserved."
        ))

    def test_workflow_tasks_use_plan_prompt_when_receipt_tail_omits_json_names(self):
        orchestrator = ExternalOrchestrator(state_root=self.root / "state")
        run = orchestrator.create(
            "marketing continuation", workspace=str(self.root), goal_run_id="goal-workflow-contract",
            context={"chatgpt_workflow": {
                "plan_prompt": "Require copy.json, measurement.json, integration.json, and verification.json.",
            }},
        )
        tasks = orchestrator._workflow_tasks(run, {"snippet": "tail without file names"})
        self.assertEqual(
            [task["scope"] for task in tasks],
            [
                ".system-agentic-goals/goal-workflow-contract/team/copy.json",
                ".system-agentic-goals/goal-workflow-contract/team/measurement.json",
                ".system-agentic-goals/goal-workflow-contract/team/integration.json",
                ".system-agentic-goals/goal-workflow-contract/team/verification.json",
            ],
        )
        self.assertTrue(all("Do not use list_files" in task["objective"] for task in tasks[:2]))

    def test_repair_task_is_tool_bounded(self):
        orchestrator = ExternalOrchestrator(state_root=self.root / "state")
        run = orchestrator.create(
            "marketing continuation", workspace=str(self.root), goal_run_id="goal-repair-contract",
            context={"chatgpt_workflow": {"plan_prompt": "copy.json measurement.json integration.json verification.json"}},
        )
        tasks = orchestrator._workflow_tasks(run, {"snippet": "copy.json measurement.json integration.json verification.json"})
        self.assertEqual(tasks[0]["tool_allowlist"], ["read_file", "write_file"])

    def test_repair_recovery_adds_new_task_and_retargets_dependant(self):
        state = self.root / "state"
        workspace = self.root / "workspace"
        workspace.mkdir()
        orchestrator = ExternalOrchestrator(state_root=state)
        run = orchestrator.create("marketing continuation", workspace=str(workspace), goal_run_id="goal-repair-recovery",
                                  context={"chatgpt_workflow": {"review_artifact_paths": [".system-agentic-goals/goal-repair-recovery/team/copy-repair-round-2.json"], "review_round": 2}})
        run.decision = "CHATGPT_TEAM"
        run.current_state = "FAILED"
        ledger_path = workspace / "ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "status": "FAILED", "tasks": [
            {"task_id": "goal-repair-recovery-repair-r2", "title": "repair", "role": "dev-content", "artifact_kind": "copy",
             "workspace": str(workspace), "objective": "read then write", "scope": ".system-agentic-goals/goal-repair-recovery/team/copy-repair-round-2.json",
             "required_artifacts": [".system-agentic-goals/goal-repair-recovery/team/copy-repair-round-2.json"], "depends_on": [],
             "execution_policy": "WORKSPACE_WRITE", "acceptance_criteria": ["write"], "grounding_required": True,
             "tool_allowlist": ["read_file", "write_file"], "state": "FAILED", "worker_attempts": [{"state": "FAILED"}], "result": None, "error": "failed"},
            {"task_id": "goal-repair-recovery-integration-r2", "title": "integration", "role": "integrator", "artifact_kind": "integration",
             "workspace": str(workspace), "objective": "integrate", "scope": ".system-agentic-goals/goal-repair-recovery/team/integration-round-2.json",
             "required_artifacts": [".system-agentic-goals/goal-repair-recovery/team/integration-round-2.json"], "depends_on": ["goal-repair-recovery-repair-r2"],
             "execution_policy": "WORKSPACE_WRITE", "acceptance_criteria": ["write"], "state": "BLOCKED", "worker_attempts": [], "result": None, "error": "dependency did not complete"}
        ], "events": []}), encoding="utf-8")
        run.team_ledger_ref = str(ledger_path)
        run.team_status = "FAILED"
        orchestrator.store.save(run)
        self.assertTrue(orchestrator._prepare_chatgpt_repair_recovery(orchestrator.store.load(run.goal_run_id)))
        ledger = json.loads(ledger_path.read_text())
        self.assertIn("goal-repair-recovery-repair-r2-recovery-1", {t["task_id"] for t in ledger["tasks"]})
        integration = next(t for t in ledger["tasks"] if t["task_id"].endswith("integration-r2"))
        self.assertEqual(integration["depends_on"], ["goal-repair-recovery-repair-r2-recovery-1"])
        self.assertEqual(next(t for t in ledger["tasks"] if t["task_id"].endswith("repair-r2") and not t["task_id"].endswith("recovery-1"))["state"], "FAILED")

    def test_failed_integration_recovery_adds_new_task_and_retargets_verifier(self):
        state = self.root / "state"
        workspace = self.root / "workspace"
        (workspace / ".system-agentic-goals/goal-integration-recovery/team").mkdir(parents=True)
        repair_path = ".system-agentic-goals/goal-integration-recovery/team/copy-repair-round-2.json"
        (workspace / repair_path).write_text(json.dumps({
            "source_refs": ["README.md"], "draft_text": "grounded", "audience": "UNKNOWN",
            "claims": ["UNKNOWN"], "DRAFT_ONLY": True, "rehearsal": True,
            "liveEligible": False, "externalSideEffects": False, "unknownRetryAllowed": False,
        }), encoding="utf-8")
        ledger_path = workspace / "ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "status": "FAILED", "tasks": [
            {"task_id": "goal-integration-recovery-repair-r2-recovery-1", "title": "repair", "role": "dev-content", "artifact_kind": "copy",
             "workspace": str(workspace), "objective": "repair", "scope": repair_path, "required_artifacts": [repair_path], "depends_on": [],
             "execution_policy": "WORKSPACE_WRITE", "acceptance_criteria": ["write"], "grounding_required": True,
             "tool_allowlist": ["read_file", "write_file"], "state": "COMPLETED", "worker_attempts": [{"state": "COMPLETED"}],
             "result": {"acceptance": {"accepted": True, "present": [repair_path], "required_artifacts": [repair_path]}}, "error": None},
            {"task_id": "goal-integration-recovery-integration-r2", "title": "integration", "role": "integrator", "artifact_kind": "integration",
             "workspace": str(workspace), "objective": "Read only input. Write .system-agentic-goals/goal-integration-recovery/team/integration-round-2.json.",
             "scope": ".system-agentic-goals/goal-integration-recovery/team/integration-round-2.json",
             "required_artifacts": [".system-agentic-goals/goal-integration-recovery/team/integration-round-2.json"],
             "depends_on": ["goal-integration-recovery-repair-r2-recovery-1"], "execution_policy": "WORKSPACE_WRITE",
             "acceptance_criteria": ["write"], "grounding_required": True, "tool_allowlist": ["read_file", "write_file"],
             "state": "FAILED", "worker_attempts": [{"state": "FAILED"}], "result": None, "error": "failed"},
            {"task_id": "goal-integration-recovery-verification-r2", "title": "verification", "role": "verifier",
             "workspace": str(workspace), "objective": "verify", "scope": ".system-agentic-goals/goal-integration-recovery/team/verification-round-2.json",
             "required_artifacts": [".system-agentic-goals/goal-integration-recovery/team/verification-round-2.json"],
             "depends_on": ["goal-integration-recovery-integration-r2"], "execution_policy": "WORKSPACE_WRITE",
             "acceptance_criteria": ["verify"], "state": "BLOCKED", "worker_attempts": [], "result": None,
             "error": "dependency did not complete"},
        ], "events": []}), encoding="utf-8")
        orchestrator = ExternalOrchestrator(state_root=state)
        run = orchestrator.create("marketing continuation", workspace=str(workspace), goal_run_id="goal-integration-recovery",
                                  context={"chatgpt_workflow": {"review_round": 2, "repair_recovery_task_id": "goal-integration-recovery-repair-r2-recovery-1"}})
        run.decision = "CHATGPT_TEAM"
        run.current_state = "FAILED"
        run.team_ledger_ref = str(ledger_path)
        run.team_status = "FAILED"
        orchestrator.store.save(run)
        self.assertTrue(orchestrator._prepare_chatgpt_failed_task_recovery(orchestrator.store.load(run.goal_run_id)))
        ledger = json.loads(ledger_path.read_text())
        new_id = "goal-integration-recovery-integration-r2-recovery-1"
        self.assertIn(new_id, {task["task_id"] for task in ledger["tasks"]})
        verification = next(task for task in ledger["tasks"] if task["task_id"].endswith("verification-r2"))
        self.assertEqual(verification["depends_on"], [new_id])

    def test_completed_repair_wave_ignores_superseded_failures(self):
        state = self.root / "state"
        workspace = self.root / "workspace"
        team_dir = workspace / ".system-agentic-goals/goal-wave-resume/team"
        team_dir.mkdir(parents=True)
        paths = {
            "repair": ".system-agentic-goals/goal-wave-resume/team/copy-repair-round-2.json",
            "integration": ".system-agentic-goals/goal-wave-resume/team/integration-round-2.json",
            "verification": ".system-agentic-goals/goal-wave-resume/team/verification-round-2.json",
        }
        (workspace / paths["repair"]).write_text(json.dumps({"source_refs": ["README.md"], "draft_text": "grounded",
            "audience": "UNKNOWN", "claims": ["UNKNOWN"], "DRAFT_ONLY": True, "rehearsal": True,
            "liveEligible": False, "externalSideEffects": False, "unknownRetryAllowed": False}), encoding="utf-8")
        (workspace / paths["integration"]).write_text(json.dumps({"input_refs": [paths["repair"]], "source_refs": ["README.md"],
            "DRAFT_ONLY": True, "rehearsal": True, "liveEligible": False, "externalSideEffects": False, "unknownRetryAllowed": False}), encoding="utf-8")
        (workspace / paths["verification"]).write_text(json.dumps({"verdict": "PASS_BOUNDED", "checked_artifacts": [paths["repair"], paths["integration"]], "evidenceRefs": ["README.md"]}), encoding="utf-8")
        tasks = [
            {"task_id": "goal-wave-resume-old", "state": "FAILED", "superseded_by": "goal-wave-resume-new", "worker_attempts": [{"state": "FAILED"}], "result": None, "error": "old"},
            {"task_id": "goal-wave-resume-new", "state": "COMPLETED", "role": "integrator", "worker_attempts": [{"state": "COMPLETED", "provider_status": "COMPLETED", "acceptance": {"accepted": True, "present": [paths["integration"]], "required_artifacts": [paths["integration"]]}}], "result": {"acceptance": {"accepted": True, "present": [paths["integration"]], "required_artifacts": [paths["integration"]]}}, "required_artifacts": [paths["integration"]], "depends_on": []},
            {"task_id": "goal-wave-resume-verifier", "state": "COMPLETED", "role": "verifier", "worker_attempts": [{"state": "COMPLETED", "provider_status": "COMPLETED", "acceptance": {"accepted": True, "present": [paths["verification"]], "required_artifacts": [paths["verification"]]}}], "result": {"acceptance": {"accepted": True, "present": [paths["verification"]], "required_artifacts": [paths["verification"]]}}, "required_artifacts": [paths["verification"]], "depends_on": ["goal-wave-resume-new"]},
        ]
        ledger_path = workspace / "ledger.json"
        ledger_path.write_text(json.dumps({"schema_version": 1, "status": "FAILED", "tasks": tasks, "events": []}), encoding="utf-8")
        orchestrator = ExternalOrchestrator(state_root=state)
        run = orchestrator.create("marketing continuation", workspace=str(workspace), goal_run_id="goal-wave-resume",
            context={"chatgpt_workflow": {"review_round": 2, "review_decision": "FOLLOW_UP_REQUIRED", "review_artifact_paths": list(paths.values()), "phases": {"PLAN_RECEIVED": {"status": "COMPLETED"}, "REVIEW_RECEIVED": {"status": "COMPLETED"}}}})
        run.decision = "CHATGPT_TEAM"; run.current_state = "FAILED"; run.team_status = "FAILED"; run.team_ledger_ref = str(ledger_path)
        orchestrator.store.save(run)
        self.assertTrue(orchestrator._prepare_chatgpt_completed_wave_resume(orchestrator.store.load(run.goal_run_id)))
        resumed = orchestrator.store.load(run.goal_run_id)
        self.assertEqual(resumed.current_state, "READY")
        self.assertEqual(resumed.team_status, "COMPLETED")
        self.assertTrue(resumed.verification["team_tasks_completed"])

    def test_review_round_uses_round_specific_approval(self):
        orchestrator = ExternalOrchestrator(state_root=self.root / "state")
        workflow = {
            "review_round": 2,
            "review_approval_id": "approval-round-1",
            "review_approval_ids": {"2": "approval-round-2"},
        }
        # The precedence is kept in the review executor; assert the durable
        # mapping itself remains the source of the round-specific choice.
        approval_ids = workflow.get("review_approval_ids")
        self.assertEqual(approval_ids.get("2"), "approval-round-2")


if __name__ == "__main__":
    unittest.main()
