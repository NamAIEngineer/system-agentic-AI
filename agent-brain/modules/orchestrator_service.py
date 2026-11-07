"""Local persistent service owner for the bounded External Orchestrator.

The service deliberately contains no planning or module execution logic.  It
owns the process boundary, local transport, leases, recovery, and normalized
GoalRun access; :class:`ExternalOrchestrator` remains the execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import fcntl
import json
import os
from pathlib import Path
import socket
import signal
import tempfile
import urllib.error
import urllib.request
import threading
import time
from typing import Any
import uuid

from modules.external_orchestrator import ExternalOrchestrator, GoalRun, GoalRunStore, _default_state_root
from modules.runtime import load_module_registry, module_inventory
from modules.team_ledger import TeamLedgerStore, TeamRunner
from providers.runtime import ProviderTask, adapter_for, discover_providers, load_registry
from providers.dispatch import reconcile_task, task_from_spec


SERVICE_SCHEMA = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8879
DEFAULT_CLIENT_TIMEOUT_SECONDS = 35
HEALTH_REFRESH_INTERVAL_SECONDS = 60.0


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


class ServiceLock:
    """Single-machine service lock held for the entire process lifetime."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None
        self._release_lock = threading.Lock()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("SYSTEM_AGENTIC_AI service is already running") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(json.dumps({"pid": os.getpid(), "acquired_at": time.time()}))
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def release(self) -> None:
        # SIGTERM cleanup and serve_forever's finally block can race.  Claim
        # the handle under a small local lock so release remains idempotent
        # without attempting to unlock an already-closed descriptor.
        with self._release_lock:
            if self.handle is None:
                return
            handle = self.handle
            self.handle = None
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                # A repeated shutdown or an already-closed descriptor must not
                # turn a clean service stop into a lifecycle error.
                pass
            finally:
                try:
                    handle.close()
                except (OSError, ValueError):
                    pass


@dataclass
class ServiceConfig:
    state_root: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT


class OrchestratorService:
    """A bounded one-worker local service around ExternalOrchestrator."""

    def __init__(self, config: ServiceConfig, *, orchestrator: ExternalOrchestrator | None = None):
        self.config = config
        self.config.state_root.mkdir(parents=True, exist_ok=True)
        self.store = GoalRunStore(self.config.state_root)
        self.orchestrator = orchestrator or ExternalOrchestrator(state_root=self.config.state_root)
        self.lock = ServiceLock(self.config.state_root / "service.lock")
        self.instance_id = f"service-{uuid.uuid4().hex[:12]}"
        self.server: ThreadingHTTPServer | None = None
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._serving = False
        self._active: set[str] = set()
        self._active_lock = threading.Lock()
        self._health_cache: dict[str, Any] | None = None
        self._health_cache_at = 0.0
        self._health_refresh_inflight = False
        self._health_lock = threading.Lock()
        self.recovery_events: list[dict[str, Any]] = []

    def start(self) -> None:
        if self.config.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("External Orchestrator service only permits loopback binding")
        self.lock.acquire()
        self.store.audit({"event": "service_start", "instance_id": self.instance_id, "pid": os.getpid()})
        self._write_service_info("RUNNING")
        self._recover_startup()
        self.server = ThreadingHTTPServer((self.config.host, self.config.port), self._handler())
        self.server.daemon_threads = True
        self._write_service_info("RUNNING", bound_port=self.server.server_port)
        self._worker = threading.Thread(target=self._worker_loop, name="system-agentic-orchestrator", daemon=True)
        self._worker.start()
        # Live module/provider probes can include MCP startup and model health
        # checks. Keep them off the HTTP request path so hooks and overview
        # calls remain responsive while the snapshot refreshes in background.
        self._request_health_refresh()

    def serve_forever(self) -> None:
        if self.server is None:
            self.start()
        assert self.server is not None
        self._serving = True
        try:
            self.server.serve_forever(poll_interval=0.1)
        finally:
            self._serving = False
            self.shutdown()

    def shutdown(self) -> None:
        # A clean stop drains bounded goal workers.  A hard process death still
        # exercises startup reconciliation; clean shutdown must not interrupt a
        # provider response and turn a valid goal into an avoidable failure.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self._active_lock:
                active = bool(self._active)
            if not active:
                break
            time.sleep(0.05)
        self._stop.set()
        server = self.server
        self.server = None
        if server is not None:
            if self._serving:
                server.shutdown()
            server.server_close()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2)
        self._write_service_info("STOPPED")
        self.store.audit({"event": "service_stop", "instance_id": self.instance_id, "pid": os.getpid()})
        self.lock.release()

    def _write_service_info(self, status: str, **extra: Any) -> None:
        path = self.config.state_root / "service.json"
        payload = {"schema": SERVICE_SCHEMA, "status": status, "instance_id": self.instance_id,
                   "pid": os.getpid(), "host": self.config.host, "port": extra.pop("bound_port", self.config.port),
                   "updated_at": time.time(), **extra}
        # A daemon HTTP thread can enter its finally/shutdown path after a
        # test or supervisor has already removed a disposable state root. The
        # service is already stopped in that case; do not recreate user-owned
        # temporary state or leak a traceback from the late cleanup.
        try:
            fd, tmp_name = tempfile.mkstemp(prefix=".service.", suffix=".tmp", dir=self.config.state_root)
        except FileNotFoundError:
            return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        goal = payload.get("goal")
        workspace = payload.get("workspace") or os.getcwd()
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal must be a non-empty string")
        if not isinstance(workspace, str):
            raise ValueError("workspace must be a path string")
        run = self.orchestrator.create(goal, workspace=workspace,
            execution_policy=payload.get("execution_policy", "WORKSPACE_WRITE"),
            context=payload.get("context") or {}, provider_policy=payload.get("provider_policy") or {},
            goal_run_id=payload.get("goal_run_id"))
        if payload.get("defer_execution") is not True:
            self._queue(run.goal_run_id)
        else:
            run = self.orchestrator.plan(run.goal_run_id)
        return {"goal_run_id": run.goal_run_id, "current_state": run.current_state,
                "service_instance": self.instance_id, "queued": payload.get("defer_execution") is not True}

    def inspect(self, goal_run_id: str) -> dict[str, Any]:
        run = self.store.load(goal_run_id)
        return self.orchestrator.result(goal_run_id)

    def result(self, goal_run_id: str) -> dict[str, Any]:
        return self.orchestrator.result(goal_run_id)

    def overview(self, goal_run_id: str) -> dict[str, Any]:
        return self.orchestrator.overview(goal_run_id)

    def resume(self, goal_run_id: str) -> dict[str, Any]:
        run = self.store.load(goal_run_id)
        repair_recovered = False
        measurement_contract_recovered = False
        verification_task_recovered = False
        chatgpt_phase_recovered = False
        chatgpt_plan_recovered = False
        review_extension_recovered = False
        if run.decision == "CHATGPT_TEAM" and run.current_state in {"FAILED", "BLOCKED", "COMPLETED"}:
            chatgpt_plan_recovered = self.orchestrator._prepare_chatgpt_plan_phase_recovery(run)
            run = self.store.load(goal_run_id)
            measurement_contract_recovered = self.orchestrator._prepare_chatgpt_measurement_contract_recovery(run)
            run = self.store.load(goal_run_id)
            # An interactive continuation may explicitly extend the bounded
            # review budget after a fresh REVIEW: FIX receipt. This mints a
            # new round key and worker wave; prior bridge requests remain
            # immutable and are never retried.
            review_extension_recovered = self.orchestrator._prepare_chatgpt_review_extension(run)
            run = self.store.load(goal_run_id)
            verification_task_recovered = self.orchestrator._prepare_chatgpt_verification_task_recovery(run)
            run = self.store.load(goal_run_id)
            repair_recovered = self.orchestrator._prepare_chatgpt_repair_recovery(run)
            run = self.store.load(goal_run_id)
            if not repair_recovered:
                repair_recovered = self.orchestrator._prepare_chatgpt_failed_task_recovery(run)
                run = self.store.load(goal_run_id)
            if not repair_recovered:
                repair_recovered = self.orchestrator._prepare_chatgpt_completed_wave_resume(run)
                run = self.store.load(goal_run_id)
            if not repair_recovered:
                repair_recovered = self.orchestrator._prepare_chatgpt_team_wave_recovery(run)
                run = self.store.load(goal_run_id)
        # An explicit operator resume may repair a browser-boundary failure
        # even when the latest completed ChatGPT phase is the same regular
        # conversation. The helper performs a fresh AX preflight first, mints
        # a new idempotency key, and never reuses the failed request.
        if (run.decision == "CHATGPT_TEAM" and run.current_state == "VERIFYING"
                and not repair_recovered):
            chatgpt_phase_recovered = self.orchestrator._prepare_chatgpt_review_phase_recovery(
                run, allow_same_conversation=True
            )
            run = self.store.load(goal_run_id)
        recovered = self._prepare_team_resume(run)
        chatgpt_recovered = False
        if run.current_state == "FAILED" and not recovered:
            chatgpt_recovered = self.orchestrator._prepare_chatgpt_review_recovery(run)
            run = self.store.load(goal_run_id)
        if run.current_state in {"FAILED", "BLOCKED", "COMPLETED"} and not (recovered or chatgpt_recovered or repair_recovered or measurement_contract_recovered or chatgpt_plan_recovered):
            raise ValueError("GoalRun is terminal and has no recoverable persisted team task")
        if (run.current_state == "VERIFYING" and run.decision == "CHATGPT_TEAM"
                and not (recovered or chatgpt_recovered or chatgpt_phase_recovered or chatgpt_plan_recovered)):
            # A completed repair wave can leave the GoalRun in VERIFYING with
            # the next review approval already persisted but no round-N
            # receipt yet. Prove the current regular ChatGPT conversation
            # through a fresh stable browser snapshot, then queue the exact
            # approved review key. This does not mint or resend a new key.
            workflow = run.context.get("chatgpt_workflow") if isinstance(run.context, dict) else None
            review_round = int(workflow.get("review_round", 1) or 1) if isinstance(workflow, dict) else 1
            phases = workflow.get("phases", {}) if isinstance(workflow, dict) else {}
            review_phase = "REVIEW_RECEIVED" if review_round == 1 else f"REVIEW_RECEIVED_{review_round}"
            pending_review = isinstance(workflow, dict) and isinstance(workflow.get("review_artifact_paths"), list)
            # A failed/unavailable bridge attempt is a pending review, not a
            # completed phase.  Older runs can persist the phase key before
            # returning a route or browser error (for example the regular
            # first route followed by an unavailable Work fallback).  Only a
            # durable COMPLETED receipt is terminal here; keep failed and
            # unavailable receipts immutable, then queue the approved review
            # key after the fresh browser preflight below.
            existing_review = phases.get(review_phase) if isinstance(phases, dict) else None
            review_pending = (
                review_phase not in phases
                or not isinstance(existing_review, dict)
                or existing_review.get("status") != "COMPLETED"
            )
            if (pending_review and run.team_status == "COMPLETED"
                    and isinstance(phases, dict) and review_pending):
                conversation_id = str(workflow.get("conversation_id") or "")
                if conversation_id:
                    try:
                        from modules.chatgpt_browser_bridge import OrcaChromeTransport
                        snapshot = OrcaChromeTransport(timeout=15).stable_snapshot(conversation_id)
                    except (OSError, RuntimeError, ValueError):
                        raise ValueError("CHATGPT_REVIEW_REQUIRES_FRESH_BROWSER_PREFLIGHT")
                    if snapshot.conversation_id != conversation_id:
                        raise ValueError("CHATGPT_REVIEW_REQUIRES_FRESH_BROWSER_PREFLIGHT")
                    self._queue(goal_run_id)
                    return {"goal_run_id": goal_run_id, "queued": True,
                            "recovered_chatgpt_pending_review": True,
                            "review_round": review_round,
                            "conversation_id": conversation_id,
                            "service_instance": self.instance_id}
            # Terminal bridge failures require an explicit fresh browser
            # preflight. Never requeue the same failed exchange from a stale
            # service call.
            raise ValueError("CHATGPT_REVIEW_REQUIRES_FRESH_BROWSER_PREFLIGHT")
        self._queue(goal_run_id)
        return {"goal_run_id": goal_run_id, "queued": True,
                "recovered_team_task": recovered, "recovered_chatgpt_review": chatgpt_recovered,
                "recovered_chatgpt_repair": repair_recovered,
                "recovered_chatgpt_measurement_contract": measurement_contract_recovered,
                "recovered_chatgpt_verification_task": verification_task_recovered,
                "recovered_chatgpt_plan": chatgpt_plan_recovered,
                "recovered_chatgpt_phase": chatgpt_phase_recovered,
                "recovered_chatgpt_review_extension": review_extension_recovered,
                "service_instance": self.instance_id}

    def health(self) -> dict[str, Any]:
        state_ok = self.config.state_root.is_dir() and os.access(self.config.state_root, os.W_OK)
        try:
            module_registry_data = load_module_registry()
            module_registry = True
        except (OSError, ValueError, RuntimeError):
            module_registry_data = {"modules": []}
            module_registry = False
        try:
            provider_registry_data = load_registry()
            provider_registry = True
        except (OSError, ValueError, RuntimeError):
            provider_registry_data = {"providers": []}
            provider_registry = False
        self._request_health_refresh()
        with self._health_lock:
            snapshot = dict(self._health_cache or {})
            snapshot_age = (time.monotonic() - self._health_cache_at) if self._health_cache_at else None
        try:
            load_module_registry()
            registry_integrity = True
            registry_reason = snapshot.get("error")
        except (OSError, ValueError, RuntimeError) as exc:
            registry_integrity = False
            registry_reason = str(exc)
        with self._active_lock:
            active = len(self._active)
        info = {}
        try:
            info = json.loads((self.config.state_root / "service.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        modules = snapshot.get("modules") if isinstance(snapshot.get("modules"), list) else []
        providers = snapshot.get("providers") if isinstance(snapshot.get("providers"), list) else []
        module_count = len(modules) or len(module_registry_data.get("modules", []))
        provider_count = len(providers) or len(provider_registry_data.get("providers", []))
        health_ok = bool(self.server is not None and state_ok)
        readiness_ok = bool(health_ok and module_registry and registry_integrity and provider_registry)
        return {"service_running": self.server is not None, "service_instance": self.instance_id,
                "pid": os.getpid(), "version": SERVICE_SCHEMA, "state_store_accessible": state_ok,
                "module_registry_readable": module_registry, "provider_registry_readable": provider_registry,
                "registry_integrity": registry_integrity, "registry_reason": registry_reason,
                "health": "PASS" if health_ok else "FAIL", "readiness": "READY" if readiness_ok else "DEGRADED",
                "execution_backend": "delegated to existing provider/runtime adapters",
                "active_goal_count": active, "module_count": module_count, "provider_count": provider_count,
                "live_discovery": "READY" if snapshot else "PENDING",
                "live_discovery_age_seconds": round(snapshot_age, 3) if snapshot_age is not None else None,
                "bind": {"host": self.config.host, "port": info.get("port", self.config.port), "local_only": self.config.host in {"127.0.0.1", "localhost", "::1"}},
                "recovery_events": list(self.recovery_events)}

    def _request_health_refresh(self) -> None:
        with self._health_lock:
            age = time.monotonic() - self._health_cache_at if self._health_cache_at else None
            if self._health_refresh_inflight or (age is not None and age < HEALTH_REFRESH_INTERVAL_SECONDS):
                return
            self._health_refresh_inflight = True

        def refresh() -> None:
            snapshot: dict[str, Any] = {}
            try:
                snapshot["modules"] = module_inventory()
                snapshot["providers"] = discover_providers()
            except (OSError, ValueError, RuntimeError) as exc:
                snapshot["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                with self._health_lock:
                    self._health_cache = snapshot
                    self._health_cache_at = time.monotonic()
                    self._health_refresh_inflight = False

        threading.Thread(target=refresh, name="system-agentic-health-refresh", daemon=True).start()

    def doctor(self) -> dict[str, Any]:
        """Read-only readiness diagnosis; never repairs state."""
        health = self.health()
        checks = {
            "service": "PASS" if health["service_running"] else "FAIL",
            "state_store": "PASS" if health["state_store_accessible"] else "FAIL",
            "registry": "PASS" if health["registry_integrity"] else "FAIL",
            "provider_registry": "PASS" if health["provider_registry_readable"] else "FAIL",
            "module_inventory": "PASS" if health["module_registry_readable"] else "FAIL",
            "local_transport": "PASS" if health["bind"]["local_only"] else "FAIL",
        }
        overall = "PASS" if all(value == "PASS" for value in checks.values()) else ("DEGRADED" if checks["service"] == "PASS" else "FAIL")
        return {"status": overall, "readiness": health["readiness"], "checks": checks,
                "registry_reason": health.get("registry_reason"), "instance": self.instance_id,
                "pid": os.getpid(), "state_root": str(self.config.state_root)}

    def _queue(self, goal_run_id: str) -> None:
        with self._active_lock:
            if goal_run_id in self._active:
                return
            self._active.add(goal_run_id)
        threading.Thread(target=self._execute_one, args=(goal_run_id,), daemon=True).start()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            # Requeue team GoalRuns whose controller timed out while a durable
            # provider task remained active. TeamRunner recovery observes the
            # same task ID; it never creates a duplicate attempt.
            self._resume_waiting_teams()
            time.sleep(1.0)

    def _resume_waiting_teams(self) -> None:
        for path in sorted(self.config.state_root.glob("*.json")):
            if path.name in {"service.json"} or path.name.endswith(".result.json"):
                continue
            try:
                run = self.store.load(path.stem)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if run.execution_owner is not None or not run.team_ledger_ref:
                # ChatGPT plan/review phases do not have a team ledger yet.
                # They still have a durable bridge idempotency key and can be
                # resumed by polling the same request; never submit a new one.
                if run.execution_owner is not None or run.decision != "CHATGPT_TEAM":
                    continue
                reason = str((run.blockers or [{}])[-1].get("reason", ""))
                pending = run.context.get("chatgpt_workflow", {}).get("pending_phases", {}) if isinstance(run.context.get("chatgpt_workflow"), dict) else {}
                recoverable_failure = reason in {"CHATGPT_PLAN_RECEIVED_RUNNING", "CHATGPT_REVIEW_RECEIVED_RUNNING"}
                recoverable_pending = isinstance(pending, dict) and bool(pending)
                # A restart can conservatively park a pre-ledger ChatGPT team
                # run in BLOCKED when the controller has not yet persisted
                # worker task IDs.  If a durable bridge phase is pending, the
                # exact idempotency key/request ID is still the recovery
                # authority, so resume the run just like FAILED.  This never
                # creates a new bridge exchange; _workflow_exchange polls or
                # reuses the adapter receipt.
                if run.current_state in {"FAILED", "BLOCKED"} and (recoverable_failure or recoverable_pending):
                    run.transition("READY", reason="reconcile existing ChatGPT bridge request by idempotency key")
                    run.final_status = None
                    self.store.save(run)
                if run.current_state in {"READY", "EXECUTING", "VERIFYING"} and (recoverable_failure or recoverable_pending):
                    self._queue(run.goal_run_id)
                continue
            if run.current_state in {"FAILED", "BLOCKED"}:
                if run.decision == "CHATGPT_TEAM":
                    recovered = self.orchestrator._prepare_chatgpt_review_extension(run)
                    if not recovered:
                        recovered = self.orchestrator._prepare_chatgpt_repair_recovery(run)
                    if not recovered:
                        recovered = self.orchestrator._prepare_chatgpt_failed_task_recovery(run)
                    if not recovered:
                        recovered = self.orchestrator._prepare_chatgpt_verifier_recovery(run)
                    if not recovered:
                        recovered = self.orchestrator._prepare_chatgpt_completed_wave_resume(run)
                    if recovered:
                        self._queue(run.goal_run_id)
                        continue
                if not self._prepare_team_resume(run):
                    continue
                # _prepare_team_resume moves a recoverable terminal GoalRun
                # back to READY; queue it immediately so the same worker
                # attempt is reconciled without a caller-side resume request.
                if run.current_state == "READY":
                    self._queue(run.goal_run_id)
                    continue
            if run.current_state == "READY" and run.decision == "CHATGPT_TEAM":
                # A recovery helper may have persisted READY while this loop
                # still holds the pre-transition snapshot. Requeue the same
                # GoalRun so its review phase resumes without a new exchange.
                self._queue(run.goal_run_id)
                continue
            if run.current_state != "EXECUTING":
                if run.current_state == "COMPLETED" and run.decision == "CHATGPT_TEAM":
                    # A child repair wave can be marked completed before the
                    # follow-up bridge review is persisted. Reopen only when
                    # the guarded workflow has a pending review artifact set;
                    # an actually accepted goal has no such predicate.
                    if self.orchestrator._prepare_chatgpt_completed_wave_resume(run):
                        self._queue(run.goal_run_id)
                    continue
                if run.current_state == "READY" and run.decision == "CHATGPT_TEAM":
                    # The completed-wave recovery above may have transitioned
                    # this same snapshot to READY. Queue it immediately;
                    # otherwise the supervisor would wait forever for another
                    # external trigger.
                    self._queue(run.goal_run_id)
                    continue
                if run.current_state == "VERIFYING" and run.decision == "CHATGPT_TEAM":
                    workflow = run.context.get("chatgpt_workflow")
                    pending = workflow.get("pending_phases", {}) if isinstance(workflow, dict) else {}
                    if isinstance(pending, dict) and pending:
                        self._queue(run.goal_run_id)
                    else:
                        # A terminal bridge preflight failure is persisted in
                        # ``phases`` rather than ``pending_phases``. Wake the
                        # exact GoalRun only while the adapter's bounded
                        # same-request recovery remains available; after the
                        # cap, leave it parked for inspection.
                        phases = workflow.get("phases", {}) if isinstance(workflow, dict) else {}
                        review_round = int(workflow.get("review_round", 1) or 1) if isinstance(workflow, dict) else 1
                        review_phase = "REVIEW_RECEIVED" if review_round == 1 else f"REVIEW_RECEIVED_{review_round}"
                        failed_review = phases.get(review_phase) if isinstance(phases, dict) else None
                        retryable_errors = {
                            "CONVERSATION_MISMATCH",
                            "orca transport action failed: element_not_found",
                        }
                        if (isinstance(failed_review, dict)
                                and failed_review.get("status") == "FAILED"
                                and failed_review.get("error") in retryable_errors
                                and int(failed_review.get("retry_count", 0) or 0) < 3):
                            self._queue(run.goal_run_id)
                continue
            try:
                ledger = TeamLedgerStore(run.team_ledger_ref).load()
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            # A worker can finish its durable team ledger after the controller
            # has returned (or after a service restart). Reconcile that
            # terminal ledger here instead of leaving the GoalRun EXECUTING.
            # ChatGPT team runs with accepted artifacts continue directly to
            # VERIFYING/review; this path reuses the recorded plan receipt and
            # never submits another bridge exchange.
            if self._reconcile_terminal_team_ledger(run, ledger):
                continue
            if run.decision == "CHATGPT_TEAM":
                workflow = run.context.get("chatgpt_workflow")
                pending = workflow.get("pending_phases", {}) if isinstance(workflow, dict) else {}
                if isinstance(pending, dict) and pending:
                    self._queue(run.goal_run_id)
                elif ledger.get("status") == "RUNNING" and any(
                    item.get("state") == "RUNNING" for item in ledger.get("tasks", [])
                    if isinstance(item, dict)
                ):
                    # A ChatGPT team can be between bridge phases while a
                    # durable worker attempt is still in flight. Reconcile
                    # that exact queue task ID even when no bridge phase is
                    # pending; otherwise a terminal provider failure leaves
                    # the GoalRun stuck in EXECUTING forever.
                    self._queue(run.goal_run_id)
                elif run.current_state == "EXECUTING" and any(
                    item.get("state") == "PENDING" and all(
                        next((dep.get("state") for dep in ledger.get("tasks", [])
                              if isinstance(dep, dict) and dep.get("task_id") == dependency), None) == "COMPLETED"
                        for dependency in item.get("depends_on", [])
                    )
                    for item in ledger.get("tasks", []) if isinstance(item, dict)
                ):
                    # A fresh ChatGPT REVIEW: FIX may have appended a repair
                    # wave. Queue the owner to continue it without waiting for
                    # another user prompt or creating a new goal run.
                    self._queue(run.goal_run_id)
                continue
            if ledger.get("status") != "RUNNING" and not any(
                item.get("state") == "RUNNING" for item in ledger.get("tasks", [])
                if isinstance(item, dict)
            ):
                continue
            self._queue(run.goal_run_id)

    def _reconcile_terminal_team_ledger(self, run: GoalRun, ledger: dict[str, Any]) -> bool:
        """Consume a terminal team ledger left behind by a prior controller.

        The ledger is the durable source of truth for worker completion. A
        terminal ledger paired with an EXECUTING GoalRun must be applied once;
        otherwise the supervisor's normal RUNNING-only recovery check leaves a
        zombie GoalRun that can never reach verification or a truthful failure.
        """
        status = ledger.get("status")
        if status not in {"COMPLETED", "FAILED"}:
            return False
        run.team_status = status
        task_states = {
            item.get("task_id"): item.get("state")
            for item in ledger.get("tasks", [])
            if isinstance(item, dict)
        }
        run.team_summary = {
            "team_run_id": ledger.get("team_run_id"),
            "status": status,
            "task_states": task_states,
            "worker_attempts": sum(
                len(item.get("worker_attempts", []))
                for item in ledger.get("tasks", [])
                if isinstance(item, dict)
            ),
            "completed_tasks": sum(state == "COMPLETED" for state in task_states.values()),
        }
        if status == "FAILED":
            self.orchestrator._apply_team_result(run)
            return True

        if run.decision == "CHATGPT_TEAM":
            self.orchestrator._apply_team_result(run, finalize=False)
            if run.verification.get("team_tasks_completed"):
                self._queue(run.goal_run_id)
            else:
                run.final_status = "FAILED"
                run.transition("FAILED", reason="worker verification failed before ChatGPT review")
                self.store.save(run)
            return True

        self.orchestrator._apply_team_result(run)
        return True

    def _prepare_team_resume(self, run: GoalRun) -> bool:
        """Adopt a queue-backed team attempt before resuming a failed GoalRun."""
        if not run.team_ledger_ref:
            return False
        try:
            config = self.orchestrator._team_config()
            runner = TeamRunner(
                run.team_ledger_ref,
                max_workers=int(run.context.get("team_max_workers", config.get("max_workers", 2))),
                wait_seconds=float(config.get("worker_wait_seconds", 600)),
                provider_policy=run.provider_policy or {"allowed_providers": ["local-qwen"]},
            )
            ledger = runner.prepare_resume()
        except (OSError, ValueError, RuntimeError):
            return False
        recovered = any(item.get("state") == "RUNNING" for item in ledger.get("tasks", [])
                        if isinstance(item, dict))
        if not recovered:
            # ``prepare_resume`` may reopen a verifier that was left BLOCKED
            # by an earlier dependency wave. Treat an eligible PENDING task as
            # recoverable so the same GoalRun is queued without a new attempt.
            states = {
                item.get("task_id"): item.get("state")
                for item in ledger.get("tasks", []) if isinstance(item, dict)
            }
            recovered = any(
                item.get("state") == "PENDING"
                and isinstance(item.get("depends_on"), list)
                and bool(item.get("depends_on"))
                and all(states.get(dep) == "COMPLETED" for dep in item.get("depends_on", []))
                for item in ledger.get("tasks", []) if isinstance(item, dict)
            )
        if not recovered:
            return False
        if run.current_state in {"FAILED", "BLOCKED"}:
            run.transition("READY", reason="recovered persisted team provider task by stable task ID")
            run.final_status = None
            run.team_status = "RUNNING"
            run.blockers.append({"class": "team_recovery", "reason": "recovered queued/running provider task without new attempt"})
            self.store.save(run)
        return True

    def _execute_one(self, goal_run_id: str) -> None:
        try:
            run = self.store.load(goal_run_id)
            if run.current_state in {"COMPLETED", "UNRESOLVED", "FAILED"}:
                return
            run.execution_owner = self.instance_id
            run.lease_acquired_at = run.heartbeat_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            run.execution_attempt += 1
            self.store.save(run)
            if run.current_state == "RECEIVED":
                self.orchestrator.plan(goal_run_id)
            run = self.store.load(goal_run_id)
            if run.current_state == "READY":
                self.orchestrator.execute(goal_run_id)
            elif run.current_state == "UNRESOLVED":
                pass
            elif run.current_state in {"EXECUTING", "VERIFYING"}:
                if run.current_state == "VERIFYING" and run.decision == "CHATGPT_TEAM":
                    # Worker artifacts and the plan receipt are durable. Resume
                    # the review phase through the same state machine.
                    self.orchestrator.execute(goal_run_id)
                    return
                if run.current_state == "EXECUTING" and run.team_ledger_ref:
                    self.orchestrator.execute(goal_run_id)
                    return
                self._reconcile_or_block(run)
                # Composition reconciliation may replay a persisted trace and
                # move the GoalRun back to READY.  The current worker already
                # owns the GoalRun, so _queue() inside reconciliation cannot
                # schedule another worker while this id is in _active.  Keep
                # the same owner and continue the normal execution path here.
                recovered = self.store.load(goal_run_id)
                if recovered.current_state == "READY":
                    self.orchestrator.execute(goal_run_id)
        except (OSError, ValueError, RuntimeError) as exc:
            try:
                run = self.store.load(goal_run_id)
                run.blockers.append({"class": "service", "reason": str(exc)})
                if run.current_state in {"RECEIVED", "ANALYZING", "PLANNED", "READY"}:
                    run.final_status = "FAILED"
                    run.transition("FAILED", reason=f"service: {exc}")
                self.store.save(run)
            except (OSError, ValueError, RuntimeError):
                pass
        finally:
            try:
                run = self.store.load(goal_run_id)
                run.execution_owner = None
                run.heartbeat_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self.store.save(run)
            except (OSError, ValueError, RuntimeError):
                pass
            with self._active_lock:
                self._active.discard(goal_run_id)

    def _recover_startup(self) -> None:
        for path in sorted(self.config.state_root.glob("*.json")):
            if path.name in {"service.json"} or path.name.endswith(".result.json"):
                continue
            try:
                run = self.store.load(path.stem)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.recovery_events.append({"goal_run_id": path.stem, "action": "state_rejected",
                                             "reason": str(exc), "path": str(path)})
                continue
            if run.current_state in {"COMPLETED", "UNRESOLVED", "FAILED"}:
                continue
            if run.execution_owner and run.execution_owner != self.instance_id:
                self.recovery_events.append({"goal_run_id": run.goal_run_id, "action": "stale_owner_cleared", "previous_owner": run.execution_owner})
                run.execution_owner = None
                self.store.save(run)
            if run.current_state in {"RECEIVED", "READY"}:
                self.recovery_events.append({"goal_run_id": run.goal_run_id, "action": "resume_ready"})
                self._queue(run.goal_run_id)
            elif run.current_state == "EXECUTING":
                reconcilable = any(ref.get("task_id") and ref.get("provider") and ref.get("task_spec")
                                   for ref in run.execution_refs)
                if not reconcilable:
                    self.recovery_events.append({"goal_run_id": run.goal_run_id, "action": "reset_pre_dispatch_execution"})
                    run.transition("READY", reason="service restart occurred before execution reference was persisted")
                    run.final_status = None
                    self.store.save(run)
                    self._queue(run.goal_run_id)
                else:
                    self.recovery_events.append({"goal_run_id": run.goal_run_id, "action": "reconcile_before_resume"})
                    self._queue(run.goal_run_id)
            elif run.current_state == "VERIFYING":
                workflow = run.context.get("chatgpt_workflow") if isinstance(run.context, dict) else None
                pending = workflow.get("pending_phases", {}) if isinstance(workflow, dict) else {}
                if run.decision == "CHATGPT_TEAM" and not pending:
                    # A terminal browser-boundary receipt is parked until an
                    # operator explicitly resumes after checking Chrome.
                    self.recovery_events.append({
                        "goal_run_id": run.goal_run_id,
                        "action": "park_verification_for_explicit_browser_resume",
                    })
                    continue
                self.recovery_events.append({"goal_run_id": run.goal_run_id, "action": "resume_verification"})
                self._queue(run.goal_run_id)

    def _reconcile_or_block(self, run: GoalRun) -> None:
        # Known task IDs are inspected by the existing backend status contract;
        # no submission is made from recovery unless the authoritative queue
        # proves the prepared idempotency key is missing. Accepted jobs are
        # polled for a bounded interval so a service restart can consume a
        # result that finishes while recovery owns the GoalRun.
        refs = [ref for ref in run.execution_refs if ref.get("task_id")]
        statuses = []
        if run.decision == "COMPOSE":
            all_terminal = True
            for ref in refs:
                provider = ref.get("provider")
                if not provider:
                    trace_path = Path(run.workspace) / ".system-agentic-workflows" / run.goal_run_id / "trace.json"
                    try:
                        trace = json.loads(trace_path.read_text(encoding="utf-8"))
                        step = next((item for item in trace.get("steps", []) if item.get("task_id") == ref.get("task_id")), None)
                        if not isinstance(step, dict) or step.get("execution_status") != "COMPLETED":
                            all_terminal = False
                        else:
                            ref.update(execution_status="COMPLETED", terminal_status="COMPLETED", dispatch_state="TERMINAL")
                            statuses.append("COMPLETED")
                    except (OSError, ValueError, json.JSONDecodeError):
                        all_terminal = False
                    continue
                status = None
                try:
                    task = task_from_spec(ref["task_spec"])
                    status = adapter_for(provider).status(task).status
                    if status == "MISSING":
                        # The durable intent exists but the queue has no
                        # accepted record. Reuse the exact identity once.
                        result = self.orchestrator._dispatch(task, module_id=ref.get("module"),
                                                            module_capability=ref.get("capability"))
                        status = result.get("execution_status", result.get("status", "UNKNOWN"))
                    else:
                        status = self._wait_existing_terminal(task, provider, status)
                    ref.update(execution_status=status,
                               terminal_status=status if status in {"COMPLETED", "FAILED", "FAILED_LOST"} else None,
                               dispatch_state="TERMINAL" if status in {"COMPLETED", "FAILED", "FAILED_LOST"} else "SUBMITTED",
                               reconciled_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                    statuses.append(status)
                    if status != "COMPLETED":
                        all_terminal = False
                except (OSError, RuntimeError, ValueError):
                    all_terminal = False
            if all_terminal and refs:
                run.transition("READY", reason="composition provider tasks reconciled; replay persisted composition trace")
                run.final_status = None
                self.store.save(run)
                self._queue(run.goal_run_id)
                return
            run.blockers.append({"class": "recovery_reconciliation", "reason": "composition provider state not terminal", "task_statuses": statuses})
            run.final_status = "BLOCKED"
            run.transition("BLOCKED", reason="composition provider state requires reconciliation")
            self.store.save(run)
            return
        for ref in refs:
            status = ref.get("execution_status")
            provider = ref.get("provider")
            if ref.get("dispatch_state") == "PREPARED" and provider and ref.get("task_spec"):
                try:
                    task = task_from_spec(ref["task_spec"])
                    status_result = adapter_for(provider).status(task)
                    status = status_result.status
                    status = self._wait_existing_terminal(task, provider, status)
                    if status == "MISSING":
                        # Same stable task ID + queue idempotency key; submit can only
                        # create the job if the durable queue never accepted attempt 1.
                        result = self.orchestrator._dispatch(task, module_id=ref.get("module"), module_capability=ref.get("capability"))
                        status = result.get("execution_status", result.get("status", "UNKNOWN"))
                        ref.update(dispatch_state="SUBMITTED", provider_job_id=result.get("queue_job_id") or ref.get("task_id"),
                                   queue_job_id=result.get("queue_job_id"), execution_status=status)
                        if status == "COMPLETED" and run.current_state == "EXECUTING":
                            run.transition("VERIFYING", reason="prepared task reconciled and completed")
                            self.store.save(run)
                            self.store.audit({"event": "provider_terminal", "goal_run_id": run.goal_run_id,
                                              "task_id": ref.get("task_id"), "status": status})
                            self.orchestrator._apply_direct_result(run, result)
                            return
                    elif status in {"COMPLETED", "FAILED", "FAILED_LOST"}:
                        # The queue accepted the durable intent before the
                        # service died.  Re-read the existing job through the
                        # normal module-aware reconciliation path so its
                        # terminal result/validation is applied exactly once.
                        result = reconcile_task(task, provider_id=provider,
                                                module_id=ref.get("module"),
                                                module_capability=ref.get("capability"))
                        status = result.get("execution_status", result.get("status", status))
                        ref.update(execution_status=status,
                                   terminal_status=status if status in {"COMPLETED", "FAILED", "FAILED_LOST"} else None,
                                   dispatch_state="TERMINAL" if status in {"COMPLETED", "FAILED", "FAILED_LOST"} else "SUBMITTED",
                                   reconciled_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                        self.store.audit({"event": "provider_reconcile", "goal_run_id": run.goal_run_id,
                                          "task_id": ref.get("task_id"), "provider_job_id": ref.get("provider_job_id"),
                                          "status": status})
                        if status == "COMPLETED" and run.current_state == "EXECUTING":
                            run.transition("VERIFYING", reason="accepted task terminal result reconciled")
                            self.store.save(run)
                            self.store.audit({"event": "provider_terminal", "goal_run_id": run.goal_run_id,
                                              "task_id": ref.get("task_id"), "status": status})
                            self.orchestrator._apply_direct_result(run, result)
                            return
                        if status in {"FAILED", "FAILED_LOST"}:
                            run.blockers.append({"class": "provider_terminal", "reason": status,
                                                 "task_id": ref.get("task_id")})
                            run.final_status = "FAILED"
                            run.transition("FAILED", reason=f"provider terminal status={status}")
                            self.store.save(run)
                            return
                    else:
                        ref.update(dispatch_state="SUBMITTED", provider_job_id=ref.get("task_id"), execution_status=status)
                except (OSError, RuntimeError, ValueError):
                    status = "UNKNOWN"
            elif provider and ref.get("task_spec"):
                try:
                    task = __import__("providers.dispatch", fromlist=["task_from_spec"]).task_from_spec(ref["task_spec"])
                    result = reconcile_task(task, provider_id=provider, module_id=ref.get("module"), module_capability=ref.get("capability"))
                    status = result.get("execution_status", result.get("status", "UNKNOWN"))
                    status = self._wait_existing_terminal(task, provider, status)
                    if status in {"COMPLETED", "FAILED", "FAILED_LOST"}:
                        result = reconcile_task(task, provider_id=provider, module_id=ref.get("module"),
                                                module_capability=ref.get("capability"))
                        status = result.get("execution_status", result.get("status", status))
                    ref["execution_status"] = status
                    ref["terminal_status"] = status if status in {"COMPLETED", "FAILED", "FAILED_LOST"} else None
                    ref["dispatch_state"] = "TERMINAL" if ref["terminal_status"] else "SUBMITTED"
                    ref["reconciled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    self.store.audit({"event": "provider_reconcile", "goal_run_id": run.goal_run_id,
                                      "task_id": ref.get("task_id"), "provider_job_id": ref.get("provider_job_id"),
                                      "status": status})
                    if status == "COMPLETED" and run.current_state == "EXECUTING":
                        run.transition("VERIFYING", reason="provider terminal result reconciled")
                        self.store.save(run)
                        self.store.audit({"event": "provider_terminal", "goal_run_id": run.goal_run_id,
                                          "task_id": ref.get("task_id"), "status": status})
                        self.orchestrator._apply_direct_result(run, result)
                        return
                    if status in {"FAILED", "FAILED_LOST"}:
                        run.blockers.append({"class": "provider_terminal", "reason": status,
                                             "task_id": ref.get("task_id")})
                        run.final_status = "FAILED"
                        if run.current_state in {"EXECUTING", "VERIFYING"}:
                            run.transition("FAILED", reason=f"provider terminal status={status}")
                        self.store.save(run)
                        return
                except (OSError, RuntimeError, ValueError):
                    status = "UNKNOWN"
            statuses.append(status)
        run.blockers.append({"class": "recovery_reconciliation", "reason": "execution state requires explicit reconciliation", "task_statuses": statuses})
        run.final_status = "BLOCKED"
        if run.current_state in {"EXECUTING", "VERIFYING"}:
            run.transition("BLOCKED", reason="service restart requires safe task reconciliation")
        self.store.save(run)

    def _wait_existing_terminal(self, task: ProviderTask, provider: str, initial: str,
                                timeout_seconds: float = 600) -> str:
        """Observe only the existing job; never resubmit while it is queued/running."""
        status = initial
        deadline = time.monotonic() + timeout_seconds
        while status in {"SUBMITTED", "RUNNING", "UNKNOWN"} and time.monotonic() < deadline:
            time.sleep(1)
            try:
                status = adapter_for(provider).status(task).status
            except (OSError, RuntimeError, ValueError):
                status = "UNKNOWN"
        return status

    def _handler(self):
        service = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return
            def send_json(self, code: int, value: Any) -> None:
                body = _json_bytes(value)
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # A bounded client may close the loopback socket while a
                    # provider/module readiness probe is still running.
                    return
            def body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                value = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(value, dict):
                    raise ValueError("request body must be an object")
                return value
            def do_GET(self) -> None:
                try:
                    if self.path == "/health":
                        return self.send_json(200, service.health())
                    if self.path == "/doctor":
                        return self.send_json(200, service.doctor())
                    parts = self.path.strip("/").split("/")
                    if len(parts) == 2 and parts[0] == "goals":
                        return self.send_json(200, service.inspect(parts[1]))
                    if len(parts) == 3 and parts[0] == "goals" and parts[2] == "overview":
                        return self.send_json(200, service.overview(parts[1]))
                    if len(parts) == 3 and parts[0] == "goals" and parts[2] == "result":
                        return self.send_json(200, service.result(parts[1]))
                    self.send_json(404, {"error": "not found"})
                except FileNotFoundError as exc:
                    self.send_json(404, {"error": str(exc)})
                except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
                    self.send_json(400, {"error": str(exc)})
            def do_POST(self) -> None:
                try:
                    if self.path == "/goals":
                        return self.send_json(202, service.submit(self.body()))
                    parts = self.path.strip("/").split("/")
                    if len(parts) == 3 and parts[0] == "goals" and parts[2] == "resume":
                        return self.send_json(202, service.resume(parts[1]))
                    self.send_json(404, {"error": "not found"})
                except FileNotFoundError as exc:
                    self.send_json(404, {"error": str(exc)})
                except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
                    self.send_json(400, {"error": str(exc)})
        return Handler


def service_config_from_env() -> ServiceConfig:
    root = Path(os.environ.get("SYSTEM_AGENTIC_ORCHESTRATOR_STATE_ROOT", str(_default_state_root()))).expanduser()
    return ServiceConfig(root, os.environ.get("SYSTEM_AGENTIC_SERVICE_HOST", DEFAULT_HOST), int(os.environ.get("SYSTEM_AGENTIC_SERVICE_PORT", DEFAULT_PORT)))


def run_service() -> None:
    service = OrchestratorService(service_config_from_env())
    def stop(_signum: int, _frame: Any) -> None:
        # ``HTTPServer.shutdown`` must run outside ``serve_forever``'s thread.
        threading.Thread(target=service.shutdown, name="system-agentic-shutdown", daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        service.shutdown()


def service_request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None,
                    config: ServiceConfig | None = None,
                    timeout_seconds: float = DEFAULT_CLIENT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Small provider-neutral client used by the CLI and tests."""
    config = config or service_config_from_env()
    url = f"http://{config.host}:{config.port}{path}"
    data = _json_bytes(payload) if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, float(timeout_seconds))) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = {"error": body}
        raise RuntimeError(detail.get("error", f"service HTTP {exc.code}")) from exc


def service_is_running(config: ServiceConfig | None = None) -> bool:
    try:
        return service_request("/health", config=config).get("service_running") is True
    except (OSError, RuntimeError, ValueError):
        return False


if __name__ == "__main__":
    run_service()
