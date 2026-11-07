from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from modules.external_orchestrator import ExternalOrchestrator
from modules.orchestrator_service import OrchestratorService, ServiceConfig, ServiceLock, service_request
from modules.test_external_orchestrator import blueprint


class OrchestratorServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="orchestrator-service-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def _service(self, dispatch=None):
        dispatch = dispatch or Mock(return_value={"status": "COMPLETED", "execution_status": "COMPLETED",
            "domain_outcome": "ARTIFACT_EXPORTED", "provider": "local-qwen", "artifacts": [{"path": "x"}]})
        orch = ExternalOrchestrator(state_root=self.root / "state", dispatch=dispatch,
            composer=lambda *a, **k: blueprint("goal-service"))
        return OrchestratorService(ServiceConfig(self.root / "state", port=0), orchestrator=orch), dispatch

    def _wait(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.assertTrue(predicate())

    def test_lock_is_single_instance(self):
        first = ServiceLock(self.root / "lock")
        second = ServiceLock(self.root / "lock")
        first.acquire()
        self.addCleanup(first.release)
        with self.assertRaises(RuntimeError):
            second.acquire()

    def test_lock_release_is_idempotent_under_concurrent_cleanup(self):
        lock = ServiceLock(self.root / "lock-race")
        lock.acquire()
        errors = []

        def release():
            try:
                lock.release()
            except Exception as exc:  # pragma: no cover - assertion captures regressions
                errors.append(exc)

        threads = [threading.Thread(target=release) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertIsNone(lock.handle)

    def test_submit_persists_lease_and_completes_without_caller(self):
        service, dispatch = self._service()
        service.start()
        self.addCleanup(service.shutdown)
        receipt = service.submit({"goal": "Xuất phiên luyện dạy thành tài liệu", "workspace": str(self.root)})
        self._wait(lambda: service.store.load(receipt["goal_run_id"]).current_state == "COMPLETED")
        result = service.result(receipt["goal_run_id"])
        self.assertTrue(result["acceptance_passed"])
        self.assertEqual(dispatch.call_count, 1)
        self.assertTrue((self.root / "state" / f'{receipt["goal_run_id"]}.json').exists())

    def test_health_is_local_and_reports_registries(self):
        service, _ = self._service()
        service.start()
        self.addCleanup(service.shutdown)
        health = service.health()
        self.assertTrue(health["service_running"])
        self.assertTrue(health["bind"]["local_only"])
        self.assertTrue(health["state_store_accessible"])

    def test_health_is_responsive_while_live_discovery_is_in_flight(self):
        service, _ = self._service()
        service.server = object()
        service._health_refresh_inflight = True
        started = time.monotonic()
        health = service.health()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertEqual(health["health"], "PASS")
        self.assertEqual(health["live_discovery"], "PENDING")

    def test_loopback_http_transport_exposes_health_and_inspect(self):
        service, _ = self._service()
        service.start()
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(service.shutdown)
        config = ServiceConfig(self.root / "state", port=service.server.server_port)
        health = service_request("/health", config=config)
        self.assertTrue(health["service_running"])
        submitted = service_request("/goals", method="POST", payload={
            "goal": "Build a quantum teleportation simulator with a certified runtime.",
            "workspace": str(self.root), "goal_run_id": "goal-http-unresolved",
            "context": {"team_enabled": False}}, config=config)
        self.assertEqual(submitted["goal_run_id"], "goal-http-unresolved")
        self._wait(lambda: service.store.load("goal-http-unresolved").current_state == "UNRESOLVED")
        inspected = service_request("/goals/goal-http-unresolved", config=config)
        self.assertEqual(inspected["current_state"], "UNRESOLVED")

    def test_startup_recovers_ready_goal(self):
        service, dispatch = self._service()
        run = service.orchestrator.create("Xuất phiên luyện dạy thành tài liệu", workspace=str(self.root), goal_run_id="goal-recovery")
        service.orchestrator.plan(run.goal_run_id)
        service.start()
        self.addCleanup(service.shutdown)
        self._wait(lambda: service.store.load("goal-recovery").current_state == "COMPLETED")
        self.assertEqual(dispatch.call_count, 1)
        self.assertTrue(service.recovery_events)

    def test_inspect_exposes_persisted_capability_gap(self):
        service, _ = self._service()
        service.start()
        self.addCleanup(service.shutdown)
        receipt = service.submit({"goal": "Build a quantum teleportation simulator with a certified runtime.",
                                  "workspace": str(self.root), "goal_run_id": "goal-service-gap",
                                  "context": {"team_enabled": False}})
        self._wait(lambda: service.store.load(receipt["goal_run_id"]).current_state == "UNRESOLVED")
        result = service.result(receipt["goal_run_id"])
        self.assertEqual(result["gap_classification"], "TRUE_CAPABILITY_GAP")
        self.assertTrue(result["capability_blueprint_ref"])
        self.assertEqual(result["execution_refs"], [])

    def test_restart_does_not_rerun_terminal_goal(self):
        service, dispatch = self._service()
        service.start()
        receipt = service.submit({"goal": "Xuất phiên luyện dạy thành tài liệu", "workspace": str(self.root), "goal_run_id": "goal-stable"})
        self._wait(lambda: service.store.load(receipt["goal_run_id"]).current_state == "COMPLETED")
        service.shutdown()
        second, _ = self._service(dispatch=dispatch)
        second.start()
        self.addCleanup(second.shutdown)
        time.sleep(.15)
        self.assertEqual(dispatch.call_count, 1)

    def test_reconciled_ready_goal_continues_with_current_owner(self):
        service, _ = self._service()
        run = service.orchestrator.create("Xuất phiên luyện dạy thành tài liệu", workspace=str(self.root),
                                          goal_run_id="goal-reconcile-ready")
        service.orchestrator.plan(run.goal_run_id)
        run = service.store.load(run.goal_run_id)
        run.transition("EXECUTING", reason="test persisted provider task")
        service.store.save(run)

        def reconcile_to_ready(current):
            current.transition("READY", reason="test provider task reconciled")
            service.store.save(current)

        service._reconcile_or_block = reconcile_to_ready
        service.orchestrator.execute = Mock()
        service._execute_one(run.goal_run_id)

        service.orchestrator.execute.assert_called_once_with(run.goal_run_id)
        self.assertEqual(service.store.load(run.goal_run_id).current_state, "READY")

    def test_terminal_team_ledger_cannot_leave_goal_executing(self):
        service, _ = self._service()
        run = service.orchestrator.create(
            "marketing team recovery", workspace=str(self.root), goal_run_id="goal-terminal-team"
        )
        run.transition("ANALYZING", reason="test")
        run.transition("PLANNED", reason="test")
        run.transition("READY", reason="test")
        run.transition("EXECUTING", reason="test persisted terminal team")
        run.decision = "CHATGPT_TEAM"
        ledger_path = self.root / "team" / "ledger.json"
        ledger_path.parent.mkdir(parents=True)
        ledger_path.write_text(json.dumps({
            "schema_version": 1,
            "team_run_id": "team-goal-terminal-team",
            "status": "FAILED",
            "tasks": [{
                "task_id": "worker-1", "state": "FAILED", "worker_attempts": [],
                "result": None, "error": "worker rejected"
            }],
        }), encoding="utf-8")
        run.team_ledger_ref = str(ledger_path)
        service.store.save(run)

        service._resume_waiting_teams()

        reconciled = service.store.load(run.goal_run_id)
        self.assertEqual(reconciled.current_state, "FAILED")
        self.assertEqual(reconciled.team_status, "FAILED")
        self.assertEqual(reconciled.team_summary["task_states"], {"worker-1": "FAILED"})

    def test_non_loopback_bind_is_rejected(self):
        service, _ = self._service()
        service.config.host = "0.0.0.0"
        with self.assertRaises(ValueError):
            service.start()

    def test_service_request_uses_bounded_health_timeout(self):
        service, _ = self._service()
        with patch("modules.orchestrator_service.urllib.request.urlopen") as opened:
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = b'{"service_running": true}'
            opened.return_value = response
            result = service_request("/health", config=ServiceConfig(self.root / "state"))
        self.assertTrue(result["service_running"])
        self.assertEqual(opened.call_args.kwargs["timeout"], 35)


if __name__ == "__main__":
    unittest.main()
