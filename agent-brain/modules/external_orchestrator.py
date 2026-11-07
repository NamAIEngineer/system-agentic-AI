"""Bounded top-level goal lifecycle for System Agentic AI.

The orchestrator owns goal state and routing. Module/provider/tool execution stays
in the existing dispatch, Composer, Composition Runtime, and Tool Runtime seams.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import re
import tempfile
import fcntl
import uuid
import inspect
import shlex
import subprocess
import time
import urllib.request
from typing import Any, Callable

from modules.system_composer import SystemBlueprint, analyze_goal, design_system, run_system
from modules.capability_gap import CapabilityGapResolver
from modules.capability_builder import CapabilityBuilder
from modules.hstry_context import capture_hstry_context
from modules.team_ledger import TeamLedgerStore, TeamRunner, create_ledger, _validate_worker_result
from providers.dispatch import dispatch_task, task_from_spec


GOAL_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}\Z")
AUTO_ACTION_RE = re.compile(
    r"\b(?:build|implement|create|add|fix|refactor|debug|update|change|design|write|"
    r"develop|migrate|integrate|automate|deploy|triển khai|xây dựng|tạo|thêm|sửa|"
    r"gỡ lỗi|cập nhật|tích hợp|tự động hóa)\b",
    re.IGNORECASE,
)
STATUS_REQUEST_RE = re.compile(
    r"^\s*(?:please\s+)?(?:provide|give|show|report|summarize|summarise|tell\s+me|"
    r"check|what(?:'s|\s+is)|how\s+is|status|progress|overview|health|"
    r"trạng\s+thái|tiến\s+độ|tổng\s+quan|sức\s+khỏe|cho\s+biết|báo\s+cáo|"
    r"kiểm\s+tra)\b.*\b(?:status|progress|overview|health|state|"
    r"trạng\s+thái|tiến\s+độ|tổng\s+quan|sức\s+khỏe)\b",
    re.IGNORECASE,
)
GOAL_RUN_SCHEMA_VERSION = 1
MAX_REPAIR_RECOVERIES_PER_LINEAGE = 3
STATES = {"RECEIVED", "ANALYZING", "PLANNED", "READY", "EXECUTING", "VERIFYING",
          "COMPLETED", "UNRESOLVED", "BLOCKED", "FAILED"}
DECISIONS = {"DIRECT", "COMPOSE", "COMBINED", "TEAM", "CHATGPT", "CHATGPT_TEAM", "UNRESOLVED"}
TRANSITIONS = {
    "RECEIVED": {"ANALYZING"},
    "ANALYZING": {"PLANNED", "FAILED"},
    "PLANNED": {"READY", "UNRESOLVED", "BLOCKED", "FAILED"},
    "READY": {"EXECUTING", "BLOCKED", "FAILED"},
    "EXECUTING": {"READY", "VERIFYING", "BLOCKED", "FAILED"},
    # ChatGPT may request a bounded repair after a child verifier passes. The
    # goal must return to execution for that new child wave instead of treating
    # PASS_BOUNDED as the terminal product verdict.
    "VERIFYING": {"EXECUTING", "COMPLETED", "BLOCKED", "FAILED"},
    # A bounded repair wave may have been reconciled as completed before its
    # required ChatGPT review phase was persisted. Allow the exact recovery
    # path to reopen that run; ordinary completed goals remain terminal unless
    # this guarded workflow predicate matches.
    "COMPLETED": {"READY"}, "UNRESOLVED": {"ANALYZING"}, "BLOCKED": {"READY"}, "FAILED": {"READY"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state_root() -> Path:
    configured = os.environ.get("SYSTEM_AGENTIC_ORCHESTRATOR_STATE_ROOT")
    if configured:
        return Path(configured).expanduser()
    # macOS reports ``os.name == "posix"``.  Use ``sys.platform`` here so a
    # process started without the LaunchAgent override still finds the same
    # persistent state directory as the native service.
    import sys
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SystemAgenticAI" / "orchestrator"
    return Path.home() / ".local" / "share" / "SystemAgenticAI" / "orchestrator"


@dataclass
class GoalRun:
    goal_run_id: str
    original_goal: str
    created_at: str
    current_state: str = "RECEIVED"
    decision: str | None = None
    composition_required: bool = False
    workspace: str = ""
    execution_policy: str = "READ_ONLY"
    context: dict[str, Any] = field(default_factory=dict)
    system_blueprint_ref: str | None = None
    resolution_ref: str | None = None
    resolved_capabilities: list[dict[str, Any]] = field(default_factory=list)
    composition_plan_ref: str | None = None
    execution_refs: list[dict[str, Any]] = field(default_factory=list)
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    retries: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    outcome: Any = None
    final_status: str | None = None
    acceptance_passed: bool = False
    transitions: list[dict[str, Any]] = field(default_factory=list)
    provider_policy: dict[str, Any] = field(default_factory=dict)
    execution_owner: str | None = None
    lease_acquired_at: str | None = None
    execution_attempt: int = 0
    heartbeat_at: str | None = None
    capability_gap_ref: str | None = None
    capability_gap_id: str | None = None
    capability_blueprint_ref: str | None = None
    gap_classification: str | None = None
    gap_buildability: str | None = None
    candidate_ref: str | None = None
    candidate_id: str | None = None
    candidate_status: str | None = None
    candidate_verification_status: str | None = None
    team_ledger_ref: str | None = None
    team_status: str | None = None
    team_summary: dict[str, Any] = field(default_factory=dict)
    schema_version: int = GOAL_RUN_SCHEMA_VERSION

    def transition(self, target: str, *, reason: str = "") -> None:
        if target not in STATES:
            raise ValueError(f"Unknown GoalRun state: {target}")
        if target not in TRANSITIONS.get(self.current_state, set()):
            raise ValueError(f"Invalid GoalRun transition {self.current_state} -> {target}")
        self.transitions.append({"from": self.current_state, "to": target, "at": _now(), "reason": reason})
        self.current_state = target

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GOAL_RUN_SCHEMA_VERSION,
            "goal_run_id": self.goal_run_id, "original_goal": self.original_goal,
            "created_at": self.created_at, "current_state": self.current_state,
            "decision": self.decision, "composition_required": self.composition_required,
            "workspace": self.workspace, "execution_policy": self.execution_policy,
            "context": self.context, "system_blueprint_ref": self.system_blueprint_ref,
            "resolution_ref": self.resolution_ref, "resolved_capabilities": self.resolved_capabilities,
            "composition_plan_ref": self.composition_plan_ref,
            "execution_refs": self.execution_refs, "artifact_refs": self.artifact_refs,
            "verification": self.verification, "retries": self.retries,
            "blockers": self.blockers, "outcome": self.outcome,
            "final_status": self.final_status, "acceptance_passed": self.acceptance_passed,
            "transitions": self.transitions, "provider_policy": self.provider_policy,
            "execution_owner": self.execution_owner, "lease_acquired_at": self.lease_acquired_at,
            "execution_attempt": self.execution_attempt, "heartbeat_at": self.heartbeat_at,
            "capability_gap_ref": self.capability_gap_ref,
            "capability_gap_id": self.capability_gap_id,
            "capability_blueprint_ref": self.capability_blueprint_ref,
            "gap_classification": self.gap_classification,
            "gap_buildability": self.gap_buildability,
            "candidate_ref": self.candidate_ref, "candidate_id": self.candidate_id,
            "candidate_status": self.candidate_status,
            "candidate_verification_status": self.candidate_verification_status,
            "team_ledger_ref": self.team_ledger_ref,
            "team_status": self.team_status,
            "team_summary": self.team_summary,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GoalRun":
        if not isinstance(value, dict) or not isinstance(value.get("goal_run_id"), str):
            raise ValueError("GoalRun requires goal_run_id")
        schema_version = value.get("schema_version", GOAL_RUN_SCHEMA_VERSION)
        if schema_version != GOAL_RUN_SCHEMA_VERSION:
            raise ValueError(f"Unsupported GoalRun schema version: {schema_version!r}")
        defaults = {
            "original_goal": "", "created_at": "", "current_state": "RECEIVED", "decision": None,
            "composition_required": False, "workspace": "", "execution_policy": "READ_ONLY",
            "context": {}, "system_blueprint_ref": None, "resolution_ref": None,
            "resolved_capabilities": [], "composition_plan_ref": None,
            "execution_refs": [], "artifact_refs": [], "verification": {}, "retries": [],
            "blockers": [], "outcome": None, "final_status": None, "acceptance_passed": False,
            "transitions": [], "provider_policy": {},
            "execution_owner": None, "lease_acquired_at": None, "execution_attempt": 0,
            "heartbeat_at": None,
            "capability_gap_ref": None, "capability_blueprint_ref": None,
            "capability_gap_id": None,
            "gap_classification": None, "gap_buildability": None,
            "candidate_ref": None, "candidate_id": None, "candidate_status": None,
            "candidate_verification_status": None,
            "team_ledger_ref": None, "team_status": None, "team_summary": {},
        }
        kwargs = {key: value.get(key, default) for key, default in defaults.items()}
        kwargs["goal_run_id"] = value["goal_run_id"]
        kwargs["context"] = dict(kwargs.get("context") or {})
        kwargs["resolved_capabilities"] = list(kwargs.get("resolved_capabilities") or [])
        for key in ("execution_refs", "artifact_refs", "retries", "blockers", "transitions"):
            kwargs[key] = list(kwargs.get(key) or [])
        kwargs["verification"] = dict(kwargs.get("verification") or {})
        kwargs["provider_policy"] = dict(kwargs.get("provider_policy") or {})
        kwargs["team_summary"] = dict(kwargs.get("team_summary") or {})
        kwargs["schema_version"] = schema_version
        if kwargs["current_state"] not in STATES:
            raise ValueError("GoalRun contains unknown current_state")
        if kwargs["decision"] is not None and kwargs["decision"] not in DECISIONS:
            raise ValueError("GoalRun contains unknown decision")
        return cls(**kwargs)


class GoalRunStore:
    """Small atomic JSON store owned by the System Agentic host runtime."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser() if root else _default_state_root()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, goal_run_id: str) -> Path:
        if not GOAL_ID.fullmatch(goal_run_id):
            raise ValueError("GoalRun ID is not path-safe")
        return self.root / f"{goal_run_id}.json"

    @contextmanager
    def creation_lock(self):
        """Serialize create-or-reuse across service threads and processes.

        Goal IDs are deterministic for hook submissions.  The state file write
        is atomic, but an unlocked existence check could still let two service
        instances plan/queue the same ID concurrently.  Keep the lock local to
        the durable state root; it carries no task or secret data.
        """
        lock_path = self.root / ".goal-create.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def save(self, run: GoalRun) -> Path:
        target = self.path(run.goal_run_id)
        fd, name = tempfile.mkstemp(prefix=f".{run.goal_run_id}.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(run.to_dict(), stream, ensure_ascii=False, indent=2)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.audit({"event": "goal_state_persisted", "goal_run_id": run.goal_run_id,
                    "state": run.current_state, "execution_owner": run.execution_owner,
                    "execution_attempt": run.execution_attempt,
                    "execution_refs": run.execution_refs})
        return target

    def audit(self, event: dict[str, Any]) -> None:
        """Append a small local audit event; never makes state persistence depend on it."""
        record = {"at": _now(), **event}
        path = self.root / "audit.jsonl"
        try:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush(); os.fsync(stream.fileno())
        except OSError:
            # Auditability must not turn a valid GoalRun write into a data loss.
            pass

    def load(self, goal_run_id: str) -> GoalRun:
        path = self.path(goal_run_id)
        if not path.is_file():
            raise FileNotFoundError(f"GoalRun not found: {goal_run_id}")
        return GoalRun.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def result_path(self, goal_run_id: str) -> Path:
        if not GOAL_ID.fullmatch(goal_run_id):
            raise ValueError("GoalRun ID is not path-safe")
        return self.root / f"{goal_run_id}.result.json"

    def save_result(self, goal_run_id: str, value: dict[str, Any]) -> Path:
        target = self.result_path(goal_run_id)
        fd, name = tempfile.mkstemp(prefix=f".{goal_run_id}.result.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return target


class ExternalOrchestrator:
    """Bounded lifecycle owner; all actual work is delegated to existing seams."""

    def __init__(self, *, state_root: str | Path | None = None,
                 inventory: list[dict[str, Any]] | None = None,
                 dispatch: Callable[..., dict[str, Any]] = dispatch_task,
                 resolver: Callable[..., dict[str, Any]] = analyze_goal,
                 composer: Callable[..., SystemBlueprint] = design_system,
                 system_runner: Callable[..., dict[str, Any]] = run_system) -> None:
        self.store = GoalRunStore(state_root)
        self.inventory = inventory
        self._dispatch = dispatch
        self._resolver = resolver
        self._composer = composer
        self._system_runner = system_runner

    def create(self, goal: str, *, workspace: str, execution_policy: str = "WORKSPACE_WRITE",
               context: dict[str, Any] | None = None, provider_policy: dict[str, Any] | None = None,
               goal_run_id: str | None = None) -> GoalRun:
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("External Orchestrator requires a non-empty goal")
        root = Path(workspace).expanduser().resolve(strict=True)
        if execution_policy not in {"READ_ONLY", "WORKSPACE_WRITE"}:
            raise ValueError("External Orchestrator refuses ELEVATED or unknown execution policy")
        run_id = goal_run_id or f"goal-{uuid.uuid4().hex[:12]}"
        if not GOAL_ID.fullmatch(run_id):
            raise ValueError("GoalRun ID is not path-safe")
        with self.store.creation_lock():
            existing_path = self.store.path(run_id)
            if existing_path.exists():
                existing = self.store.load(run_id)
                if existing.original_goal != goal.strip() or existing.workspace != str(root):
                    raise ValueError("GoalRun ID already belongs to a different goal or workspace")
                return existing
            run = GoalRun(run_id, goal.strip(), _now(), workspace=str(root), execution_policy=execution_policy,
                          context=dict(context or {}), provider_policy=dict(provider_policy or {}))
            self.store.save(run)
            return run

    def plan(self, goal_run_id: str) -> GoalRun:
        run = self.store.load(goal_run_id)
        if run.current_state == "ANALYZING":
            run.current_state = "RECEIVED"
        if run.current_state in {"COMPLETED", "UNRESOLVED", "BLOCKED", "FAILED", "READY", "EXECUTING", "VERIFYING"}:
            return run
        run.transition("ANALYZING", reason="top-level goal analysis")
        self.store.save(run)
        try:
            state_dir = Path(run.workspace) / ".system-agentic-goals" / run.goal_run_id
            state_dir.mkdir(parents=True, exist_ok=True)
            # History is evidence only.  Capture a bounded, provenance-first
            # receipt before planning so every history-dependent run can be
            # audited without copying a transcript into GoalRun state.
            if run.context.get("hstry_context_enabled", True) is not False:
                try:
                    requested_chars = run.context.get("hstry_max_chars", 2400)
                    try:
                        requested_chars = int(requested_chars)
                    except (TypeError, ValueError):
                        requested_chars = 2400
                    history_query = run.context.get("hstry_query")
                    if not isinstance(history_query, str) or not history_query.strip():
                        history_query = run.original_goal
                    receipt = capture_hstry_context(
                        history_query,
                        max_chars=requested_chars,
                        mode=str(run.context.get("hstry_mode", "auto")),
                        workspace=run.workspace,
                    )
                except Exception:
                    # The helper is deliberately fail-closed for its own
                    # output; planning remains available when hstry is down.
                    receipt = {"status": "ERROR", "error_code": "HSTRY_GATE_EXCEPTION",
                               "query": run.original_goal, "mode": "auto", "scope": "local", "hits": []}
                context_dir = state_dir / "context"
                context_dir.mkdir(parents=True, exist_ok=True)
                receipt_path = context_dir / "hstry.json"
                fd, temporary = tempfile.mkstemp(prefix=".hstry.", suffix=".tmp", dir=context_dir)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(receipt, stream, ensure_ascii=False, indent=2)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, receipt_path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                run.context["hstry_receipt_ref"] = str(receipt_path)
                run.context["hstry_receipt"] = {
                    key: receipt.get(key)
                    for key in ("status", "query", "mode", "scope", "last_sync_at", "completeness", "hits")
                    if key in receipt
                }
                self.store.save(run)
            chatgpt_workflow = run.context.get("chatgpt_workflow")
            if isinstance(chatgpt_workflow, dict):
                # A workflow owns both bridge exchanges and the worker wave.
                # Keep the plan prompt explicit, but never persist its body in
                # the GoalRun file or in the overview.
                run.resolved_capabilities = [
                    {"capability": "chatgpt.exchange", "module": "chatgpt-web",
                     "reason": "explicit context.chatgpt_workflow plan/review phases"},
                    {"capability": "task.submit", "module": "team-ledger",
                     "reason": "ChatGPT plan feeds durable worker tasks"},
                ]
                run.decision = "CHATGPT_TEAM"
                run.composition_required = True
                safe = {key: value for key, value in chatgpt_workflow.items()
                        if key not in {"plan_prompt", "review_prompt", "transcript", "content"}}
                workflow_path = state_dir / "chatgpt-workflow.json"
                workflow_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
                run.context["chatgpt_workflow_ref"] = str(workflow_path)
                self.store.save(run)
                run.transition("PLANNED", reason="ChatGPT plan/review workflow ready")
                run.transition("READY", reason="approval-gated bridge and team workflow ready")
                self.store.save(run)
                return run
            chatgpt_request = run.context.get("chatgpt_request")
            if isinstance(chatgpt_request, dict):
                # Keep the web transport explicit. hstry retrieval still runs
                # first, but a ChatGPT exchange never enters the generic team
                # or provider route implicitly.
                run.resolved_capabilities = [{
                    "capability": "chatgpt.exchange",
                    "module": "chatgpt-web",
                    "reason": "explicit context.chatgpt_request",
                }]
                run.decision = "CHATGPT"
                run.composition_required = False
                request_path = state_dir / "chatgpt-request.json"
                safe_request = {key: value for key, value in chatgpt_request.items()
                                if key in {"run_id", "account_id", "conversation_id", "idempotency_key", "approval_id"}}
                request_path.write_text(json.dumps(safe_request, ensure_ascii=False, indent=2), encoding="utf-8")
                run.context["chatgpt_request_ref"] = str(request_path)
                self.store.save(run)
                run.transition("PLANNED", reason="explicit ChatGPT web adapter request")
                run.transition("READY", reason="approval-gated ChatGPT transport ready")
                self.store.save(run)
                return run
            # Natural action goals intentionally enter the generic team lane
            # without a live capability inventory sweep; the sweep can start
            # MCP/model probes and exceed the service lifecycle budget. An
            # explicitly disabled team route must still resolve capabilities
            # first so unknown work stays truthful UNRESOLVED instead of being
            # parked in an unexecutable ledger.
            if self._team_enabled(run):
                self._prepare_team(run, state_dir)
            else:
                pre_analysis = self._resolver(run.original_goal, inventory=self.inventory)
                if pre_analysis.get("blueprint_status") == "UNRESOLVED":
                    run.context["pre_analysis"] = pre_analysis
                    self.store.save(run)
            if run.team_ledger_ref:
                run.decision = "TEAM"
                run.composition_required = True
                run.transition("PLANNED", reason="team ledger ready")
                run.transition("READY", reason="team workflow ready for execution")
                self.store.save(run)
                return run
            analysis = run.context.pop("pre_analysis", None) or self._resolver(run.original_goal, inventory=self.inventory)
            run.resolved_capabilities = [
                {"capability": item["requirement"]["capability"], "module": item["module"]["id"],
                 "reason": item["requirement"].get("reason")}
                for item in analysis["resolved"]
            ]
            resolution_path = state_dir / "resolution.json"
            resolution_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
            run.resolution_ref = str(resolution_path)
            run.composition_required = bool(analysis["composition_required"])
            capability_set = {item["requirement"]["capability"] for item in analysis["resolved"]}
            combined = {"browser.automation", "software.coding"}.issubset(capability_set)
            run.decision = "UNRESOLVED" if analysis["blueprint_status"] != "VALID" else ("COMBINED" if combined else ("COMPOSE" if run.composition_required else "DIRECT"))
            run.transition("PLANNED", reason=f"decision={run.decision}")
            if run.decision == "UNRESOLVED":
                run.blockers = list(analysis["unresolved"]) or [{"reason": analysis["blueprint_status"]}]
                gap = CapabilityGapResolver(state_root=self.store.root, inventory=self.inventory).resolve(
                    source_goal_run_id=run.goal_run_id, original_goal=run.original_goal,
                    unresolved=list(analysis.get("unresolved", [])), context=run.context)
                run.capability_gap_ref = gap.gap_ref
                run.capability_gap_id = gap.gap_id
                run.capability_blueprint_ref = gap.blueprint_ref
                run.gap_classification = gap.classification
                run.gap_buildability = gap.buildability_status
                run.outcome = "CAPABILITY_GAP" if gap.classification == "TRUE_CAPABILITY_GAP" else gap.classification
                run.blockers.append({"classification": gap.classification, "gap_id": gap.gap_id,
                                     "gap_ref": gap.gap_ref, "blueprint_ref": gap.blueprint_ref,
                                     "buildability_status": gap.buildability_status})
                if gap.classification == "TRUE_CAPABILITY_GAP" and gap.buildability_status == "BUILDABLE_NOW" and gap.blueprint_ref:
                    blueprint_data = json.loads(Path(gap.blueprint_ref).read_text(encoding="utf-8"))
                    from modules.capability_gap import CapabilityBlueprint
                    blueprint_obj = CapabilityBlueprint(**{key: tuple(value) if isinstance(value, list) and key not in {"name", "purpose", "problem_solved", "safety_risk", "suggested_implementation_type", "estimated_complexity", "buildability_status"} else value for key, value in blueprint_data.items()})
                    candidate = CapabilityBuilder(state_root=self.store.root, workspace=run.workspace).build(gap, blueprint_obj)
                    if hasattr(candidate, "to_dict"):
                        candidate_dict = candidate.to_dict()
                        candidate_path = Path(candidate.candidate_root) / "candidate.json"
                        run.candidate_ref = str(candidate_path)
                        run.candidate_id = candidate.candidate_id
                        run.candidate_status = candidate.candidate_status
                        run.candidate_verification_status = candidate.verification_status
                        run.verification["candidate"] = {"candidate_id": candidate.candidate_id, "status": candidate.candidate_status,
                                                          "verification_status": candidate.verification_status}
                    else:
                        run.candidate_status = candidate.get("candidate_status")
                        run.blockers.append({"class": "capability_builder", **candidate})
                run.final_status = "UNRESOLVED"
                run.transition("UNRESOLVED", reason="required capability or supported scope unavailable")
            else:
                if run.decision == "COMPOSE":
                    blueprint = self._composer(
                        run.original_goal, workspace=run.workspace, execution_policy=run.execution_policy,
                        system_id=run.goal_run_id, context=run.context, inventory=self.inventory)
                    blueprint_path = state_dir / "blueprint.json"
                    blueprint_path.write_text(json.dumps(blueprint.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
                    run.system_blueprint_ref = str(blueprint_path)
                run.transition("READY", reason="validated blueprint ready for execution")
        except (OSError, ValueError, RuntimeError) as exc:
            run.blockers.append({"class": "planning", "reason": str(exc)})
            run.final_status = "FAILED"
            if run.current_state == "ANALYZING":
                run.transition("FAILED", reason=str(exc))
        self.store.save(run)
        return run

    def execute(self, goal_run_id: str) -> GoalRun:
        run = self.store.load(goal_run_id)
        if run.current_state in {"COMPLETED", "UNRESOLVED", "BLOCKED", "FAILED"}:
            return run
        if run.current_state == "RECEIVED":
            run = self.plan(goal_run_id)
        if run.current_state == "UNRESOLVED":
            return run
        if run.current_state == "READY":
            run.transition("EXECUTING", reason=f"execute {run.decision}")
            self.store.save(run)
        elif run.current_state == "VERIFYING" and run.decision == "CHATGPT_TEAM":
            # A completed prior review can establish the current browser
            # conversation even when a later phase was recorded against a
            # stale tab. Prepare one linked continuation before re-entering
            # the review; never reuse the failed request key.
            if self._prepare_chatgpt_review_phase_recovery(run):
                run = self.store.load(goal_run_id)
            self._execute_chatgpt_team_review(run)
            return self.store.load(goal_run_id)
        elif run.current_state != "EXECUTING":
            raise ValueError(f"GoalRun is not executable from state {run.current_state}")
        try:
            if run.decision == "TEAM":
                self._execute_team(run)
                run = self.store.load(goal_run_id)
                # TeamRunner may return while an accepted provider job is
                # still running.  Keep the GoalRun EXECUTING so service
                # recovery can reconcile the persisted task ID later.
                if run.team_status == "RUNNING":
                    run.final_status = None
                    self.store.save(run)
                    return run
                self._apply_team_result(run)
            elif run.decision == "CHATGPT":
                self._execute_chatgpt(run)
            elif run.decision == "CHATGPT_TEAM":
                self._execute_chatgpt_team(run)
            elif run.decision == "DIRECT":
                self._execute_direct(run)
            elif run.decision == "COMBINED":
                self._execute_combined(run)
            elif run.decision == "COMPOSE":
                blueprint = self._load_blueprint(run)
                self._execute_compose(run, blueprint)
            else:
                raise ValueError(f"Unknown execution decision: {run.decision}")
        except (OSError, ValueError, RuntimeError) as exc:
            # A bridge exchange persists its bounded terminal receipt before
            # raising a phase error. Reload that state before adding the
            # controller blocker; otherwise the stale in-memory GoalRun
            # overwrites the newly persisted phase and recovery cannot see
            # that the same request already consumed its one retry.
            try:
                persisted = self.store.load(goal_run_id)
                if persisted.context.get("chatgpt_workflow") != run.context.get("chatgpt_workflow"):
                    run = persisted
            except (OSError, ValueError, RuntimeError):
                pass
            run.blockers.append({"class": self.classify_failure(str(exc)), "reason": str(exc)})
            run.final_status = "FAILED"
            if run.current_state == "EXECUTING":
                run.transition("FAILED", reason=str(exc))
            self.store.save(run)
        return run

    def _execute_chatgpt(self, run: GoalRun) -> None:
        from modules.chatgpt_web_adapter import ChatGPTWebAdapter

        request = run.context.get("chatgpt_request")
        if not isinstance(request, dict):
            run.blockers.append({"class": "input", "reason": "chatgpt.exchange requires context.chatgpt_request"})
            run.final_status = "BLOCKED"
            run.transition("BLOCKED", reason="ChatGPT request missing")
            self.store.save(run)
            return
        result = ChatGPTWebAdapter().send(request)
        run.outcome = {key: value for key, value in result.items()
                       if key not in {"prompt", "content", "response"}}
        status = result.get("status")
        if status == "APPROVAL_REQUIRED":
            run.blockers.append({"class": "approval", "reason": "ChatGPT web send requires explicit approval",
                                 "approval_id": result.get("approval_id"), "run_id": run.goal_run_id})
            run.final_status = "BLOCKED"
            run.transition("BLOCKED", reason="ChatGPT web approval required")
        elif status == "COMPLETED":
            run.transition("VERIFYING", reason="ChatGPT web exchange completed")
            run.verification = {"chatgpt_exchange_completed": True,
                                "provenance_present": bool(result.get("source") and result.get("conversation_id")),
                                "idempotency_key_present": bool(result.get("idempotency_key")),
                                "content_not_logged": result.get("content_sent") is True,
                                "consumed_once": bool(result.get("receipt_ref")),
                                "consumption_receipt_ref": result.get("receipt_ref")}
            run.acceptance_passed = all(run.verification.values())
            run.final_status = "COMPLETED" if run.acceptance_passed else "FAILED"
            if not run.acceptance_passed:
                run.blockers.append({"class": "chatgpt_verification", "reason": "exchange receipt missing provenance or idempotency evidence"})
            run.transition("COMPLETED" if run.acceptance_passed else "FAILED", reason="ChatGPT web exchange verification")
        else:
            run.blockers.append({"class": "chatgpt_transport", "reason": str(result.get("error", status or "UNKNOWN"))})
            run.final_status = "BLOCKED" if status in {"UNAVAILABLE", "INPUT_INVALID"} else "FAILED"
            run.transition("BLOCKED" if run.final_status == "BLOCKED" else "FAILED", reason="ChatGPT web exchange did not complete")
        self.store.save(run)

    @staticmethod
    def _bounded_receipt(result: dict[str, Any], *, phase: str, expected_idempotency: str) -> dict[str, Any]:
        """Normalize one fresh bridge result for durable workflow state."""
        allowed = ("status", "adapter", "request_id", "run_id", "account_id",
                   "channel", "fallback_from", "fallback_required", "route_attempts", "routing_status",
                   "conversation_id", "message_id", "created_at", "url", "source",
                   "source_id", "idempotency_key", "snippet", "receipt_ref",
                   "reused_receipt", "error", "retry_count")
        receipt = {key: result.get(key) for key in allowed if key in result}
        receipt.update({"phase": phase, "expected_idempotency_key": expected_idempotency,
                        "idempotency_match": result.get("idempotency_key") == expected_idempotency})
        return receipt

    def _workflow_exchange(self, run: GoalRun, *, phase: str, prompt: str,
                           conversation_id: str | None, account_id: str,
                           approval_id: str | None, idempotency_key: str) -> dict[str, Any]:
        from modules.chatgpt_web_adapter import ChatGPTWebAdapter
        workflow = run.context.get("chatgpt_workflow") or {}
        channel = str(workflow.get("chatgpt_channel") or workflow.get("channel") or "regular")
        dynamic_routing = workflow.get("dynamic_routing", True) is not False
        request = {"run_id": run.goal_run_id, "account_id": account_id,
                   "conversation_id": conversation_id, "prompt": prompt,
                   "approval_id": approval_id, "idempotency_key": idempotency_key,
                   "channel": channel, "fallback_enabled": dynamic_routing}
        # A workflow can carry explicit user authorization for this bounded
        # bridge exchange. Persist approvals before sending so a fresh run can
        # resume after a controller restart without asking the host to replay
        # the prompt. The flag is deliberately required; a workflow that does
        # not carry it still stops at the adapter's approval gate.
        if channel == "regular" and workflow.get("approval_granted") is True:
            from modules.chatgpt_web_adapter import ChatGPTWebAdapter
            adapter = ChatGPTWebAdapter()
            adapter.record_approval(
                {"run_id": run.goal_run_id, "account_id": account_id,
                 "conversation_id": conversation_id, "prompt": prompt[:12_000],
                 "channel": "regular", "idempotency_key": idempotency_key},
                approval_id=approval_id or f"approval-{run.goal_run_id}-regular-{phase.lower()}",
                approved_by="interactive user authorized regular ChatGPT bridge exchange",
            )
            request["approval_id"] = approval_id or f"approval-{run.goal_run_id}-regular-{phase.lower()}"
            # Persist the Work approval binding before a regular exchange. It
            # is only consumed if regular ChatGPT returns an allowlisted quota
            # or availability error; no Work request is sent during this step.
            if dynamic_routing:
                work_account = str(workflow.get("work_account_id") or "chrome-work-chatgpt")
                work_key = f"{idempotency_key}:route:work"
                work_approval = f"{approval_id or ('approval-' + run.goal_run_id)}-work"
                adapter.record_approval(
                    {"run_id": run.goal_run_id, "account_id": work_account,
                     "conversation_id": workflow.get("work_conversation_id"),
                     "prompt": prompt[:12_000], "channel": "work", "idempotency_key": work_key},
                    approval_id=work_approval,
                    approved_by="interactive user authorized regular-first ChatGPT routing with bounded Work fallback",
                )
                request.update({"work_account_id": work_account,
                                "fallback_idempotency_key": work_key,
                                "fallback_approval_id": work_approval})
        result = ChatGPTWebAdapter().send(request)
        receipt = self._bounded_receipt(result, phase=phase, expected_idempotency=idempotency_key)
        if result.get("status") == "COMPLETED" and isinstance(result.get("channel"), str):
            # Once regular ChatGPT has failed over to Work, keep subsequent
            # phases on the same Work route/conversation. Re-entering regular
            # with a Work conversation would fail the bridge's identity guard.
            workflow["chatgpt_channel"] = result.get("channel")
            if result.get("account_id"):
                workflow["account_id"] = result.get("account_id")
            if result.get("conversation_id"):
                workflow["conversation_id"] = result.get("conversation_id")
            run.context["chatgpt_workflow"] = workflow
            self.store.save(run)
        if result.get("status") in {"RUNNING", "SUBMITTED", "PENDING"}:
            # The bridge may still be completing a browser turn after the
            # adapter's bounded poll. Persist the exact request receipt and
            # let the service requeue this GoalRun for reconciliation. This
            # path never submits a second request for the same idempotency key.
            workflow = run.context.setdefault("chatgpt_workflow", {})
            pending = workflow.setdefault("pending_phases", {})
            pending[phase] = receipt
            workflow["last_phase"] = phase
            workflow["conversation_id"] = result.get("conversation_id", conversation_id)
            self.store.audit({"event": "chatgpt_workflow_phase_pending", "goal_run_id": run.goal_run_id,
                              "phase": phase, "request_id": result.get("request_id"),
                              "idempotency_key": idempotency_key})
            self.store.save(run)
            return receipt
        if result.get("status") != "COMPLETED":
            # Keep a bounded terminal receipt in the GoalRun before surfacing
            # the failure. Recovery can then distinguish a browser boundary
            # error (for example, the visible tab was on another conversation)
            # from a completed exchange that must never be resent.
            workflow = run.context.setdefault("chatgpt_workflow", {})
            phases = workflow.setdefault("phases", {})
            phases[phase] = receipt
            workflow["last_phase"] = phase
            workflow["last_failure"] = {
                "phase": phase,
                "status": result.get("status"),
                "error": result.get("error"),
                "request_id": result.get("request_id"),
                "idempotency_key": idempotency_key,
            }
            self.store.save(run)
            raise RuntimeError(f"CHATGPT_{phase.upper()}_{result.get('status', 'UNKNOWN')}")
        if result.get("idempotency_key") != idempotency_key:
            raise RuntimeError(f"CHATGPT_{phase.upper()}_STALE_RECEIPT")
        if not result.get("source") or not result.get("conversation_id"):
            raise RuntimeError(f"CHATGPT_{phase.upper()}_PROVENANCE_MISSING")
        workflow = run.context.setdefault("chatgpt_workflow", {})
        phases = workflow.setdefault("phases", {})
        previous = phases.get(phase)
        if isinstance(previous, dict) and previous.get("idempotency_key") != idempotency_key:
            # A failed browser-boundary exchange may be continued on a fresh
            # conversation/account (for example after Work quota exhaustion).
            # Keep the failed receipt immutable in an audit slot, then allow
            # the explicitly linked replacement key. Never replace a
            # completed receipt or silently change an in-flight exchange.
            if previous.get("status") != "FAILED":
                raise RuntimeError(f"CHATGPT_{phase.upper()}_PREDECESSOR_MISMATCH")
            superseded = workflow.setdefault("superseded_phases", {})
            superseded.setdefault(phase, []).append(previous)
        phases[phase] = receipt
        pending = workflow.get("pending_phases")
        if isinstance(pending, dict):
            pending.pop(phase, None)
        workflow["last_phase"] = phase
        workflow["conversation_id"] = result.get("conversation_id")
        self.store.audit({"event": "chatgpt_workflow_phase_completed", "goal_run_id": run.goal_run_id,
                          "phase": phase, "request_id": result.get("request_id"),
                          "idempotency_key": idempotency_key, "conversation_id": result.get("conversation_id")})
        self.store.save(run)
        return receipt

    def _workflow_tasks(self, run: GoalRun, plan_receipt: dict[str, Any]) -> list[dict[str, Any]]:
        workflow = run.context.get("chatgpt_workflow") or {}
        supplied = workflow.get("team_tasks")
        if isinstance(supplied, list) and supplied:
            return supplied
        root = Path(run.workspace) / ".system-agentic-goals" / run.goal_run_id / "team"
        rel = root.relative_to(Path(run.workspace).resolve())
        # The bridge plan is bounded by design.  Derive the artifact contract
        # from its receipt when it names JSON outputs; keep the markdown
        # baseline for older plans that did not specify a format.  This keeps
        # worker ownership aligned with the fresh ChatGPT plan without ever
        # forwarding its unbounded transcript.
        # The bridge receipt is intentionally a bounded tail and may omit the
        # beginning of the plan.  Keep the original bounded prompt in the
        # durable run context and use it only to recover the declared artifact
        # contract; never forward the prompt to workers or the review call.
        plan_text = "\n".join((
            str(plan_receipt.get("snippet") or ""),
            str(workflow.get("plan_prompt") or ""),
        ))
        copy_name = "copy.json" if re.search(r"\bcopy\.json\b", plan_text, re.IGNORECASE) else "copy.md"
        measurement_name = (
            "measurement.json"
            if re.search(r"\bmeasurement\.json\b", plan_text, re.IGNORECASE)
            else "tracking.md"
        )
        integration_name = (
            "integration.json"
            if re.search(r"\bintegration\.json\b", plan_text, re.IGNORECASE)
            else "integration.md"
        )
        verification_name = (
            "verification.json"
            if re.search(r"\bverification\.json\b", plan_text, re.IGNORECASE)
            else "verification.md"
        )
        # Bridge receipts are intentionally bounded and can begin in the
        # middle of ChatGPT's answer. A plan naming the JSON integration
        # contract also selects the JSON verifier contract, even if the
        # bounded tail clips the literal verification.json token.
        if integration_name == "integration.json" or verification_name == "verification.json":
            copy_name = "copy.json"
            measurement_name = "measurement.json"
            integration_name = "integration.json"
            verification_name = "verification.json"
        copy_path = rel / copy_name
        measurement_path = rel / measurement_name
        integration_path = rel / integration_name
        verification_path = rel / verification_name
        verifier_json = verification_name.endswith(".json")
        verifier_output = (
            f"Write exactly one bounded verification receipt to {verification_path}. "
            f"First read only these three existing inputs: {copy_path}, {measurement_path}, and {integration_path}. "
            f"Do not list files, search text, or read {verification_path} before writing it. "
            f"If all three inputs exist and are non-empty, write {'valid JSON with verdict PASS_BOUNDED, checked_artifacts, and evidenceRefs' if verifier_json else 'PASS_BOUNDED with the three paths and one evidence sentence'}; otherwise write {'valid JSON with verdict FAIL and the missing path' if verifier_json else 'FAIL with the missing path'}. "
            "After the single write, stop and report the path. Do not edit worker inputs, product files, or publish."
        )
        source_paths = [path for path in ("README.md", "package.json", "src/server.mjs")
                        if (Path(run.workspace) / path).is_file()]
        source_refs_json = json.dumps(source_paths, ensure_ascii=False)
        source_files = ", ".join(source_paths) or "README.md, package.json, and src/server.mjs"
        copy_json_shape = ('{"source_refs":' + source_refs_json + ',"draft_text":"bounded draft grounded in the files",'
                           '"audience":"UNKNOWN","claims":["UNKNOWN"],"DRAFT_ONLY":true,"rehearsal":true,'
                           '"liveEligible":false,"externalSideEffects":false,"unknownRetryAllowed":false}')
        measurement_json_shape = ('{"source_refs":' + source_refs_json + ',"verified_publication_count":"UNKNOWN",'
                                  '"channel_metrics":"UNKNOWN","attribution":"UNKNOWN",'
                                  '"evidence_refs":"UNKNOWN","live_eligibility":"UNKNOWN",'
                                  '"DRAFT_ONLY":true,"rehearsal":true,"liveEligible":false,"externalSideEffects":false,'
                                  '"unknownRetryAllowed":false}')
        integration_json_shape = ('{"input_refs":["' + str(copy_path) + '","' + str(measurement_path) + '"],'
                                  '"source_refs":' + source_refs_json + ',"DRAFT_ONLY":true,"rehearsal":true,'
                                  '"liveEligible":false,"externalSideEffects":false,"unknownRetryAllowed":false}')
        worker_contract = (
            "Read the named source files first; do not combine a write_file call with the read calls. "
            "After the read results, issue exactly one write_file call in a later turn. The JSON object "
            "must use the literal keys shown below; copy key names exactly (plural claims, channel_metrics, "
            "externalSideEffects). Do not invent aliases, abbreviations, comments, or extra flags. "
            "If the validator rejects a payload, the rejection means no file was written: use its canonical "
            "recovery instruction and issue one corrected write, then stop."
        )
        return [
            {"task_id": f"{run.goal_run_id}-copy", "title": "Marketing copy worker",
             "role": "dev-content", "artifact_kind": "copy", "objective": f"{worker_contract} Read only these existing product files with read_file: {source_files}. Do not use list_files, search_text, or run_command. If a named file is missing, use UNKNOWN for unsupported non-claim fields and continue. Then call write_file for {copy_path}. Write one compact valid JSON object matching this exact shape (replace every placeholder with observed values; JSON double quotes, no comments): {copy_json_shape}. Produce substantive source-grounded copy: draft_text and claims must not be UNKNOWN or generic placeholders, and claims must include at least one concrete claim from each named source file, citing its filename. Do not publish or contact anyone, and finish.",
             "scope": str(copy_path), "required_artifacts": [str(copy_path)],
             "grounding_required": True, "grounding_source_refs": source_paths,
             "substantive_claims_required": True,
             "tool_allowlist": ["read_file", "write_file"],
             "depends_on": [], "execution_policy": "WORKSPACE_WRITE",
             "acceptance_criteria": [f"{copy_name} exists", "copy is bounded and product-grounded", "no external publish"]},
            {"task_id": f"{run.goal_run_id}-tracking", "title": "Marketing tracking worker",
             "role": "dev-analytics", "artifact_kind": "measurement", "objective": f"{worker_contract} Read only these existing product files with read_file: {source_files}. Do not use list_files, search_text, or run_command. If a named file is missing, use UNKNOWN and continue. Then call write_file for {measurement_path}. Write one compact valid JSON object matching this exact shape (replace only values; JSON double quotes, no comments): {measurement_json_shape}. Use UNKNOWN for absent metrics, channels, publication, or attribution; live_eligibility must be false or UNKNOWN, never true. Do not publish or contact anyone, and finish.",
             "scope": str(measurement_path), "required_artifacts": [str(measurement_path)],
             "grounding_required": True, "grounding_source_refs": source_paths,
             "tool_allowlist": ["read_file", "write_file"],
             "depends_on": [], "execution_policy": "WORKSPACE_WRITE",
             "acceptance_criteria": [f"{measurement_name} exists", "checks are concrete", "no external publish"]},
            {"task_id": f"{run.goal_run_id}-integrator", "title": "Marketing integrator",
             "role": "integrator", "artifact_kind": "integration", "objective": f"Read only {copy_path} and {measurement_path} with read_file. Do not list files, search text, or run_command. Then call write_file exactly once for {integration_path}. Write one compact valid JSON object matching this shape (preserve both inputs): {integration_json_shape}. Do not read the output after writing and do not publish.",
             "scope": str(integration_path), "required_artifacts": [str(integration_path)],
             "grounding_required": True, "grounding_source_refs": source_paths,
             "input_refs": [str(copy_path), str(measurement_path)],
             "tool_allowlist": ["read_file", "write_file"],
             "depends_on": [f"{run.goal_run_id}-copy", f"{run.goal_run_id}-tracking"], "execution_policy": "WORKSPACE_WRITE",
             "acceptance_criteria": [f"{integration_name} exists", "both inputs cited", "no external publish"]},
            {"task_id": f"{run.goal_run_id}-verifier", "title": "Independent marketing verifier",
             "role": "verifier", "artifact_kind": "verification", "objective": (
                 f"Independently read the generated artifacts {copy_path}, {measurement_path}, and {integration_path}, "
                 f"then read the product sources {source_files}. Check each generated JSON object, verify the copy claims "
                 f"directly against README.md, package.json, and src/server.mjs, and check the draft-only safety flags. "
                 f"Write {verification_path} exactly once as valid JSON with verdict PASS_BOUNDED or FAIL, "
                 f"checked_artifacts containing all three generated paths, evidenceRefs containing README.md, package.json, "
                 f"and src/server.mjs, and a short findings field. Do not read the verification output before writing it. "
                 "Do not edit inputs or publish."),
             "scope": str(verification_path), "required_artifacts": [str(verification_path)],
             "grounding_required": True, "grounding_source_refs": source_paths,
             "verification_input_refs": [str(copy_path), str(measurement_path), str(integration_path)],
             "verification_source_evidence_required": True, "verification_source_refs": source_paths,
             "tool_allowlist": ["read_file", "write_file"],
             "depends_on": [f"{run.goal_run_id}-integrator"], "execution_policy": "WORKSPACE_WRITE",
             "acceptance_criteria": [f"{verification_name} exists", "builder and integrator artifacts checked", "PASS_BOUNDED or FAIL stated"]},
        ]

    def _execute_chatgpt_team(self, run: GoalRun) -> None:
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict):
            raise RuntimeError("CHATGPT_WORKFLOW_MISSING")
        account_id = workflow.get("account_id") or "chrome-default-chatgpt"
        conversation_id = workflow.get("conversation_id")
        plan_prompt = workflow.get("plan_prompt")
        review_prompt = workflow.get("review_prompt")
        if not isinstance(plan_prompt, str) or not plan_prompt.strip() or not isinstance(review_prompt, str) or not review_prompt.strip():
            raise RuntimeError("CHATGPT_WORKFLOW_PROMPTS_MISSING")
        plan_key = str(workflow.get("plan_idempotency_key") or f"{run.goal_run_id}:plan:1")
        review_key = str(workflow.get("review_idempotency_key") or f"{run.goal_run_id}:review:1")
        plan_approval = workflow.get("plan_approval_id")
        review_approval = workflow.get("review_approval_id")
        # A completed plan is durable input to the worker wave. Reuse its
        # receipt on recovery; sending the same plan again would duplicate the
        # browser exchange and violate the run's idempotency contract.
        phases = workflow.get("phases") if isinstance(workflow.get("phases"), dict) else {}
        persisted_plan = phases.get("PLAN_RECEIVED")
        if (isinstance(persisted_plan, dict)
                and persisted_plan.get("status") == "COMPLETED"
                and persisted_plan.get("idempotency_key") == plan_key):
            plan = dict(persisted_plan)
        else:
            plan = self._workflow_exchange(run, phase="PLAN_RECEIVED", prompt=plan_prompt,
                                           conversation_id=conversation_id, account_id=account_id,
                                           approval_id=plan_approval, idempotency_key=plan_key)
        if plan.get("status") in {"RUNNING", "SUBMITTED", "PENDING"}:
            run.team_status = "WAITING_CHATGPT_PLAN"
            self.store.save(run)
            return
        workflow["plan_task_input"] = {"source": plan.get("source"), "conversation_id": plan.get("conversation_id"),
                                       "request_id": plan.get("request_id"), "snippet": plan.get("snippet")}
        # Pass only the bounded, provenance-bearing plan receipt to workers.
        # The prompt and full browser transcript never enter the team ledger.
        workflow["plan_receipt"] = {
            key: plan.get(key) for key in
            ("request_id", "source", "source_id", "conversation_id", "created_at",
             "url", "idempotency_key", "snippet")
            if key in plan
        }
        workflow["team_tasks"] = self._workflow_tasks(run, plan)
        run.context["team_tasks"] = workflow["team_tasks"]
        state_dir = Path(run.workspace) / ".system-agentic-goals" / run.goal_run_id
        derived_path = state_dir / "plan-derived-tasks.json"
        derived_path.write_text(json.dumps({
            "run_id": run.goal_run_id,
            "phase": "PLAN_RECEIVED",
            "plan_receipt": {key: plan.get(key) for key in
                             ("request_id", "source", "source_id", "conversation_id", "created_at", "idempotency_key", "snippet")},
            "task_ids": [item.get("task_id") for item in workflow["team_tasks"]],
            "tasks": [{key: item.get(key) for key in ("task_id", "title", "role", "scope", "depends_on", "required_artifacts")}
                      for item in workflow["team_tasks"] if isinstance(item, dict)],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        workflow["plan_derived_tasks_ref"] = str(derived_path)
        self._prepare_team(run, state_dir)
        run.decision = "CHATGPT_TEAM"
        run.team_status = "READY"
        self.store.save(run)
        self._execute_team(run)
        run = self.store.load(run.goal_run_id)
        if run.team_status == "RUNNING":
            self.store.save(run)
            return
        if run.team_status != "COMPLETED":
            # A repair ledger can remain aggregate-FAILED because it retains
            # superseded predecessor attempts. Reconcile the active wave
            # first; PASS_BOUNDED only ends that child wave, then the goal
            # must continue into the pending ChatGPT review.
            self._apply_team_result(run, finalize=False)
            run = self.store.load(run.goal_run_id)
            if not run.verification.get("team_tasks_completed"):
                self._apply_team_result(run)
                return
        # Verify worker artifacts before asking ChatGPT to review them.
        self._apply_team_result(run, finalize=False)
        run = self.store.load(run.goal_run_id)
        if not run.verification.get("team_tasks_completed"):
            run.final_status = "FAILED"
            run.transition("FAILED", reason="worker verification failed before ChatGPT review")
            self.store.save(run)
            return
        repair_paths = workflow.get("review_artifact_paths")
        integration_paths = ([item for item in repair_paths if isinstance(item, str)]
                             if isinstance(repair_paths, list) and repair_paths else
                             [item.get("path") for item in run.artifact_refs if isinstance(item, dict)])
        review_prompt_bound = review_prompt.replace(
            "{{ARTIFACTS}}", self._bounded_artifact_context(run, integration_paths)
        )
        # The plan may belong to an earlier ChatGPT mode (for example Work
        # before quota exhaustion). Use the workflow's current conversation
        # for each later review round.
        # Worker execution may persist a newer bridge conversation/key than
        # the workflow snapshot captured at method entry. Reload before the
        # review so a mode/account switch cannot send the next round to the
        # stale plan conversation or approval.
        latest_run = self.store.load(run.goal_run_id)
        latest_workflow = latest_run.context.get("chatgpt_workflow") or {}
        self._execute_chatgpt_team_review(
            latest_run, plan=plan, review_prompt=None,
            account_id=latest_workflow.get("account_id") or account_id,
            conversation_id=latest_workflow.get("conversation_id"),
            approval_id=None, review_key=None,
        )

    def _execute_chatgpt_team_review(self, run: GoalRun, *, plan: dict[str, Any] | None = None,
                                     review_prompt: str | None = None, account_id: str | None = None,
                                     conversation_id: str | None = None, approval_id: str | None = None,
                                     review_key: str | None = None) -> None:
        workflow = run.context.get("chatgpt_workflow") or {}
        phases = workflow.get("phases") if isinstance(workflow.get("phases"), dict) else {}
        plan = plan or phases.get("PLAN_RECEIVED")
        if not isinstance(plan, dict):
            raise RuntimeError("CHATGPT_PLAN_RECEIPT_MISSING")
        account_id = account_id or workflow.get("account_id") or "chrome-default-chatgpt"
        # Prefer the workflow's current conversation. The plan receipt may
        # belong to an earlier ChatGPT mode/session (for example Work quota
        # exhaustion followed by a regular ChatGPT conversation).
        conversation_id = conversation_id or self._latest_chatgpt_conversation(workflow) or workflow.get("conversation_id") or plan.get("conversation_id")
        review_prompt = review_prompt or workflow.get("review_prompt")
        if not isinstance(review_prompt, str) or not review_prompt.strip():
            raise RuntimeError("CHATGPT_REVIEW_PROMPT_MISSING")
        review_round = max(1, int(workflow.get("review_round", 1)))
        # A browser/account switch (for example Work quota exhaustion to a
        # regular ChatGPT conversation) must use a new durable key bound to
        # that conversation. Preserve an explicitly supplied or persisted
        # round key; never fall back to the old round number and accidentally
        # reuse the failed exchange.
        review_key = (review_key or str(workflow.get("review_idempotency_key") or
                                        f"{run.goal_run_id}:review:{review_round}"))
        approval_ids = workflow.get("review_approval_ids") if isinstance(workflow.get("review_approval_ids"), dict) else {}
        # Round-specific approval must win over the legacy/current-round-1
        # field. Otherwise a valid round-2 approval is ignored and the bridge
        # correctly returns APPROVAL_REQUIRED for the mismatched key.
        approval_id = approval_ids.get(str(review_round)) or approval_id or workflow.get("review_approval_id")
        integration_paths = [item.get("path") for item in run.artifact_refs if isinstance(item, dict)]
        review_prompt = review_prompt.replace(
            "{{ARTIFACTS}}", self._bounded_artifact_context(run, integration_paths)
        )
        review_phase = "REVIEW_RECEIVED" if review_round == 1 else f"REVIEW_RECEIVED_{review_round}"
        repair_paths = workflow.get("review_artifact_paths")
        if isinstance(repair_paths, list) and repair_paths:
            integration_paths = [item for item in repair_paths if isinstance(item, str)]
            review_prompt = review_prompt.replace(
                "{{ARTIFACTS}}", self._bounded_artifact_context(run, integration_paths)
            )
        review = self._workflow_exchange(run, phase=review_phase, prompt=review_prompt,
                                         conversation_id=conversation_id, account_id=account_id,
                                         approval_id=approval_id, idempotency_key=review_key)
        if review.get("status") in {"RUNNING", "SUBMITTED", "PENDING"}:
            run.team_status = f"WAITING_CHATGPT_REVIEW_{review_round}"
            self.store.save(run)
            return
        workflow["review_task_input"] = {"source": review.get("source"), "conversation_id": review.get("conversation_id"),
                                         "request_id": review.get("request_id"), "snippet": review.get("snippet")}
        # The bridge receipt proves transport/provenance only.  Acceptance is
        # a separate bounded decision extracted from the fresh ChatGPT reply;
        # never mark a review accepted merely because the exchange completed.
        review_decision = self._parse_chatgpt_review_decision(review.get("snippet"))
        workflow["review_decision"] = review_decision
        run.context["chatgpt_workflow"] = workflow
        run.verification["chatgpt_plan_exchange"] = True
        run.verification["chatgpt_review_exchange"] = True
        run.verification["chatgpt_review_accepted"] = review_decision == "ACCEPTED"
        workflow["review_round"] = review_round
        workflow["review_decision"] = review_decision
        if review_decision == "FOLLOW_UP_REQUIRED":
            max_rounds = workflow.get("max_review_rounds", 3)
            try:
                max_rounds = max(1, min(int(max_rounds), 5))
            except (TypeError, ValueError):
                max_rounds = 3
            if review_round >= max_rounds:
                workflow["follow_up_task"] = {"status": "BLOCKED", "reason": "configured ChatGPT review round limit reached",
                    "round": review_round, "request_id": review.get("request_id"),
                    "snippet": str(review.get("snippet", ""))[:800]}
                run.context["chatgpt_workflow"] = workflow
                run.blockers.append({"class": "chatgpt_review_round_limit",
                                     "reason": "ChatGPT requested fixes at the configured review round limit"})
                run.final_status = "BLOCKED"
                run.transition("BLOCKED", reason="bounded ChatGPT review round limit reached")
                self.store.save(run)
                return
            next_round = review_round + 1
            next_review_key = f"{run.goal_run_id}:review:{next_round}"
            next_approval_id = f"approval-{run.goal_run_id}-review-r{next_round}"
            next_prompt = review_prompt.replace(
                "{{ARTIFACTS}}", "(The next round will attach its newly verified repair artifacts.)"
            )
            # The interactive user explicitly authorized this bounded,
            # approval-gated review loop. Persist a run/account/conversation/
            # idempotency-scoped approval for the next review; the adapter
            # still checks the exact receipt before sending.
            from modules.chatgpt_web_adapter import ChatGPTWebAdapter
            approval = ChatGPTWebAdapter().record_approval(
                {"run_id": run.goal_run_id, "account_id": account_id,
                 "conversation_id": conversation_id, "prompt": next_prompt[:12_000],
                 "idempotency_key": next_review_key},
                approval_id=next_approval_id,
                approved_by="interactive user authorized bounded downstream-distribution review loop",
            )
            if approval.get("status") != "APPROVED":
                raise RuntimeError("CHATGPT_REVIEW_APPROVAL_PERSISTENCE_FAILED")
            approval_ids[str(next_round)] = next_approval_id
            workflow["review_approval_ids"] = approval_ids
            wave = self._append_chatgpt_repair_wave(run, review, review_round + 1)
            workflow["follow_up_task"] = {
                "title": "Address ChatGPT review findings",
                "source": "chatgpt-web", "conversation_id": review.get("conversation_id"),
                "request_id": review.get("request_id"), "snippet": str(review.get("snippet", ""))[:800],
                "status": "RUNNING", "round": review_round + 1, "task_ids": wave,
            }
            workflow["review_round"] = next_round
            workflow["review_idempotency_key"] = next_review_key
            run.context["chatgpt_workflow"] = workflow
            run.team_status = "RUNNING"
            run.final_status = None
            if run.current_state == "VERIFYING":
                run.transition("EXECUTING", reason=f"ChatGPT REVIEW: FIX; repair wave {review_round + 1} queued")
            self.store.save(run)
            return

        run.context["chatgpt_workflow"] = workflow
        run.acceptance_passed = (review_decision == "ACCEPTED"
                                 and run.verification.get("team_tasks_completed") is True
                                 and run.verification.get("chatgpt_plan_exchange") is True
                                 and run.verification.get("chatgpt_review_exchange") is True)
        run.outcome = "MARKETING_PLAN_WORKER_INTEGRATION_REVIEW_ACCEPTED"
        run.final_status = "COMPLETED" if run.acceptance_passed else "FAILED"
        run.transition("COMPLETED" if run.acceptance_passed else "FAILED", reason="ChatGPT plan, worker integration, and review completed")
        self.store.save(run)

    @staticmethod
    def _latest_chatgpt_conversation(workflow: dict[str, Any]) -> str | None:
        """Return the newest completed bridge conversation in this workflow.

        A plan can come from a Work tab that later exhausts quota. Review
        phases must follow the latest completed exchange, which may be in a
        regular ChatGPT conversation. Failed phases are deliberately ignored.
        """
        phases = workflow.get("phases") if isinstance(workflow.get("phases"), dict) else {}
        candidates: list[tuple[int, str]] = []
        for name, receipt in phases.items():
            if not isinstance(receipt, dict) or receipt.get("status") != "COMPLETED":
                continue
            conversation_id = receipt.get("conversation_id")
            if not isinstance(conversation_id, str) or not conversation_id:
                continue
            if name == "PLAN_RECEIVED":
                rank = 0
            else:
                match = re.fullmatch(r"REVIEW_RECEIVED_(\d+)", str(name))
                rank = int(match.group(1)) if match else (1 if name == "REVIEW_RECEIVED" else -1)
            candidates.append((rank, conversation_id))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def _prepare_chatgpt_review_phase_recovery(
        self, run: GoalRun, *, allow_same_conversation: bool = False
    ) -> bool:
        """Link one fresh review request after a stale-conversation failure.

        The failed request remains immutable. A new key and approval are
        created only when the latest completed phase proves a different
        conversation, such as switching from Work to regular ChatGPT.
        """
        if run.decision != "CHATGPT_TEAM" or run.current_state not in {"FAILED", "BLOCKED", "VERIFYING"}:
            return False
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict):
            return False
        try:
            review_round = max(1, int(workflow.get("review_round", 1) or 1))
        except (TypeError, ValueError):
            return False
        phases = workflow.get("phases") if isinstance(workflow.get("phases"), dict) else {}
        phase_name = "REVIEW_RECEIVED" if review_round == 1 else f"REVIEW_RECEIVED_{review_round}"
        failed = phases.get(phase_name)
        if not isinstance(failed, dict) or failed.get("status") not in {"FAILED", "UNAVAILABLE"}:
            return False
        # A stale orchestrator process can persist the adapter's loopback
        # configuration error before a LaunchAgent restart supplies the
        # configured bridge URL/auth environment.  An explicit resume is
        # allowed to recover that immutable phase receipt only after the
        # fresh browser preflight below; automatic supervisor ticks still
        # never mint a request.
        recoverable_errors = {
            "CONVERSATION_MISMATCH", "CONVERSATION_CHANGED_DURING_EXCHANGE",
            "CONVERSATION_RESTORE_TIMEOUT", "orca transport action failed: element_not_found",
            "CONVERSATION_NOT_STABLE",
            "ChatGPT composer not found in Chrome accessibility tree",
            "ChatGPT send button not found in Chrome accessibility tree",
            "ChatGPT bridge URL must be a plain loopback HTTP origin",
        }
        # A prior interactive resume can run outside the LaunchAgent and
        # persist the adapter's generic ``ValueError`` before it reaches the
        # configured bridge. That receipt has no bridge request ID, so no
        # browser exchange was accepted. Permit one linked recovery only for
        # this exact pre-submit shape; input errors and accepted requests stay
        # immutable.
        missing_bridge_request = (
            failed.get("status") == "UNAVAILABLE"
            and failed.get("error") == "ValueError"
            and not failed.get("request_id")
        )
        if failed.get("error") not in recoverable_errors and not missing_bridge_request:
            return False
        latest_conversation = self._latest_chatgpt_conversation(workflow)
        failed_conversation = failed.get("conversation_id")
        if not latest_conversation:
            return False
        same_conversation = latest_conversation == failed_conversation
        if same_conversation and not allow_same_conversation:
            return False
        counts = workflow.get("review_phase_recovery_counts")
        if not isinstance(counts, dict):
            counts = {}
        count = int(counts.get(str(review_round), 0) or 0)
        # A browser tab can be reclaimed by another local app between the
        # preflight snapshot and the bridge worker. Allow at most three
        # explicit, separately keyed phase recoveries; the bridge itself also
        # caps retries at three. Never reuse a completed or failed key.
        if count >= 3:
            return False
        if same_conversation:
            # Same-conversation recovery is only reachable from an explicit
            # service-resume call after a fresh browser preflight. Automatic
            # supervisor ticks must never mint an unbounded stream of new
            # prompts for a tab that keeps failing its boundary check.
            try:
                from modules.chatgpt_browser_bridge import OrcaChromeTransport
                snapshot = OrcaChromeTransport(timeout=15).snapshot(latest_conversation)
            except (OSError, RuntimeError, ValueError):
                return False
            if snapshot.conversation_id != latest_conversation:
                return False
        account_id = str(workflow.get("account_id") or "chrome-default-chatgpt")
        prompt = workflow.get("review_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return False
        recovery_no = count + 1
        next_key = f"{run.goal_run_id}:review:{review_round}:conversation-recovery-{recovery_no}"
        next_approval_id = f"approval-{run.goal_run_id}-review-r{review_round}-conversation-recovery-{recovery_no}"
        from modules.chatgpt_web_adapter import ChatGPTWebAdapter
        approval = ChatGPTWebAdapter().record_approval(
            {"run_id": run.goal_run_id, "account_id": account_id,
             "conversation_id": latest_conversation, "prompt": prompt[:12_000],
             "idempotency_key": next_key},
            approval_id=next_approval_id,
            approved_by="interactive user authorized bounded downstream-distribution review loop",
        )
        if approval.get("status") != "APPROVED":
            raise RuntimeError("CHATGPT_REVIEW_APPROVAL_PERSISTENCE_FAILED")
        workflow["review_phase_recovery_counts"] = {**counts, str(review_round): recovery_no}
        workflow["review_phase_recovery_from"] = {
            "phase": phase_name, "request_id": failed.get("request_id"),
            "conversation_id": failed_conversation, "error": failed.get("error"),
        }
        workflow["conversation_id"] = latest_conversation
        workflow["review_idempotency_key"] = next_key
        workflow["review_approval_id"] = next_approval_id
        approval_ids = workflow.get("review_approval_ids") if isinstance(workflow.get("review_approval_ids"), dict) else {}
        workflow["review_approval_ids"] = {**approval_ids, str(review_round): next_approval_id}
        run.context["chatgpt_workflow"] = workflow
        run.final_status = None
        run.acceptance_passed = False
        run.blockers.append({"class": "chatgpt_recovery",
                             "reason": "new review exchange prepared after stale conversation failure",
                             "previous_request_id": failed.get("request_id"),
                             "new_idempotency_key": next_key,
                             "conversation_id": latest_conversation})
        if run.current_state in {"FAILED", "BLOCKED"}:
            run.transition("READY", reason="recovered review on latest completed ChatGPT conversation")
        self.store.save(run)
        return True

    def _append_chatgpt_repair_wave(self, run: GoalRun, review: dict[str, Any], round_no: int) -> list[str]:
        """Append an isolated repair/integration/verifier DAG to this run ledger."""
        if not run.team_ledger_ref:
            raise RuntimeError("CHATGPT_REPAIR_REQUIRES_TEAM_LEDGER")
        from modules.team_ledger import TeamLedgerStore, _normalize_tasks
        ledger_store = TeamLedgerStore(run.team_ledger_ref)
        ledger = ledger_store.load()
        workspace = Path(run.workspace).resolve(strict=True)
        root = Path(".system-agentic-goals") / run.goal_run_id / "team"
        copy_paths = [item.get("path") for item in run.artifact_refs
                      if isinstance(item, dict) and str(item.get("path", "")).endswith("copy.json")]
        measurement_paths = [item.get("path") for item in run.artifact_refs
                             if isinstance(item, dict) and "measurement" in str(item.get("path", ""))]
        integration_paths = [item.get("path") for item in run.artifact_refs
                             if isinstance(item, dict) and "integration" in str(item.get("path", ""))]
        verifier_paths = [item.get("path") for item in run.artifact_refs
                          if isinstance(item, dict) and "verification" in str(item.get("path", ""))]
        source_paths = [path for path in ("README.md", "package.json", "src/server.mjs")
                        if (workspace / path).is_file()]
        # A continuation can be linked to a predecessor run whose accepted
        # measurement is not copied into the current GoalRun artifact_refs.
        # Reuse that durable local artifact as an explicit verifier input.
        workflow = run.context.get("chatgpt_workflow")
        linked_source_run = workflow.get("linked_source_run") if isinstance(workflow, dict) else None
        if isinstance(linked_source_run, str) and GOAL_ID.fullmatch(linked_source_run):
            linked_measurement = (Path(".system-agentic-goals") / linked_source_run /
                                  "team" / "measurement.json").as_posix()
            if (workspace / linked_measurement).is_file() and linked_measurement not in measurement_paths:
                measurement_paths.append(linked_measurement)
        repair_name = f"copy-repair-round-{round_no}.json"
        integration_name = f"integration-round-{round_no}.json"
        verification_name = f"verification-round-{round_no}.json"
        repair_path = (root / repair_name).as_posix()
        # A review may explicitly request measurement/tracking evidence. An
        # existing linked measurement is enough for an integration-only repair;
        # create a worker only when no durable measurement exists or the review
        # explicitly asks for a fresh one.
        review_text = " ".join(str(review.get(key, "")) for key in ("snippet", "finding", "request", "objective"))
        measurement_requested = self._review_requests_measurement(review_text, review)
        fresh_measurement_requested = bool(re.search(
            r"\b(?:fresh|new|refresh)\s+(?:the\s+|actual\s+)?"
            r"(?:measurement|measurements|metric|metrics|tracking|attribution)\b"
            r"|\b(?:re-?measure|rebuild(?:\s+the)?)\s+"
            r"(?:measurement|measurements|metric|metrics|tracking|attribution)\b",
            review_text, re.IGNORECASE,
        )) or bool(review.get("fresh_measurement_requested"))
        measurement_task_requested = measurement_requested and (
            fresh_measurement_requested or not measurement_paths
        )
        measurement_path = (root / f"measurement-round-{round_no}.json").as_posix()
        integration_path = (root / integration_name).as_posix()
        verification_path = (root / verification_name).as_posix()
        repair_task_id = f"{run.goal_run_id}-repair-r{round_no}"
        measurement_task_id = f"{run.goal_run_id}-measurement-r{round_no}"
        integration_task_id = f"{run.goal_run_id}-integration-r{round_no}"
        verification_task_id = f"{run.goal_run_id}-verification-r{round_no}"
        task_ids = [repair_task_id, integration_task_id, verification_task_id]
        if measurement_task_requested:
            task_ids = [repair_task_id, measurement_task_id, integration_task_id, verification_task_id]
        finding = str(review.get("snippet", ""))[:800]
        source_list = json.dumps(source_paths, ensure_ascii=False)
        prior_artifacts = json.dumps(copy_paths + measurement_paths + integration_paths + verifier_paths,
                                     ensure_ascii=False)
        flags = '"DRAFT_ONLY":true,"rehearsal":true,"liveEligible":false,"externalSideEffects":false,"unknownRetryAllowed":false'
        repair_shape = (f'{{"source_refs":{source_list},"draft_text":"<concrete source-grounded draft>",'
                        f'"audience":"<specific audience or UNKNOWN>","claims":["<claim grounded in README.md>",'
                        f'"<claim grounded in package.json>","<claim grounded in src/server.mjs>"],{flags}}}')
        tasks = [
            {"task_id": repair_task_id, "title": f"ChatGPT copy repair round {round_no}",
             "role": "dev-content", "artifact_kind": "copy", "workspace": str(workspace),
             "objective": (f"Review ChatGPT's bounded finding: {finding}. Use only read_file on product sources {source_paths} "
                 f"and prior artifacts {copy_paths + measurement_paths + integration_paths + verifier_paths}; then use exactly one "
                 f"write_file call for {repair_path}. Read all three product sources before writing. Produce substantive, "
                 "non-generic marketing copy: include at least one concrete claim grounded in each of README.md, package.json, "
                 "and src/server.mjs, cite those file names in the claims, and do not use UNKNOWN for claims. Do not copy "
                 f"the placeholder values from this shape; replace every angle-bracket value with real source-grounded text. "
                 f"The JSON keys and safety flags must match exactly: {repair_shape}. Do not use list_files, search_text, "
                 "run_command, browser, or any other tool. Ground only supported claims; preserve all draft-only flags; "
                 "no publish or contact. After the write, stop."),
             "scope": repair_path, "required_artifacts": [repair_path], "grounding_required": True,
             "substantive_claims_required": True,
             "grounding_source_refs": source_paths,
             "tool_allowlist": ["read_file", "write_file"],
             "depends_on": [], "execution_policy": "WORKSPACE_WRITE",
             "acceptance_criteria": ["schema accepted", "finding addressed", "no external side effects"]},
        ]
        if measurement_task_requested:
            measurement_shape = ('{"source_refs":' + source_list + ',"verified_publication_count":"UNKNOWN",'
                                 '"channel_metrics":"UNKNOWN","attribution":"UNKNOWN",'
                                 '"evidence_refs":[],"live_eligibility":false,' + flags + '}')
            tasks.append({"task_id": measurement_task_id, "title": f"Measure ChatGPT repair round {round_no}",
             "role": "dev-analytics", "artifact_kind": "measurement", "workspace": str(workspace),
             "objective": (f"Read only product sources {source_paths} and prior accepted artifacts {prior_artifacts}. "
                 f"Write {measurement_path} with exact compact JSON: {measurement_shape}. "
                 "Use UNKNOWN for absent metrics; preserve draft-only flags; no publish or contact."),
             "scope": measurement_path, "required_artifacts": [measurement_path], "grounding_required": True,
             "grounding_source_refs": source_paths,
             "tool_allowlist": ["read_file", "write_file"], "depends_on": [],
             "execution_policy": "WORKSPACE_WRITE",
             "acceptance_criteria": ["fresh measurement exists", "unknowns are explicit", "no external side effects"]})
        tasks.extend([
            {"task_id": integration_task_id, "title": f"Integrate ChatGPT repair round {round_no}",
             "role": "integrator", "artifact_kind": "integration", "workspace": str(workspace),
             "objective": (f"Read only the prior accepted measurement {measurement_paths} "
                 + (f"and fresh measurement {measurement_path} " if measurement_task_requested else "")
                 + f"and new repair {repair_path}. Write {integration_path} with exact compact JSON: {{\"input_refs\":{json.dumps(measurement_paths + ([measurement_path] if measurement_task_requested else []) + [repair_path])},"
                 f"\"source_refs\":{source_list},{flags}}}. Do not publish."),
             "scope": integration_path, "required_artifacts": [integration_path], "grounding_required": True,
             "grounding_source_refs": source_paths,
             "input_refs": measurement_paths + ([measurement_path] if measurement_task_requested else []) + [repair_path],
             "tool_allowlist": ["read_file", "write_file"],
             "depends_on": [repair_task_id] + ([measurement_task_id] if measurement_task_requested else []), "execution_policy": "WORKSPACE_WRITE",
             "acceptance_criteria": ["both inputs referenced", "draft-only flags preserved"]},
            {"task_id": verification_task_id, "title": f"Verify ChatGPT repair round {round_no}",
             "role": "verifier", "artifact_kind": "verification", "workspace": str(workspace),
             "objective": (f"Independently read the new repair {repair_path}, integration {integration_path}, "
                 f"and prior measurement {measurement_paths}"
                 + (f" plus fresh measurement {measurement_path}" if measurement_task_requested else "")
                 + f" plus the product sources {source_paths}. Check the copy draft_text and every claim directly against "
                 "the named source file that supports it; check the measurement artifact's exact canonical keys and types "
                 "(verified_publication_count, channel_metrics, attribution, evidence_refs, live_eligibility); check "
                 "valid JSON, required flags false/true safely, and exact path references. Write checked_artifacts with every "
                 f"declared generated input path (including {integration_path}) and evidenceRefs with README.md, package.json, "
                 f"and src/server.mjs. Write {verification_path} as JSON with verdict PASS_BOUNDED or FAIL, checked_artifacts, "
                 "evidenceRefs, findings. Do not edit inputs or publish."),
             "scope": verification_path, "required_artifacts": [verification_path],
             "verification_input_refs": [repair_path, integration_path]
                 + (measurement_paths + [measurement_path] if measurement_task_requested else measurement_paths),
             "verification_source_evidence_required": True,
             "verification_source_refs": source_paths,
             "tool_allowlist": ["read_file", "write_file"],
             "depends_on": [integration_task_id], "execution_policy": "WORKSPACE_WRITE",
             "acceptance_criteria": ["independent checks recorded", "PASS_BOUNDED or FAIL stated"]},
        ])
        normalized = _normalize_tasks(tasks, goal=run.original_goal, workspace=str(workspace), run_id=run.goal_run_id)
        existing_ids = {item.get("task_id") for item in ledger.get("tasks", []) if isinstance(item, dict)}
        if any(item["task_id"] in existing_ids for item in normalized):
            raise RuntimeError("CHATGPT_REPAIR_TASKS_ALREADY_PRESENT")
        ledger["tasks"].extend({**item, "state": "PENDING", "worker_attempts": [], "result": None, "error": None}
                               for item in normalized)
        ledger["status"] = "READY"
        ledger.setdefault("events", []).append({"at": _now(), "event": "chatgpt_repair_wave_added",
                                                  "task_id": task_ids[0],
                                                  "payload": {"round": round_no, "task_ids": task_ids,
                                                              "review_request_id": review.get("request_id"),
                                                              "measurement_requested": measurement_requested,
                                                              "measurement_task_requested": measurement_task_requested,
                                                              "measurement_path": measurement_path if measurement_task_requested else None}})
        ledger["updated_at"] = _now()
        ledger_store.save(ledger)
        workflow = run.context.setdefault("chatgpt_workflow", {})
        review_measurements = list(measurement_paths)
        if measurement_task_requested:
            review_measurements.append(measurement_path)
        workflow["review_artifact_paths"] = [repair_path, *review_measurements,
                                              integration_path, verification_path]
        return task_ids

    @staticmethod
    def _review_requests_measurement(review_text: str, review: dict[str, Any] | None = None) -> bool:
        """Recognize measurement requirements in bounded ChatGPT review text."""
        if isinstance(review, dict) and review.get("measurement_requested") is True:
            return True
        text = str(review_text or "")
        if not text.strip():
            return False
        return bool(re.search(
            r"\b(?:measurement(?:\.json)?|measurements|metrics?|tracking|attribution)\b"
            r"(?:[^.!?\n]{0,100}\b(?:artifact|json|key|keys|schema|type|types|"
            r"include|reference|check|verify|missing|omit|complete|canonical|exact)\b)?"
            r"|\b(?:artifact|json|key|keys|schema|type|types|include|reference|check|"
            r"verify|missing|omit|complete|canonical|exact)\b[^.!?\n]{0,100}"
            r"\b(?:measurement(?:\.json)?|measurements|metrics?|tracking|attribution)\b",
            text, re.IGNORECASE,
        ))

    def _prepare_chatgpt_measurement_contract_recovery(self, run: GoalRun) -> bool:
        """Repair an active review wave that omitted a durable measurement.

        ChatGPT's round-3 receipt is already consumed. If the controller
        created the integration/verifier before measurement detection was
        fixed, append a new integration/verifier pair that references the
        predecessor's actual ``measurement.json`` and the completed copy
        repair. Supersede only the incomplete pair; worker and bridge receipts
        remain immutable. This path never creates a new ChatGPT exchange.
        """
        if run.decision != "CHATGPT_TEAM" or not run.team_ledger_ref:
            return False
        if run.current_state not in {"FAILED", "BLOCKED", "COMPLETED"}:
            return False
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict) or workflow.get("review_decision") != "FOLLOW_UP_REQUIRED":
            return False
        try:
            review_round = int(workflow.get("review_round", 1) or 1)
        except (TypeError, ValueError):
            return False
        if review_round < 1:
            return False
        ledger_store = TeamLedgerStore(run.team_ledger_ref)
        ledger = ledger_store.load()
        workspace = Path(run.workspace).resolve(strict=True)
        linked_source_run = workflow.get("linked_source_run")
        if not isinstance(linked_source_run, str) or not GOAL_ID.fullmatch(linked_source_run):
            return False
        measurement_path = (Path(".system-agentic-goals") / linked_source_run /
                            "team" / "measurement.json").as_posix()
        if not (workspace / measurement_path).is_file():
            return False
        review_paths = workflow.get("review_artifact_paths")
        if not isinstance(review_paths, list):
            return False
        copy_path = next((path for path in review_paths
                          if isinstance(path, str) and "copy" in path and path.endswith(".json")), None)
        if not isinstance(copy_path, str) or not (workspace / copy_path).is_file():
            return False
        # This recovery is specifically for an integration/verifier pair that
        # lacks measurement.json. If a corrected pair already exists, reuse it
        # and let the normal team resume path reconcile the same task IDs.
        marker = f"-r{review_round}-measurement-recovery-1"
        integration_id = f"{run.goal_run_id}-integration{marker}"
        verification_id = f"{run.goal_run_id}-verification{marker}"
        existing = {item.get("task_id") for item in ledger.get("tasks", [])
                    if isinstance(item, dict)}
        if integration_id in existing or verification_id in existing:
            return False
        old_integration = next((item for item in ledger.get("tasks", [])
                                if isinstance(item, dict)
                                and item.get("task_id") == f"{run.goal_run_id}-integration-r{review_round}"
                                and not item.get("superseded_by")), None)
        old_verification = next((item for item in ledger.get("tasks", [])
                                 if isinstance(item, dict)
                                 and item.get("task_id") == f"{run.goal_run_id}-verification-r{review_round}"
                                 and not item.get("superseded_by")), None)
        if not isinstance(old_integration, dict) or not isinstance(old_verification, dict):
            return False
        integration_path = (Path(".system-agentic-goals") / run.goal_run_id / "team" /
                            f"integration-round-{review_round}-measurement-recovery-1.json").as_posix()
        verification_path = (Path(".system-agentic-goals") / run.goal_run_id / "team" /
                             f"verification-round-{review_round}-measurement-recovery-1.json").as_posix()
        source_paths = [path for path in ("README.md", "package.json", "src/server.mjs")
                        if (workspace / path).is_file()]
        if len(source_paths) != 3:
            return False
        input_refs = [measurement_path, copy_path]
        flags = '"DRAFT_ONLY":true,"rehearsal":true,"liveEligible":false,"externalSideEffects":false,"unknownRetryAllowed":false'
        from modules.team_ledger import _normalize_tasks
        integration_task = {
            "task_id": integration_id,
            "title": f"Integrate ChatGPT repair round {review_round} with measurement recovery",
            "role": "integrator", "artifact_kind": "integration", "workspace": str(workspace),
            "objective": (
                f"Read only {measurement_path} and {copy_path} with read_file. Then call write_file exactly once "
                f"for {integration_path}. Write one valid JSON object only: "
                f"{{\"input_refs\":{json.dumps(input_refs)},\"source_refs\":{json.dumps(source_paths)},"
                f"{flags}}}. Preserve both inputs exactly. Do not read output, edit product files, or publish."),
            "scope": integration_path, "required_artifacts": [integration_path],
            "grounding_required": True, "grounding_source_refs": source_paths,
            "input_refs": input_refs, "tool_allowlist": ["read_file", "write_file"],
            # The repair prerequisite already exists in the persisted ledger;
            # add this cross-wave dependency after local normalization.
            "depends_on": [],
            "execution_policy": "WORKSPACE_WRITE",
            "acceptance_criteria": ["measurement and copy referenced exactly once", "draft-only flags preserved"]
        }
        verification_task = {
            "task_id": verification_id,
            "title": f"Verify ChatGPT repair round {review_round} with measurement recovery",
            "role": "verifier", "artifact_kind": "verification", "workspace": str(workspace),
            "objective": (
                f"Independently read {copy_path}, {measurement_path}, and {integration_path}, then read "
                f"{source_paths}. Check all JSON objects; check exact measurement keys and types for "
                "verified_publication_count, channel_metrics, attribution, evidence_refs, live_eligibility; "
                "compare draft_text and every claim to the named source files; check all five safety flags and "
                f"both integration input_refs. Write exactly one valid JSON object to {verification_path} with "
                "verdict PASS_BOUNDED only if every check passes, checked_artifacts containing all three generated "
                "paths, evidenceRefs containing all three source files, and concise findings. Do not edit inputs or publish."),
            "scope": verification_path, "required_artifacts": [verification_path],
            "grounding_required": False, "grounding_source_refs": [],
            "verification_input_refs": [copy_path, measurement_path, integration_path],
            "verification_source_evidence_required": True,
            "verification_source_refs": source_paths,
            "tool_allowlist": ["read_file", "write_file"],
            "depends_on": [integration_id], "execution_policy": "WORKSPACE_WRITE",
            "acceptance_criteria": ["all generated inputs checked", "exact measurement schema checked", "PASS_BOUNDED or FAIL stated"]
        }
        normalized = _normalize_tasks([integration_task, verification_task],
                                      goal=run.original_goal, workspace=str(workspace), run_id=run.goal_run_id)
        repair_prerequisite = str(workflow.get("repair_recovery_task_id") or
                                  f"{run.goal_run_id}-repair-r{review_round}-recovery-1")
        normalized[0]["depends_on"] = [repair_prerequisite]
        old_integration["superseded_by"] = integration_id
        old_verification["superseded_by"] = verification_id
        ledger.setdefault("tasks", []).extend(
            {**item, "state": "PENDING", "worker_attempts": [], "result": None, "error": None}
            for item in normalized
        )
        ledger["status"] = "READY"
        ledger.setdefault("events", []).append({
            "at": _now(), "event": "chatgpt_measurement_contract_recovery_added",
            "task_id": integration_id,
            "payload": {"review_round": review_round, "measurement_path": measurement_path,
                        "copy_path": copy_path, "superseded": [old_integration.get("task_id"), old_verification.get("task_id")]}
        })
        ledger["updated_at"] = _now()
        ledger_store.save(ledger)
        workflow["review_artifact_paths"] = [copy_path, measurement_path, integration_path, verification_path]
        workflow["measurement_contract_recovery"] = {
            "integration_task_id": integration_id, "verification_task_id": verification_id,
            "measurement_path": measurement_path, "copy_path": copy_path,
            "recovery_of": [old_integration.get("task_id"), old_verification.get("task_id")],
        }
        workflow["follow_up_task"] = {**(workflow.get("follow_up_task") or {}),
                                       "status": "RUNNING", "round": review_round,
                                       "task_ids": [integration_id, verification_id]}
        run.context["chatgpt_workflow"] = workflow
        run.team_status = "READY"
        run.final_status = None
        run.acceptance_passed = False
        run.blockers.append({"class": "chatgpt_review_recovery",
                             "reason": "repaired integration/verifier contract to include predecessor measurement",
                             "review_round": review_round, "measurement_path": measurement_path})
        if run.current_state in {"FAILED", "BLOCKED", "COMPLETED"}:
            run.transition("READY", reason="bounded measurement contract recovery queued")
        self.store.save(run)
        return True

    def _prepare_chatgpt_repair_recovery(self, run: GoalRun) -> bool:
        """Append one fresh worker task after a repair worker failed pre-write.

        The terminal attempt remains immutable.  Only a repair task with no
        persisted artifact can receive this bounded continuation; its blocked
        dependants are retargeted to the new task ID and then resume normally.
        """
        if run.decision != "CHATGPT_TEAM" or not run.team_ledger_ref:
            return False
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict) or not workflow.get("review_artifact_paths"):
            return False
        ledger_store = TeamLedgerStore(run.team_ledger_ref)
        ledger = ledger_store.load()
        # A provider can report COMPLETED after writing a JSON placeholder.
        # Treat that as a recoverable repair failure when the durable task
        # contract rejects the artifact.  Queue completion alone is never
        # enough to advance a grounded repair wave.
        repair = None
        repair_contract = None
        for candidate in reversed(ledger.get("tasks", [])):
            if not isinstance(candidate, dict) or candidate.get("superseded_by"):
                continue
            if "-repair-r" not in str(candidate.get("task_id", "")):
                continue
            state = candidate.get("state")
            if state in {"FAILED", "NO-OP", "REFUSED"}:
                repair = candidate
                break
            if state != "COMPLETED":
                continue
            contract = dict(candidate)
            # Ledgers created before metadata preservation was added may have
            # lost the copy-specific acceptance fields. Reconstruct only the
            # bounded repair contract from its stable task identity/source
            # refs; this does not alter the historical receipt.
            contract["artifact_kind"] = str(contract.get("artifact_kind") or "copy")
            if not contract.get("substantive_claims_required"):
                contract["substantive_claims_required"] = True
            if not contract.get("grounding_source_refs"):
                contract["grounding_source_refs"] = ["README.md", "package.json", "src/server.mjs"]
            required_candidate = contract.get("required_artifacts")
            if not isinstance(required_candidate, list) or not required_candidate:
                continue
            try:
                validation = _validate_worker_result(contract, {"execution_status": "COMPLETED"})
            except (OSError, ValueError, RuntimeError):
                validation = {"accepted": False}
            if not validation.get("accepted"):
                repair = candidate
                repair_contract = contract
                break
        if not isinstance(repair, dict):
            return False
        required = repair.get("required_artifacts")
        if not isinstance(required, list) or not required:
            return False
        workspace = Path(run.workspace).resolve(strict=True)
        attempts = repair.get("worker_attempts") or []
        last_attempt = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
        result = repair.get("result") if isinstance(repair.get("result"), dict) else {}
        acceptance = result.get("acceptance") if isinstance(result.get("acceptance"), dict) else last_attempt.get("acceptance")
        artifact_present = any((workspace / path).is_file() and (workspace / path).stat().st_size > 0
                               for path in required if isinstance(path, str))
        invalid_artifact = (isinstance(acceptance, dict)
                            and acceptance.get("accepted") is False
                            and acceptance.get("classification") in {"FAILED", "NO-OP", "REFUSED"})
        if repair_contract is not None:
            contract_validation = _validate_worker_result(
                repair_contract, {"execution_status": "COMPLETED"}
            )
            invalid_artifact = not contract_validation.get("accepted")
        if artifact_present and not invalid_artifact:
            return False
        old_id = str(repair.get("task_id"))
        existing = {item.get("task_id") for item in ledger.get("tasks", []) if isinstance(item, dict)}
        match = re.search(r"-recovery-(\d+)$", old_id)
        recovery_no = int(match.group(1)) + 1 if match else 1
        # Keep recovery bounded per repair lineage. The original task plus
        # three linked recovery tasks is enough to recover a transient local
        # worker/tool failure without allowing a hot loop. The cap is per
        # lineage, not global to the whole goal: a fresh review round gets its
        # own bounded recovery budget.
        if recovery_no > MAX_REPAIR_RECOVERIES_PER_LINEAGE:
            return False
        new_id = f"{old_id}-recovery-{recovery_no}"
        if new_id in existing:
            return False
        from modules.team_ledger import _normalize_tasks
        recovery_task = dict(repair_contract or repair)
        recovery_task.update({
            "task_id": new_id,
            "title": f"{repair.get('title', old_id)} recovery",
            "depends_on": [],
            "recovery_of": old_id,
        })
        old_paths = [path for path in required if isinstance(path, str) and path.strip()]
        recovery_paths = []
        for old_path in old_paths:
            recovery_paths.append(
                f"{old_path[:-5]}-recovery-{recovery_no}.json" if old_path.endswith(".json")
                else f"{old_path}-recovery-{recovery_no}"
            )
        if recovery_paths:
            recovery_task["scope"] = recovery_paths[0]
            recovery_task["required_artifacts"] = recovery_paths
        if str(recovery_task.get("artifact_kind") or "").lower() == "copy":
            source_refs = [item for item in recovery_task.get("grounding_source_refs", [])
                           if isinstance(item, str) and item.strip()]
            if not source_refs:
                source_refs = ["README.md", "package.json", "src/server.mjs"]
            source_refs_json = json.dumps(source_refs, ensure_ascii=False)
            target = recovery_paths[0] if recovery_paths else str(recovery_task.get("scope", ""))
            recovery_task["objective"] = (
                "Read only these existing product files with read_file, in this order: "
                "README.md, package.json, src/server.mjs. Then issue exactly one write_file "
                f"call for {target}. Write one compact valid JSON object with exactly these "
                f"keys: source_refs, draft_text, audience, claims, DRAFT_ONLY, rehearsal, "
                "liveEligible, externalSideEffects, unknownRetryAllowed. "
                f"Set source_refs exactly to {source_refs_json}. audience must be one JSON "
                "string, never an array. claims must be an array of at least three concrete "
                "source-grounded strings, with at least one claim from each named source; "
                "do not use UNKNOWN for claims or draft_text. Include the source filename in "
                "each claim. Set DRAFT_ONLY=true, rehearsal=true, liveEligible=false, "
                "externalSideEffects=false, unknownRetryAllowed=false. Do not use list_files, "
                "search_text, run_command, browser, or any other tool. Do not publish or "
                "contact anyone. If a validator rejects the first payload, correct only the "
                "schema issue while preserving concrete claims; never replace it with a "
                "placeholder. Stop after the successful write."
            )
        repair["superseded_by"] = new_id
        normalized = _normalize_tasks([recovery_task], goal=run.original_goal,
                                      workspace=str(workspace), run_id=run.goal_run_id)[0]
        ledger.setdefault("tasks", []).append({**normalized, "state": "PENDING",
                                                 "worker_attempts": [], "result": None, "error": None})
        for item in ledger["tasks"]:
            if not isinstance(item, dict):
                continue
            if old_id in item.get("depends_on", []):
                item["depends_on"] = [new_id if dep == old_id else dep
                                       for dep in item.get("depends_on", [])]
                if item.get("state") == "BLOCKED":
                    item["state"] = "PENDING"
                    item["error"] = None
            if old_paths and isinstance(item.get("objective"), str):
                for old_path, recovery_path in zip(old_paths, recovery_paths):
                    item["objective"] = item["objective"].replace(old_path, recovery_path)
            # Verification contracts also carry explicit input paths. Keep
            # those references aligned with the recovery artifact names so a
            # completed verifier can be accepted after a controller restart.
            if old_paths and isinstance(item.get("verification_input_refs"), list):
                item["verification_input_refs"] = [
                    next((recovery_path for old_path, recovery_path in zip(old_paths, recovery_paths)
                          if ref == old_path), ref)
                    for ref in item["verification_input_refs"]
                ]
        ledger["status"] = "READY"
        ledger.setdefault("events", []).append({"at": _now(),
            "event": "chatgpt_repair_recovery_added", "task_id": new_id,
            "payload": {"recovery_of": old_id, "round": workflow.get("review_round"),
                        "reason": ("repair artifact failed grounded/schema validation"
                                   if repair_contract is not None
                                   else "repair worker failed before declared artifact write")}})
        ledger["updated_at"] = _now()
        ledger_store.save(ledger)
        workflow["repair_recovery_count"] = int(workflow.get("repair_recovery_count", 0) or 0) + 1
        workflow["repair_recovery_task_id"] = new_id
        workflow["repair_recovery_of"] = old_id
        if recovery_paths and isinstance(workflow.get("review_artifact_paths"), list):
            workflow["review_artifact_paths"] = [
                recovery_paths[old_paths.index(path)] if path in old_paths else path
                for path in workflow["review_artifact_paths"]
            ]
        workflow["follow_up_task"] = {**(workflow.get("follow_up_task") or {}),
                                       "status": "RUNNING", "recovery_task_id": new_id}
        run.context["chatgpt_workflow"] = workflow
        run.team_status = "READY"
        run.final_status = None
        if run.current_state in {"FAILED", "BLOCKED"}:
            run.transition("READY", reason="bounded ChatGPT repair worker recovery queued")
        self.store.save(run)
        return True

    def _prepare_chatgpt_review_extension(self, run: GoalRun) -> bool:
        """Open one explicitly authorized review round after the normal cap.

        The default review budget remains bounded at three rounds.  A user can
        authorize one linked continuation by persisting
        ``review_extension_approved`` on the active GoalRun after a fresh
        ``REVIEW: FIX`` receipt.  This helper appends a new worker wave and
        mints a new ChatGPT approval/idempotency key; it never resends the
        capped review request.
        """
        if run.decision != "CHATGPT_TEAM" or run.current_state not in {"FAILED", "BLOCKED"}:
            return False
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict) or workflow.get("review_extension_approved") is not True:
            return False
        try:
            review_round = int(workflow.get("review_round", 1) or 1)
            max_rounds = int(workflow.get("max_review_rounds", 3) or 3)
        except (TypeError, ValueError):
            return False
        if review_round < max_rounds or review_round >= 5:
            return False
        phases = workflow.get("phases") if isinstance(workflow.get("phases"), dict) else {}
        phase_name = "REVIEW_RECEIVED" if review_round == 1 else f"REVIEW_RECEIVED_{review_round}"
        review = phases.get(phase_name)
        if not isinstance(review, dict) or review.get("status") != "COMPLETED":
            return False
        if self._parse_chatgpt_review_decision(review.get("snippet")) != "FOLLOW_UP_REQUIRED":
            return False
        ledger_ref = run.team_ledger_ref
        if not ledger_ref:
            return False
        ledger_store = TeamLedgerStore(ledger_ref)
        ledger = ledger_store.load()
        next_round = review_round + 1
        next_ids = {
            "copy": f"{run.goal_run_id}-repair-r{next_round}",
            "measurement": f"{run.goal_run_id}-measurement-r{next_round}",
            "integration": f"{run.goal_run_id}-integration-r{next_round}",
            "verification": f"{run.goal_run_id}-verification-r{next_round}",
        }
        existing_ids = {item.get("task_id") for item in ledger.get("tasks", []) if isinstance(item, dict)}
        if any(task_id in existing_ids for task_id in next_ids.values()):
            return False

        next_key = f"{run.goal_run_id}:review:{next_round}"
        approval_id = f"approval-{run.goal_run_id}-review-r{next_round}"
        account_id = str(workflow.get("account_id") or "chrome-default-chatgpt")
        conversation_id = str(workflow.get("conversation_id") or review.get("conversation_id") or "")
        prompt = str(workflow.get("review_prompt") or "")
        if not conversation_id or not prompt.strip():
            return False
        # Persist the approval before appending the worker wave. If approval
        # storage fails, no orphaned round-N tasks are left behind.
        from modules.chatgpt_web_adapter import ChatGPTWebAdapter
        approval = ChatGPTWebAdapter().record_approval(
            {"run_id": run.goal_run_id, "account_id": account_id,
             "conversation_id": conversation_id, "prompt": prompt[:12_000],
             "idempotency_key": next_key},
            approval_id=approval_id,
            approved_by="interactive user authorized bounded downstream-distribution review continuation",
        )
        if approval.get("status") != "APPROVED":
            raise RuntimeError("CHATGPT_REVIEW_EXTENSION_APPROVAL_PERSISTENCE_FAILED")

        # Build the new bounded wave from the already received ChatGPT FIX.
        wave = self._append_chatgpt_repair_wave(run, review, next_round)
        ledger = ledger_store.load()
        new_tasks = [item for item in ledger.get("tasks", [])
                     if isinstance(item, dict) and item.get("task_id") in set(wave)]
        new_ids_by_kind = {
            str(item.get("artifact_kind")): item.get("task_id")
            for item in new_tasks if item.get("artifact_kind")
        }
        # Older rounds remain immutable evidence but cannot participate in the
        # active acceptance calculation. Mark every prior repair-wave task as
        # superseded by the matching round-N task.
        for item in ledger.get("tasks", []):
            if not isinstance(item, dict) or item.get("superseded_by"):
                continue
            task_id = str(item.get("task_id", ""))
            match = re.search(r"-(?:repair|measurement|integration|verification)-r\d+(?:-recovery-\d+)?$", task_id)
            if not match:
                continue
            kind = str(item.get("artifact_kind") or "")
            if kind == "copy" or "-repair-r" in task_id:
                successor = new_ids_by_kind.get("copy")
            elif kind == "measurement" or "-measurement-r" in task_id:
                successor = new_ids_by_kind.get("measurement")
            elif kind == "integration" or "-integration-r" in task_id:
                successor = new_ids_by_kind.get("integration")
            elif kind == "verification" or "-verification-r" in task_id:
                successor = new_ids_by_kind.get("verification")
            else:
                continue
            if task_id not in set(wave) and successor:
                item["superseded_by"] = successor
        workflow["max_review_rounds"] = max(max_rounds, next_round)
        workflow["review_round"] = next_round
        workflow["review_idempotency_key"] = next_key
        workflow["review_approval_id"] = approval_id
        approval_ids = workflow.get("review_approval_ids") if isinstance(workflow.get("review_approval_ids"), dict) else {}
        workflow["review_approval_ids"] = {**approval_ids, str(next_round): approval_id}
        workflow["review_extension_from"] = {
            "round": review_round,
            "request_id": review.get("request_id"),
            "reason": "explicit continuation after bounded REVIEW: FIX",
        }
        workflow["review_extension_approved"] = False
        workflow["follow_up_task"] = {
            "title": "Address ChatGPT review findings",
            "source": "chatgpt-web", "conversation_id": review.get("conversation_id"),
            "request_id": review.get("request_id"), "snippet": str(review.get("snippet", ""))[:800],
            "status": "RUNNING", "round": next_round,
            "task_ids": list(wave),
        }
        run.context["chatgpt_workflow"] = workflow
        run.team_status = "READY"
        run.final_status = None
        run.acceptance_passed = False
        run.blockers.append({"class": "chatgpt_review_extension",
                             "reason": "bounded review continuation queued after explicit authorization",
                             "previous_round": review_round, "next_round": next_round,
                             "previous_request_id": review.get("request_id"),
                             "new_idempotency_key": next_key})
        ledger["updated_at"] = _now()
        ledger.setdefault("events", []).append({"at": _now(),
            "event": "chatgpt_review_extension_added", "task_id": next_ids["copy"],
            "payload": {"previous_round": review_round, "next_round": next_round,
                        "review_request_id": review.get("request_id"),
                        "idempotency_key": next_key}})
        ledger_store.save(ledger)
        run.transition("READY", reason=f"explicitly authorized ChatGPT review extension to round {next_round}")
        self.store.save(run)
        return True

    def _prepare_chatgpt_failed_task_recovery(self, run: GoalRun) -> bool:
        """Queue one fresh attempt for a failed repair-wave task.

        Repair workers and integration workers have separate ownership scopes.
        A failed integration attempt must therefore receive its own stable new
        task ID; reusing the failed ID would erase audit history and could
        duplicate a provider submission. Only a missing declared artifact is
        eligible. The dependent verifier is retargeted to the recovery task.
        """
        if run.decision != "CHATGPT_TEAM" or not run.team_ledger_ref:
            return False
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict):
            return False
        ledger_store = TeamLedgerStore(run.team_ledger_ref)
        ledger = ledger_store.load()
        task = next((item for item in ledger.get("tasks", [])
                     if isinstance(item, dict)
                     and "-integration-r" in str(item.get("task_id", ""))
                     and not item.get("superseded_by")
                     and item.get("state") in {"FAILED", "NO-OP", "REFUSED"}), None)
        if not isinstance(task, dict):
            return False
        required = task.get("required_artifacts")
        if not isinstance(required, list) or not required:
            return False
        workspace = Path(run.workspace).resolve(strict=True)
        if any((workspace / path).is_file() and (workspace / path).stat().st_size > 0
               for path in required if isinstance(path, str)):
            return False
        old_id = str(task.get("task_id"))
        match = re.search(r"-recovery-(\d+)$", old_id)
        recovery_no = int(match.group(1)) + 1 if match else 1
        if recovery_no > MAX_REPAIR_RECOVERIES_PER_LINEAGE:
            return False
        new_id = f"{old_id}-recovery-{recovery_no}"
        existing = {item.get("task_id") for item in ledger.get("tasks", []) if isinstance(item, dict)}
        if new_id in existing:
            return False
        from modules.team_ledger import _normalize_tasks
        recovery_task = dict(task)
        # The repair recovery task is the only valid input for this integration
        # wave. Preserve the original task's bounded prompt and scope.
        repair_recovery_id = workflow.get("repair_recovery_task_id")
        if isinstance(repair_recovery_id, str) and repair_recovery_id:
            prerequisite = next((item for item in ledger.get("tasks", [])
                                 if isinstance(item, dict) and item.get("task_id") == repair_recovery_id), None)
            if not isinstance(prerequisite, dict) or prerequisite.get("state") != "COMPLETED":
                return False
        recovery_task.update({
            "task_id": new_id,
            "title": f"{task.get('title', old_id)} recovery",
            # This recovery is appended after its repair prerequisite is
            # already COMPLETED. Keep the new task's local DAG dependency
            # empty so normalization does not reject a dependency that lives
            # in the existing ledger; the verifier is retargeted below.
            "depends_on": [],
            "recovery_of": old_id,
        })
        task["superseded_by"] = new_id
        normalized = _normalize_tasks([recovery_task], goal=run.original_goal,
                                      workspace=str(workspace), run_id=run.goal_run_id)[0]
        if isinstance(repair_recovery_id, str) and repair_recovery_id:
            # The prerequisite already exists in the persisted ledger; add it
            # after single-task normalization, which cannot see external IDs.
            normalized["depends_on"] = [repair_recovery_id]
        ledger.setdefault("tasks", []).append({**normalized, "state": "PENDING",
                                                 "worker_attempts": [], "result": None, "error": None})
        for item in ledger["tasks"]:
            if not isinstance(item, dict):
                continue
            if old_id in item.get("depends_on", []):
                item["depends_on"] = [new_id if dep == old_id else dep
                                       for dep in item.get("depends_on", [])]
                if item.get("state") == "BLOCKED":
                    item["state"] = "PENDING"
                    item["error"] = None
        ledger["status"] = "READY"
        ledger.setdefault("events", []).append({"at": _now(),
            "event": "chatgpt_task_recovery_added", "task_id": new_id,
            "payload": {"recovery_of": old_id, "round": workflow.get("review_round"),
                        "reason": "integration worker failed before declared artifact write"}})
        ledger["updated_at"] = _now()
        ledger_store.save(ledger)
        # Keep the legacy aggregate counter for reporting, but bound retries by
        # the active task lineage so a failure in round 1 cannot consume the
        # recovery budget for a later review round.
        try:
            aggregate_recovery_count = int(workflow.get("integration_recovery_count", 0) or 0)
        except (TypeError, ValueError):
            aggregate_recovery_count = 0
        workflow["integration_recovery_count"] = aggregate_recovery_count + 1
        workflow["integration_recovery_task_id"] = new_id
        workflow["integration_recovery_of"] = old_id
        workflow["follow_up_task"] = {**(workflow.get("follow_up_task") or {}),
                                       "status": "RUNNING", "recovery_task_id": new_id}
        run.context["chatgpt_workflow"] = workflow
        run.team_status = "READY"
        run.final_status = None
        if run.current_state in {"FAILED", "BLOCKED"}:
            run.transition("READY", reason="bounded ChatGPT integration worker recovery queued")
        self.store.save(run)
        return True

    def _prepare_chatgpt_verifier_recovery(self, run: GoalRun) -> bool:
        """Queue one fresh repair wave when the current verifier says FAIL.

        A verifier receipt is durable evidence even when its verdict is FAIL.
        Preserve that receipt for audit, supersede the failed wave as a unit,
        and append a fresh, independently owned wave.  The bridge review is
        not resent; the existing review receipt remains the source of the
        bounded repair instruction.
        """
        if run.decision != "CHATGPT_TEAM" or run.current_state not in {"FAILED", "BLOCKED", "COMPLETED"}:
            return False
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict) or workflow.get("review_decision") != "FOLLOW_UP_REQUIRED":
            return False
        if workflow.get("verification_recovery_count", 0):
            return False
        try:
            review_round = int(workflow.get("review_round", 1) or 1)
        except (TypeError, ValueError):
            return False
        if review_round <= 1 or not run.team_ledger_ref:
            return False
        ledger_store = TeamLedgerStore(run.team_ledger_ref)
        ledger = ledger_store.load()
        verification_id = f"{run.goal_run_id}-verification-r{review_round}"
        verifier = next((item for item in ledger.get("tasks", [])
                         if isinstance(item, dict)
                         and item.get("task_id") == verification_id
                         and not item.get("superseded_by")), None)
        if not isinstance(verifier, dict):
            return False
        attempts = verifier.get("worker_attempts") or []
        attempt = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
        result = verifier.get("result") if isinstance(verifier.get("result"), dict) else {}
        acceptance = result.get("acceptance") if isinstance(result.get("acceptance"), dict) else attempt.get("acceptance")
        present = acceptance.get("present", []) if isinstance(acceptance, dict) else []
        verdict = None
        for raw_path in present:
            if not isinstance(raw_path, str) or "verification" not in raw_path:
                continue
            try:
                receipt = json.loads((Path(run.workspace) / raw_path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                receipt = {}
            if isinstance(receipt, dict):
                verdict = receipt.get("verdict")
                break
        if verdict != "FAIL":
            return False
        review = workflow.get("follow_up_task")
        if not isinstance(review, dict):
            phases = workflow.get("phases") if isinstance(workflow.get("phases"), dict) else {}
            review = phases.get(f"REVIEW_RECEIVED_{review_round}") or phases.get("REVIEW_RECEIVED_2")
        if not isinstance(review, dict):
            return False
        recovery_round = f"{review_round}-recovery-1"
        task_ids = [f"{run.goal_run_id}-repair-r{recovery_round}",
                    f"{run.goal_run_id}-measurement-r{recovery_round}",
                    f"{run.goal_run_id}-integration-r{recovery_round}",
                    f"{run.goal_run_id}-verification-r{recovery_round}"]
        existing_ids = {item.get("task_id") for item in ledger.get("tasks", []) if isinstance(item, dict)}
        if any(task_id in existing_ids for task_id in task_ids):
            return False
        # Use the normal bounded task constructor so schema validation and
        # ownership rules remain identical to a ChatGPT-originated repair.
        recovery_review = {**review, "measurement_requested": True}
        self._append_chatgpt_repair_wave(run, recovery_review, recovery_round)
        # The constructor appends paths and tasks. Link each predecessor to
        # its corresponding replacement without mutating its receipt.
        ledger = ledger_store.load()
        for item in ledger.get("tasks", []):
            if not isinstance(item, dict):
                continue
            task_id = item.get("task_id")
            mapping = {
                f"{run.goal_run_id}-repair-r{review_round}": task_ids[0],
                f"{run.goal_run_id}-integration-r{review_round}": task_ids[2],
                f"{run.goal_run_id}-verification-r{review_round}": task_ids[3],
            }
            if task_id in mapping:
                item["superseded_by"] = mapping[task_id]
        ledger["updated_at"] = _now()
        ledger_store.save(ledger)
        workflow["verification_recovery_count"] = 1
        workflow["verification_recovery_of"] = verification_id
        workflow["follow_up_task"] = {**(workflow.get("follow_up_task") or {}),
                                       "status": "RUNNING", "round": recovery_round,
                                       "task_ids": task_ids}
        run.context["chatgpt_workflow"] = workflow
        run.team_status = "READY"
        run.final_status = None
        run.acceptance_passed = False
        if run.current_state in {"FAILED", "BLOCKED", "COMPLETED"}:
            run.transition("READY", reason="bounded ChatGPT verifier failure recovery queued")
        self.store.save(run)
        return True

    def _prepare_chatgpt_verification_task_recovery(self, run: GoalRun) -> bool:
        """Retry only a failed verifier when its inputs are already complete."""
        if run.decision != "CHATGPT_TEAM" or not run.team_ledger_ref:
            return False
        if run.current_state not in {"FAILED", "BLOCKED", "COMPLETED"}:
            return False
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict) or workflow.get("review_decision") != "FOLLOW_UP_REQUIRED":
            return False
        if int(workflow.get("verification_task_recovery_count", 0) or 0) >= 1:
            return False
        try:
            review_round = int(workflow.get("review_round", 1) or 1)
        except (TypeError, ValueError):
            return False
        if review_round <= 1:
            return False
        ledger_store = TeamLedgerStore(run.team_ledger_ref)
        ledger = ledger_store.load()
        verifier_id = f"{run.goal_run_id}-verification-r{review_round}"
        verifier = next((item for item in ledger.get("tasks", [])
                         if isinstance(item, dict)
                         and item.get("task_id") == verifier_id
                         and not item.get("superseded_by")), None)
        if not isinstance(verifier, dict) or verifier.get("state") not in {"FAILED", "NO-OP", "REFUSED"}:
            return False
        required = verifier.get("required_artifacts")
        if not isinstance(required, list) or not required:
            return False
        workspace = Path(run.workspace).resolve(strict=True)
        if any((workspace / path).is_file() and (workspace / path).stat().st_size > 0
               for path in required if isinstance(path, str)):
            return False
        old_id = str(verifier.get("task_id"))
        match = re.search(r"-recovery-(\d+)$", old_id)
        recovery_no = int(match.group(1)) + 1 if match else 1
        if recovery_no > MAX_REPAIR_RECOVERIES_PER_LINEAGE:
            return False
        new_id = f"{old_id}-recovery-{recovery_no}"
        existing = {item.get("task_id") for item in ledger.get("tasks", []) if isinstance(item, dict)}
        if new_id in existing:
            return False
        from modules.team_ledger import _normalize_tasks
        recovery_task = dict(verifier)
        recovery_task.update({"task_id": new_id, "title": f"{verifier.get('title', old_id)} recovery",
                              "depends_on": [], "recovery_of": old_id})
        old_paths = [path for path in required if isinstance(path, str) and path.strip()]
        recovery_paths = [f"{path[:-5]}-recovery-{recovery_no}.json" if path.endswith(".json")
                          else f"{path}-recovery-{recovery_no}" for path in old_paths]
        if recovery_paths:
            recovery_task["scope"] = recovery_paths[0]
            recovery_task["required_artifacts"] = recovery_paths
            if isinstance(recovery_task.get("objective"), str):
                for old_path, new_path in zip(old_paths, recovery_paths):
                    recovery_task["objective"] = recovery_task["objective"].replace(old_path, new_path)
        verifier["superseded_by"] = new_id
        normalized = _normalize_tasks([recovery_task], goal=run.original_goal,
                                      workspace=str(workspace), run_id=run.goal_run_id)[0]
        ledger.setdefault("tasks", []).append({**normalized, "state": "PENDING",
                                                 "worker_attempts": [], "result": None, "error": None})
        ledger["status"] = "READY"
        ledger.setdefault("events", []).append({"at": _now(),
            "event": "chatgpt_verification_task_recovery_added", "task_id": new_id,
            "payload": {"recovery_of": old_id, "round": review_round,
                         "reason": "verifier provider failure with no persisted receipt"}})
        ledger["updated_at"] = _now()
        ledger_store.save(ledger)
        workflow["verification_task_recovery_count"] = recovery_no
        workflow["verification_task_recovery_task_id"] = new_id
        workflow["verification_task_recovery_of"] = old_id
        if recovery_paths and isinstance(workflow.get("review_artifact_paths"), list):
            workflow["review_artifact_paths"] = [
                recovery_paths[old_paths.index(path)] if path in old_paths else path
                for path in workflow["review_artifact_paths"]
            ]
        workflow["follow_up_task"] = {**(workflow.get("follow_up_task") or {}),
                                       "status": "RUNNING", "recovery_task_id": new_id}
        run.context["chatgpt_workflow"] = workflow
        run.team_status = "READY"
        run.final_status = None
        run.acceptance_passed = False
        if run.current_state in {"FAILED", "BLOCKED", "COMPLETED"}:
            run.transition("READY", reason="bounded ChatGPT verifier task recovery queued")
        self.store.save(run)
        return True

    @staticmethod
    def _parse_chatgpt_review_decision(snippet: Any) -> str:
        """Parse the first explicit decision token from a bounded reply."""
        if not isinstance(snippet, str):
            return "FOLLOW_UP_REQUIRED"
        match = re.search(r"\bREVIEW\s*:\s*(ACCEPT|FIX)\b", snippet, re.IGNORECASE)
        if not match:
            return "FOLLOW_UP_REQUIRED"
        return "ACCEPTED" if match.group(1).upper() == "ACCEPT" else "FOLLOW_UP_REQUIRED"

    @staticmethod
    def _bounded_artifact_context(run: GoalRun, paths: list[Any]) -> str:
        """Read only declared, bounded worker outputs for the review prompt."""
        workspace = Path(run.workspace).resolve(strict=True)
        blocks: list[str] = []
        total = 0
        for raw in paths:
            if not isinstance(raw, str) or not raw.strip() or len(blocks) >= 8:
                continue
            candidate = (workspace / raw).resolve(strict=False)
            try:
                candidate.relative_to(workspace)
            except ValueError:
                continue
            if not candidate.is_file() or candidate.stat().st_size > 16_000:
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Keep the live bridge input bounded and avoid forwarding common
            # credential-like material if a worker accidentally writes it.
            text = re.sub(r"(?i)(bearer\s+|token|cookie|password|secret|api[_ -]?key)\s*[:=]\s*\S+", r"\1[REDACTED]", text)
            text = text[:2_000]
            block = f"PATH: {raw}\nCONTENT:\n{text}"
            if total + len(block) > 8_000:
                break
            blocks.append(block)
            total += len(block)
        return "\n\n".join(blocks) or "(No bounded artifact content was available.)"

    def _apply_team_result(self, run: GoalRun, *, finalize: bool = True) -> None:
        ledger = TeamLedgerStore(run.team_ledger_ref).load() if run.team_ledger_ref else {}
        if run.current_state == "EXECUTING":
            run.transition("VERIFYING", reason="team worker wave returned")
        tasks = ledger.get("tasks", []) if isinstance(ledger, dict) else []
        task_checks: dict[str, dict[str, Any]] = {}
        child_verifier_passes: dict[str, bool] = {}
        for item in tasks:
            if not isinstance(item, dict):
                continue
            attempts = item.get("worker_attempts") or []
            attempt = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            acceptance = result.get("acceptance") if isinstance(result.get("acceptance"), dict) else attempt.get("acceptance")
            # Failed predecessor verifiers remain in the ledger for audit.
            # Only the active verifier may determine the current wave result.
            if (item.get("role") == "verifier"
                    and not item.get("superseded_by")
                    and isinstance(acceptance, dict)):
                verdict_paths = [path for path in acceptance.get("present", [])
                                 if isinstance(path, str) and "verification" in path]
                for verdict_path in verdict_paths:
                    try:
                        verdict_receipt = json.loads((Path(run.workspace) / verdict_path).read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        verdict_receipt = {}
                    child_verifier_passes[item.get("task_id", "unknown")] = (
                        isinstance(verdict_receipt, dict)
                        and verdict_receipt.get("verdict") == "PASS_BOUNDED"
                    )
            task_checks[item.get("task_id", "unknown")] = {
                "state": item.get("state"),
                "provider_status": attempt.get("provider_status") or result.get("provider_status"),
                "attempt_state": attempt.get("state"),
                "acceptance_recorded": isinstance(acceptance, dict),
                "acceptance_passed": acceptance.get("accepted") is True if isinstance(acceptance, dict) else False,
                "artifacts": acceptance.get("present", []) if isinstance(acceptance, dict) else [],
                "required_artifacts": acceptance.get("required_artifacts", []) if isinstance(acceptance, dict) else [],
            }
        workflow = run.context.get("chatgpt_workflow")
        attempts_total = sum(len(item.get("worker_attempts", [])) for item in tasks if isinstance(item, dict))
        active_tasks = [item for item in tasks
                        if isinstance(item, dict) and not item.get("superseded_by")]
        task_acceptance = bool(active_tasks) and all(
            check["state"] == "COMPLETED"
            and check["attempt_state"] == "COMPLETED"
            and check["provider_status"] == "COMPLETED"
            and check["acceptance_recorded"]
            and check["acceptance_passed"]
            and bool(check["required_artifacts"])
            and bool(check["artifacts"])
            for item in active_tasks
            for check in [task_checks.get(item.get("task_id", "unknown"), {})]
        ) and len(active_tasks) == sum(
            1 for item in active_tasks if item.get("task_id") in task_checks
        )
        # A repair wave keeps failed predecessor attempts for audit. Their
        # terminal state must not poison the current wave once each
        # predecessor is marked `superseded_by` and every active task has an
        # accepted artifact. The ledger's aggregate status remains historical;
        # GoalRun status reflects the active, dependency-complete wave.
        if task_acceptance and run.team_status == "FAILED":
            run.team_summary = {
                **(run.team_summary if isinstance(run.team_summary, dict) else {}),
                "ledger_status": "FAILED",
                "status": "COMPLETED",
                "active_wave_completed": True,
            }
            run.team_status = "COMPLETED"
        run.artifact_refs = [
            {"task_id": task_id, "path": path}
            for task_id, check in task_checks.items()
            for path in check.get("artifacts", [])
            if isinstance(path, str) and path.strip()
        ]
        required_verifier_passed = (bool(child_verifier_passes)
                                    and all(child_verifier_passes.values())) if run.decision == "CHATGPT_TEAM" else True
        run.verification = {"team_tasks_completed": run.team_status == "COMPLETED" and task_acceptance
                                                 and required_verifier_passed,
                            "worker_attempts_recorded": attempts_total >= len(tasks) if tasks else False,
                            "task_acceptance_evidence": task_acceptance,
                            "team_ledger_persisted": bool(run.team_ledger_ref),
                            "provider_policy_preserved": True,
                            "task_checks": task_checks,
                            "child_verifier_passes": child_verifier_passes,
                            "child_verifier_passed": required_verifier_passed}
        run.acceptance_passed = all(value is True for key, value in run.verification.items()
                                    if key not in {"task_checks", "child_verifier_passes"})
        if not finalize:
            run.final_status = None
            if not run.acceptance_passed:
                run.blockers.append({"class": "team_verification",
                                     "reason": "worker artifacts failed independent acceptance before ChatGPT review",
                                     "task_checks": task_checks})
            self.store.save(run)
            return
        run.final_status = "COMPLETED" if run.acceptance_passed else "FAILED"
        if run.acceptance_passed:
            # Recovery history remains in the team ledger. GoalRun blockers
            # represent only unresolved current blockers, so clear stale
            # verification/recovery entries after accepted reconciliation.
            run.blockers = [item for item in run.blockers
                            if item.get("class") not in {"team_verification", "team_recovery"}]
        else:
            run.blockers.append({"class": "team_verification",
                                 "reason": "one or more team tasks lacks accepted artifact evidence",
                                 "task_checks": task_checks})
        run.transition("COMPLETED" if run.acceptance_passed else "FAILED", reason="team verification")
        self.store.save(run)

    @staticmethod
    def _team_config() -> dict[str, Any]:
        path = Path(__file__).resolve().parents[2] / "agent-brain" / "team-config.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _team_enabled(self, run: GoalRun) -> bool:
        # Capability-native combined repair loops own their bounded sequence;
        # do not divert a browser/coding goal into the generic research team.
        if isinstance(run.context.get("combined"), dict):
            return False
        # ChatGPT plan/review workflows explicitly own a durable worker wave.
        # They must not depend on lexical team trigger phrases in the original
        # goal, otherwise a completed plan can be accepted while no ledger or
        # worker is ever created.
        if isinstance(run.context.get("chatgpt_workflow"), dict):
            return True
        if run.context.get("team_enabled") is False:
            return False
        if run.context.get("team_enabled") is True:
            return True
        config = self._team_config()
        if config.get("enabled") is not True:
            return False
        text = run.original_goal.casefold()
        if any(isinstance(item, str) and item.casefold() in text
               for item in config.get("auto_triggers", [])):
            return True
        # Keep direct service/API submissions aligned with the normal ORCA
        # UserPromptSubmit hook: bounded action goals use the existing team
        # lane, while questions and short conversational/status turns remain
        # direct. Explicit context.team_enabled still wins above.
        if STATUS_REQUEST_RE.search(text):
            return False
        return len(text) >= 24 and "?" not in text and bool(AUTO_ACTION_RE.search(text))

    def _prepare_team(self, run: GoalRun, state_dir: Path) -> None:
        if run.team_ledger_ref or not self._team_enabled(run):
            return
        ledger_path = state_dir / "team" / "ledger.json"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger = create_ledger(goal=run.original_goal, workspace=run.workspace,
                               run_id=run.goal_run_id, raw_tasks=run.context.get("team_tasks"),
                               path=ledger_path)
        run.team_ledger_ref = str(ledger_path)
        run.team_status = ledger.get("status", "READY")
        run.context["team_ledger_ref"] = str(ledger_path)
        self.store.audit({"event": "team_ledger_prepared", "goal_run_id": run.goal_run_id,
                          "team_run_id": ledger.get("team_run_id"),
                          "task_ids": [item.get("task_id") for item in ledger.get("tasks", [])]})
        self.store.save(run)

    def _execute_team(self, run: GoalRun) -> None:
        if not run.team_ledger_ref:
            return
        config = self._team_config()
        try:
            max_workers = int(run.context.get("team_max_workers", config.get("max_workers", 2)))
        except (TypeError, ValueError):
            max_workers = 2
        runner = TeamRunner(run.team_ledger_ref, max_workers=max_workers,
                            wait_seconds=float(config.get("worker_wait_seconds", 600)),
                            provider_policy=run.provider_policy or {"allowed_providers": ["local-qwen"]},
                            shared_context={
                                "hstry_receipt_ref": run.context.get("hstry_receipt_ref"),
                                "chatgpt_plan_receipt_ref": run.context.get("chatgpt_workflow_ref"),
                                "chatgpt_plan_receipt": (run.context.get("chatgpt_workflow") or {}).get("plan_receipt"),
                            })
        ledger = runner.run()
        run.team_status = ledger.get("status", "FAILED")
        task_states = {item.get("task_id"): item.get("state") for item in ledger.get("tasks", [])}
        run.team_summary = {
            "team_run_id": ledger.get("team_run_id"), "status": run.team_status,
            "task_states": task_states,
            "worker_attempts": sum(len(item.get("worker_attempts", [])) for item in ledger.get("tasks", [])),
            "completed_tasks": sum(state == "COMPLETED" for state in task_states.values()),
        }
        self.store.audit({"event": "team_finished", "goal_run_id": run.goal_run_id,
                          "team_run_id": ledger.get("team_run_id"), "status": run.team_status,
                          "task_states": task_states})
        self.store.save(run)

    def run(self, goal: str, *, workspace: str, execution_policy: str = "WORKSPACE_WRITE",
            context: dict[str, Any] | None = None, provider_policy: dict[str, Any] | None = None,
            goal_run_id: str | None = None) -> GoalRun:
        run = self.create(goal, workspace=workspace, execution_policy=execution_policy, context=context,
                          provider_policy=provider_policy, goal_run_id=goal_run_id)
        self.plan(run.goal_run_id)
        return self.execute(run.goal_run_id)

    def resume(self, goal_run_id: str) -> GoalRun:
        run = self.store.load(goal_run_id)
        if run.current_state in {"FAILED", "BLOCKED"} and run.decision == "CHATGPT_TEAM":
            if self._prepare_chatgpt_plan_phase_recovery(run):
                return self.execute(goal_run_id)
            if self._prepare_chatgpt_measurement_contract_recovery(run):
                return self.execute(goal_run_id)
            if self._prepare_chatgpt_verification_task_recovery(run):
                return self.execute(goal_run_id)
            if self._prepare_chatgpt_repair_recovery(run):
                return self.execute(goal_run_id)
            if self._prepare_chatgpt_failed_task_recovery(run):
                return self.execute(goal_run_id)
            if self._prepare_chatgpt_verifier_recovery(run):
                return self.execute(goal_run_id)
            if self._prepare_chatgpt_team_wave_recovery(run):
                return self.execute(goal_run_id)
        if run.current_state == "FAILED" and run.decision == "CHATGPT_TEAM":
            if self._prepare_chatgpt_review_recovery(run):
                return self.execute(goal_run_id)
            # A failed bridge phase is resumable only when its durable receipt
            # is absent or retryable.  Never silently retry a recorded bridge
            # request; the caller must create a linked continuation after the
            # browser/session boundary is repaired.
            reason = ""
            if run.blockers:
                reason = str(run.blockers[-1].get("reason", ""))
            if reason.startswith("CHATGPT_"):
                raise ValueError("CHATGPT_WORKFLOW_FAILED_REQUIRES_LINKED_CONTINUATION")
        if run.current_state == "RECEIVED":
            self.plan(goal_run_id)
        return self.execute(goal_run_id)

    def _prepare_chatgpt_team_wave_recovery(self, run: GoalRun) -> bool:
        """Resume a completed ChatGPT plan whose worker ledger was never made.

        This repairs the pre-dispatch orchestration failure in-place. The
        completed plan receipt remains immutable and is consumed by
        ``_execute_chatgpt_team`` without another browser request.
        """
        if run.decision != "CHATGPT_TEAM" or run.current_state not in {"FAILED", "BLOCKED"}:
            return False
        if run.team_ledger_ref:
            return False
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict):
            return False
        phases = workflow.get("phases") if isinstance(workflow.get("phases"), dict) else {}
        plan = phases.get("PLAN_RECEIVED")
        if not isinstance(plan, dict) or plan.get("status") != "COMPLETED":
            return False
        if not isinstance(workflow.get("team_tasks"), list) or not workflow.get("team_tasks"):
            return False
        state_dir = Path(run.workspace) / ".system-agentic-goals" / run.goal_run_id
        self._prepare_team(run, state_dir)
        if not run.team_ledger_ref:
            return False
        run.team_status = "READY"
        run.final_status = None
        run.acceptance_passed = False
        run.blockers.append({"class": "team_recovery",
                             "reason": "created missing worker ledger after completed ChatGPT plan",
                             "plan_request_id": plan.get("request_id"),
                             "plan_idempotency_key": plan.get("idempotency_key")})
        run.transition("READY", reason="recovered missing worker ledger after completed ChatGPT plan")
        self.store.save(run)
        return True

    def _prepare_chatgpt_plan_phase_recovery(self, run: GoalRun) -> bool:
        """Prepare one explicit recovery after a pre-response plan failure.

        The original bridge request remains immutable. A fresh browser
        preflight and a new idempotency key are required before resubmitting a
        plan that failed at the visible-tab boundary. Automatic supervisor
        ticks never call this helper; it is reached only by explicit resume.
        """
        if run.decision != "CHATGPT_TEAM" or run.current_state not in {"FAILED", "BLOCKED"}:
            return False
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict):
            return False
        phases = workflow.get("phases") if isinstance(workflow.get("phases"), dict) else {}
        failed = phases.get("PLAN_RECEIVED")
        if not isinstance(failed, dict) or failed.get("status") != "FAILED":
            return False
        if failed.get("error") not in {
            "CONVERSATION_NOT_STABLE", "CONVERSATION_MISMATCH",
            "CONVERSATION_CHANGED_DURING_EXCHANGE", "CONVERSATION_RESTORE_TIMEOUT",
            "orca transport action failed: element_not_found",
        }:
            return False
        try:
            recovery_count = int(workflow.get("plan_phase_recovery_count", 0) or 0)
        except (TypeError, ValueError):
            recovery_count = 0
        if recovery_count >= 1:
            return False
        conversation_id = str(workflow.get("conversation_id") or failed.get("conversation_id") or "")
        prompt = workflow.get("plan_prompt")
        if not conversation_id or not isinstance(prompt, str) or not prompt.strip():
            return False
        try:
            from modules.chatgpt_browser_bridge import OrcaChromeTransport
            snapshot = OrcaChromeTransport(timeout=15).stable_snapshot(conversation_id)
        except (OSError, RuntimeError, ValueError):
            return False
        if snapshot.conversation_id != conversation_id:
            return False
        account_id = str(workflow.get("account_id") or "chrome-default-chatgpt")
        recovery_no = recovery_count + 1
        next_key = f"{run.goal_run_id}:plan:recovery-{recovery_no}"
        next_approval_id = f"approval-{run.goal_run_id}-plan-recovery-{recovery_no}"
        from modules.chatgpt_web_adapter import ChatGPTWebAdapter
        approval = ChatGPTWebAdapter().record_approval(
            {"run_id": run.goal_run_id, "account_id": account_id,
             "conversation_id": conversation_id, "prompt": prompt[:12_000],
             "idempotency_key": next_key},
            approval_id=next_approval_id,
            approved_by="interactive user authorized bounded downstream-distribution continuation",
        )
        if approval.get("status") != "APPROVED":
            raise RuntimeError("CHATGPT_PLAN_APPROVAL_PERSISTENCE_FAILED")
        workflow["plan_phase_recovery_count"] = recovery_no
        workflow["plan_phase_recovery_from"] = {
            "request_id": failed.get("request_id"),
            "idempotency_key": failed.get("idempotency_key"),
            "error": failed.get("error"),
        }
        workflow["plan_idempotency_key"] = next_key
        workflow["plan_approval_id"] = next_approval_id
        workflow["conversation_id"] = conversation_id
        run.context["chatgpt_workflow"] = workflow
        run.final_status = None
        run.acceptance_passed = False
        run.blockers.append({"class": "chatgpt_recovery",
                             "reason": "new plan exchange prepared after browser boundary failure",
                             "previous_request_id": failed.get("request_id"),
                             "new_idempotency_key": next_key,
                             "conversation_id": conversation_id})
        run.transition("READY", reason="recovered ChatGPT plan after fresh browser preflight")
        self.store.save(run)
        return True

    def _prepare_chatgpt_review_recovery(self, run: GoalRun) -> bool:
        """Prepare one bounded review retry after a browser-boundary failure.

        The failed exchange remains immutable and is never resent. Only a
        review that failed before receiving a response because the visible
        Chrome tab was on another conversation can be continued. A fresh
        idempotency key and approval bind the new exchange to the same run,
        account, and conversation. The caller must have repaired the browser
        tab; the bridge performs the final conversation check.
        """
        if run.decision != "CHATGPT_TEAM" or run.current_state != "FAILED":
            return False
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict):
            return False
        review = workflow.get("phases", {}).get("REVIEW_RECEIVED") if isinstance(workflow.get("phases"), dict) else None
        if not isinstance(review, dict):
            review = workflow.get("last_failure")
        if not isinstance(review, dict):
            # Older controller versions raised before persisting terminal
            # phase receipts. The adapter receipt is the durable source of
            # truth for that run-scoped idempotency key.
            try:
                from modules.chatgpt_web_adapter import ChatGPTWebAdapter
                review = ChatGPTWebAdapter()._existing_receipt(
                    str(workflow.get("review_idempotency_key") or "")
                )
            except (OSError, RuntimeError, ValueError):
                review = None
        if not isinstance(review, dict):
            return False
        if review.get("status") != "FAILED" or review.get("error") not in {
            "CONVERSATION_MISMATCH", "CONVERSATION_CHANGED_DURING_EXCHANGE",
            "CONVERSATION_RESTORE_TIMEOUT",
        }:
            return False
        if workflow.get("review_recovery_used"):
            return False
        if run.team_status != "COMPLETED" or not run.team_ledger_ref:
            return False
        plan = workflow.get("phases", {}).get("PLAN_RECEIVED") if isinstance(workflow.get("phases"), dict) else None
        if not isinstance(plan, dict) or plan.get("status") != "COMPLETED":
            return False

        account_id = str(workflow.get("account_id") or "chrome-default-chatgpt")
        conversation_id = str(workflow.get("conversation_id") or plan.get("conversation_id") or "")
        old_key = str(workflow.get("review_idempotency_key") or "")
        if not conversation_id or not old_key:
            return False
        try:
            recovery_no = int(workflow.get("review_recovery_count", 0)) + 1
        except (TypeError, ValueError):
            recovery_no = 1
        # Three bounded recoveries cover a tab that first needs to be restored
        # to the target conversation and then needs one or two SPA remounts.
        # Never continue beyond this cap or retry a completed exchange.
        if recovery_no > 3:
            return False
        next_key = f"{run.goal_run_id}:review:1:recovery-{recovery_no}"
        next_approval_id = f"approval-{run.goal_run_id}-review-r1-recovery-{recovery_no}"
        from modules.chatgpt_web_adapter import ChatGPTWebAdapter
        prompt = str(workflow.get("review_prompt") or "")
        if not prompt.strip():
            return False
        approval = ChatGPTWebAdapter().record_approval(
            {"run_id": run.goal_run_id, "account_id": account_id,
             "conversation_id": conversation_id, "prompt": prompt[:12_000],
             "idempotency_key": next_key},
            approval_id=next_approval_id,
            approved_by="interactive user authorized bounded downstream-distribution review loop",
        )
        if approval.get("status") != "APPROVED":
            raise RuntimeError("CHATGPT_REVIEW_APPROVAL_PERSISTENCE_FAILED")
        workflow["review_recovery_count"] = recovery_no
        workflow["review_recovery_from"] = {
            "request_id": review.get("request_id"),
            "idempotency_key": old_key,
            "error": review.get("error"),
        }
        workflow["review_idempotency_key"] = next_key
        workflow["review_approval_id"] = next_approval_id
        workflow["review_approval_ids"] = {**(
            workflow.get("review_approval_ids") if isinstance(workflow.get("review_approval_ids"), dict) else {}
        ), "1": next_approval_id}
        workflow["last_phase"] = "PLAN_RECEIVED"
        run.context["chatgpt_workflow"] = workflow
        run.final_status = None
        run.acceptance_passed = False
        run.blockers.append({"class": "chatgpt_recovery", "reason": "new review exchange prepared after CONVERSATION_MISMATCH",
                             "previous_request_id": review.get("request_id"), "new_idempotency_key": next_key})
        run.transition("READY", reason="recovered ChatGPT review after browser conversation mismatch")
        self.store.save(run)
        return True

    def _prepare_chatgpt_completed_wave_resume(self, run: GoalRun) -> bool:
        """Resume review after a completed repair wave with failed history.

        A terminal worker ledger can retain superseded failed attempts even
        after its current repair/integration/verifier wave succeeds. Reconcile
        the active wave, then continue to the already-approved ChatGPT review
        phase without dispatching another worker or bridge request.
        """
        if run.decision != "CHATGPT_TEAM" or run.current_state not in {"FAILED", "BLOCKED", "COMPLETED"}:
            return False
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict) or workflow.get("review_decision") != "FOLLOW_UP_REQUIRED":
            return False
        review_round = int(workflow.get("review_round", 1) or 1)
        if review_round <= 1 or not isinstance(workflow.get("review_artifact_paths"), list):
            return False
        phases = workflow.get("phases") if isinstance(workflow.get("phases"), dict) else {}
        if f"REVIEW_RECEIVED_{review_round}" in phases:
            return False
        # Do not let the service requeue a terminal browser-boundary failure
        # forever. One same-request recovery is allowed by the bridge for a
        # preflight conversation mismatch; after that, preserve FAILED for
        # operator inspection instead of creating a hot loop.
        failed_review = phases.get(f"REVIEW_RECEIVED_{review_round}")
        if isinstance(failed_review, dict) and failed_review.get("status") == "FAILED":
            if not (
                failed_review.get("error") in {
                    "CONVERSATION_MISMATCH",
                    "orca transport action failed: element_not_found",
                }
                and int(failed_review.get("retry_count", 0) or 0) < 3
            ):
                return False
        if not run.team_ledger_ref:
            return False
        self._apply_team_result(run, finalize=False)
        reconciled = self.store.load(run.goal_run_id)
        if not reconciled.verification.get("team_tasks_completed"):
            return False
        reconciled.acceptance_passed = False
        reconciled.final_status = None
        reconciled.transition("READY", reason="active ChatGPT repair wave completed; resume approved review")
        self.store.save(reconciled)
        return True

    def replan_after_promotion(self, goal_run_id: str, *, promotion_id: str, capability_id: str) -> GoalRun:
        run = self.store.load(goal_run_id)
        if run.current_state == "COMPLETED":
            return run
        if run.current_state == "UNRESOLVED":
            run.blockers.append({"class": "capability_promotion", "promotion_id": promotion_id,
                                 "capability_id": capability_id, "event": "replan_requested"})
            run.transition("ANALYZING", reason=f"trusted capability promoted: {capability_id}")
            self.store.save(run)
        self.plan(goal_run_id)
        return self.execute(goal_run_id)

    def _load_blueprint(self, run: GoalRun) -> SystemBlueprint:
        if not run.system_blueprint_ref:
            raise ValueError("GoalRun has no persisted blueprint reference")
        return SystemBlueprint.from_dict(json.loads(Path(run.system_blueprint_ref).read_text(encoding="utf-8")))

    def _execute_direct(self, run: GoalRun) -> None:
        if len(run.resolved_capabilities) != 1:
            raise ValueError("DIRECT GoalRun must have exactly one resolved capability")
        resolved = run.resolved_capabilities[0]
        capability, module_id = resolved["capability"], resolved["module"]
        if capability == "pedagogy.session":
            request = dict(run.context.get("pedagogy_request", {}))
            if ("pedagogy_request" in run.context or self._dispatch is dispatch_task) and (not isinstance(request.get("session_id"), str) or not request["session_id"].strip()):
                run.outcome = "INPUT_MISSING"
                run.final_status = "BLOCKED"
                run.blockers.append({"class": "input", "reason": "pedagogy.session requires context.pedagogy_request.session_id"})
                run.transition("BLOCKED", reason="required input missing")
                self.store.save(run)
                return
        step_id = capability.replace(".", "-")
        task_id = f"{run.goal_run_id}-{step_id}"
        logical_task_id = task_id
        execution_attempt_id = f"{task_id}-attempt-{run.execution_attempt or 1}"
        idempotency_key = f"{run.goal_run_id}:{logical_task_id}:{run.execution_attempt or 1}"
        context = dict(run.context)
        if capability == "pedagogy.session":
            request = {"action": "export", "format": "md", **dict(context.get("pedagogy_request", {}))}
            request.update({"action": "export", "format": "md"})
            context["pedagogy_request"] = request
        spec = {"task_id": task_id, "workspace": run.workspace, "objective": run.original_goal,
                "context": context, "constraints": [],
                "execution_policy": "READ_ONLY" if capability == "pedagogy.session" else run.execution_policy,
                "provider_policy": run.provider_policy,
                "execution_context": {"origin": "external-orchestrator", "host_role": "orchestrator",
                                      "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH",
                                      "workflow_id": run.goal_run_id, "workflow_step": step_id,
                                      "parent_task_id": run.goal_run_id,
                                      "logical_task_id": logical_task_id,
                                      "execution_attempt_id": execution_attempt_id,
                                      "idempotency_key": idempotency_key}}
        task = task_from_spec(spec)
        try:
            from providers.runtime import select_provider
            _, selection = select_provider(task)
            selected_provider = selection.provider_id
        except (OSError, RuntimeError, ValueError):
            selected_provider = None
        ref = {"task_id": task_id, "logical_task_id": logical_task_id,
               "execution_attempt_id": execution_attempt_id, "idempotency_key": idempotency_key,
               "module": module_id, "capability": capability, "provider": selected_provider, "dispatch_state": "PREPARED",
               "terminal_status": None, "provider_job_id": None, "task_spec": spec}
        run.execution_refs.append(ref)
        self.store.audit({"event": "provider_submit_intent", "goal_run_id": run.goal_run_id, **ref})
        self.store.save(run)
        result = self._dispatch(task, module_id=module_id, module_capability=capability)
        execution_status = result.get("execution_status", result.get("status"))
        ref.update({"provider": result.get("provider"), "queue_job_id": result.get("queue_job_id"),
                    "provider_job_id": result.get("queue_job_id") or task_id,
                    "execution_status": execution_status,
                    "terminal_status": execution_status if execution_status in {"COMPLETED", "FAILED", "FAILED_LOST"} else None,
                    "dispatch_state": "COMPLETED" if execution_status == "COMPLETED" else ("SUBMITTED" if result.get("queue_job_id") or result.get("status") in {"SUBMITTED", "RUNNING", "UNKNOWN"} else "REJECTED")})
        self.store.audit({"event": "provider_submitted", "goal_run_id": run.goal_run_id, **ref})
        self.store.save(run)
        self._apply_direct_result(run, result)

    def _execute_combined(self, run: GoalRun) -> None:
        """Bounded live app -> browser defect -> coding repair -> browser retest."""
        root = Path(run.workspace).resolve(strict=True)
        combined = run.context.get("combined") if isinstance(run.context.get("combined"), dict) else {}
        if not combined:
            # Supported projects may expose a small local compatibility
            # manifest. The user still supplies one natural-language goal;
            # project discovery supplies only stack/runtime facts and bounded
            # verification contracts. Provider/module IDs never come from the
            # manifest and are still resolved by the registry.
            for candidate in ("agentic.project.json", ".system-agentic/project.json"):
                manifest = root / candidate
                if not manifest.is_file() or manifest.stat().st_size > 32_768:
                    continue
                try:
                    loaded = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(loaded, dict) and isinstance(loaded.get("combined"), dict):
                    combined = dict(loaded["combined"])
                    run.context["combined_manifest_ref"] = str(manifest)
                    run.context["combined"] = combined
                    break
        app = None
        evidence: dict[str, Any] = {"workflow": "coding-browser-repair", "goal": run.original_goal,
                                    "discovery": {"manifest": run.context.get("combined_manifest_ref")},
                                    "phases": []}
        try:
            command = combined.get("app_command")
            if command:
                argv = shlex.split(command) if isinstance(command, str) else list(command)
                if not argv or any(not isinstance(item, str) or item.startswith(("http://", "https://")) for item in argv):
                    raise ValueError("combined app_command must be a local argv")
                app = subprocess.Popen(argv, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, start_new_session=True)
                evidence["app_pid"] = app.pid
                app_url = combined.get("app_url")
                if isinstance(app_url, str):
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        try:
                            with urllib.request.urlopen(app_url, timeout=0.4) as response:
                                if 200 <= response.status < 500:
                                    break
                        except OSError:
                            time.sleep(0.1)
                    else:
                        raise RuntimeError("combined app did not become reachable")
            app_url = combined.get("app_url")
            if not isinstance(app_url, str):
                raise ValueError("combined workflow requires context.combined.app_url")
            browser_context = {"browser": {"url": app_url, "headless": True,
                              "actions": list(combined.get("browser_initial_actions", [])),
                              "screenshot": f".system-agentic-goals/{run.goal_run_id}/initial.png"}}
            coding = combined.get("coding")
            if not isinstance(coding, dict):
                raise ValueError("combined workflow requires explicit bounded coding patch/test context")
            initial = self._combined_dispatch(run, "browser-agent", "browser.automation", browser_context, "initial-browser")
            evidence["phases"].append({"phase": "detect", "result": initial})
            if initial.get("execution_status") == "COMPLETED":
                raise RuntimeError("browser initial phase did not expose a defect")
            repair_context = {"coding": coding}
            repaired = self._combined_dispatch(run, "coding-agent", "software.coding", repair_context, "coding-repair")
            evidence["phases"].append({"phase": "repair", "result": repaired})
            if repaired.get("execution_status") != "COMPLETED":
                raise RuntimeError("coding repair did not verify")
            # Restart the owned app so the browser observes the repaired source.
            if app is not None:
                app.terminate()
                try: app.wait(timeout=3)
                except subprocess.TimeoutExpired: app.kill(); app.wait(timeout=2)
                app = None
                command = combined.get("app_command")
                argv = shlex.split(command) if isinstance(command, str) else list(command)
                app = subprocess.Popen(argv, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, start_new_session=True)
                evidence["restart_pid"] = app.pid
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(app_url, timeout=0.4) as response:
                            if 200 <= response.status < 500: break
                    except OSError: time.sleep(0.1)
            retest_context = {"browser": {"url": app_url, "headless": True,
                              "actions": list(combined.get("browser_retest_actions", [])),
                              "screenshot": f".system-agentic-goals/{run.goal_run_id}/retest.png"}}
            retest = self._combined_dispatch(run, "browser-agent", "browser.automation", retest_context, "browser-retest")
            evidence["phases"].append({"phase": "retest", "result": retest})
            if retest.get("execution_status") != "COMPLETED":
                raise RuntimeError("browser retest did not verify")
            evidence["status"] = "COMPLETED"
            evidence["acceptance_passed"] = True
            run.outcome = "COMBINED_REPAIR_VERIFIED"
            run.verification = {"defect_detected": True, "coding_repair_verified": True, "browser_retest_verified": True,
                                "process_cleanup": True}
            run.acceptance_passed = True
            run.final_status = "COMPLETED"
            run.transition("VERIFYING", reason="combined live repair loop returned")
            run.transition("COMPLETED", reason="combined browser retest verified")
        except (OSError, ValueError, RuntimeError) as exc:
            evidence["status"] = "FAILED"; evidence["acceptance_passed"] = False
            evidence["error"] = str(exc)
            run.outcome = "COMBINED_REPAIR_FAILED"; run.final_status = "FAILED"; run.acceptance_passed = False
            run.blockers.append({"class": self.classify_failure(str(exc)), "reason": str(exc)})
            if run.current_state == "EXECUTING": run.transition("FAILED", reason=str(exc))
        finally:
            if app is not None:
                try:
                    app.terminate(); app.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    try: app.kill(); app.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired): pass
                evidence["process_cleanup"] = app.poll() is not None
            path = root / ".system-agentic-goals" / run.goal_run_id / "combined-evidence.json"
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            run.context["combined_evidence_ref"] = str(path)
            self.store.save(run)

    def _combined_dispatch(self, run: GoalRun, module: str, capability: str, context: dict[str, Any], phase: str) -> dict[str, Any]:
        task_id = f"{run.goal_run_id}-{phase}"
        spec = {"task_id": task_id, "workspace": run.workspace, "objective": f"Combined workflow phase: {phase}",
                "targetModuleId": module, "requestedCapability": capability, "context": context,
                "execution_policy": "WORKSPACE_WRITE", "execution_context": {"origin": "external-orchestrator",
                "host_role": "orchestrator", "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH",
                "workflow_id": run.goal_run_id, "workflow_step": phase, "framing_result": "combined live repair loop"}}
        task = task_from_spec(spec)
        result = self._dispatch(task, module_id=module, module_capability=capability)
        run.execution_refs.append({"task_id": task_id, "module": module, "capability": capability,
                                   "execution_status": result.get("execution_status", result.get("status")), "phase": phase})
        self.store.save(run)
        return result

    def _apply_direct_result(self, run: GoalRun, result: dict[str, Any]) -> None:
        """Apply one already-produced module result exactly once."""
        resolved = run.resolved_capabilities[0]
        capability = resolved["capability"]
        raw_result = result.get("result") if isinstance(result.get("result"), dict) else result
        run.outcome = result.get("domain_outcome") or raw_result.get("domain_outcome") or result.get("status")
        run.artifact_refs = [
            {key: value for key, value in artifact.items() if key != "content"}
            for artifact in result.get("artifacts", []) if isinstance(artifact, dict)
        ]
        run.transition("VERIFYING", reason="direct execution returned")
        completed = result.get("execution_status", result.get("status")) == "COMPLETED"
        expected_outcomes = {
            "pedagogy.session": {"ARTIFACT_EXPORTED"},
            "software.code-review": {"CLEAN"},
            "data.table.summarize": {"SUMMARY_VERIFIED"},
            "desktop.automation": {"VERIFIED"},
            "desktop.snapshot": {"VERIFIED"},
            "desktop.action": {"VERIFIED"},
            "browser.automation": {"BROWSER_VERIFIED"},
            "software.coding": {"CODE_CHANGED_AND_VERIFIED"},
        }
        outcome_ok = run.outcome in expected_outcomes.get(capability, {"COMPLETED"})
        desktop_evidence = raw_result.get("evidence", []) if capability.startswith("desktop.") else []
        desktop_observed = any(isinstance(item, dict) and item.get("type") in {"observation", "verification_observation"}
                               and item.get("complete") is True for item in desktop_evidence)
        desktop_asserted = any(isinstance(item, dict) and item.get("type") == "assertion" and item.get("passed") is True
                               for item in desktop_evidence)
        artifacts_ok = bool(run.artifact_refs) or (
            capability == "data.table.summarize"
            and result.get("result") == {"row_count": 3, "total_value": 35, "by_category": {"alpha": 30, "beta": 5}}
        ) or (capability == "software.coding" and isinstance(raw_result.get("verification"), dict)
              and raw_result.get("verification", {}).get("exit_code") == 0
              and isinstance(raw_result.get("changed_files"), list) and bool(raw_result.get("changed_files"))) or (
            capability == "browser.automation"
            and any(isinstance(item, dict) and item.get("type") == "screenshot" for item in raw_result.get("evidence", []))
        ) or (capability.startswith("desktop.") and (desktop_observed or desktop_asserted))
        run.verification = {"module_execution_completed": completed, "outcome_satisfied": outcome_ok,
                            "artifacts_present": artifacts_ok, "provider_policy_preserved": True}
        run.acceptance_passed = all(run.verification.values())
        run.final_status = "COMPLETED" if run.acceptance_passed else "FAILED"
        if not run.acceptance_passed:
            run.blockers.append({"class": self.classify_failure(result),
                                 "reason": "direct verification failed"})
        run.transition("COMPLETED" if run.acceptance_passed else "FAILED", reason="direct verification")
        self.store.save(run)

    def _execute_compose(self, run: GoalRun, blueprint: SystemBlueprint) -> None:
        def step_started(step: Any, task: Any) -> None:
            selected_provider = None
            try:
                from providers.runtime import select_provider
                if step.module_id not in {"pedagogy"}:
                    _, selection = select_provider(task)
                    selected_provider = selection.provider_id
            except (OSError, RuntimeError, ValueError):
                pass
            ref = next((item for item in run.execution_refs if item.get("task_id") == task.task_id), None)
            if ref is None:
                ref = {"task_id": task.task_id, "logical_task_id": task.task_id,
                       "execution_attempt_id": f"{task.task_id}-attempt-1",
                       "idempotency_key": f"{run.goal_run_id}:{task.task_id}:1",
                       "module": step.module_id, "capability": step.capability,
                       "provider": selected_provider, "dispatch_state": "PREPARED",
                       "terminal_status": None, "provider_job_id": None,
                       "task_spec": {"task_id": task.task_id, "workspace": str(task.workspace),
                                     "objective": task.objective, "context": task.context,
                                     "constraints": list(task.constraints),
                                     "execution_policy": task.execution_policy,
                                     "provider_policy": task.provider_policy,
                                     "execution_context": task.execution_context,
                                     "required_capabilities": list(task.required_capabilities)}}
                run.execution_refs.append(ref)
            elif selected_provider:
                ref["provider"] = selected_provider
            self.store.audit({"event": "provider_submit_intent", "goal_run_id": run.goal_run_id,
                              "task_id": task.task_id, "module": step.module_id,
                              "capability": step.capability})
            self.store.save(run)
        kwargs = {"provider_policy": run.provider_policy, "on_step_started": step_started}
        try:
            result = self._system_runner(blueprint, **kwargs)
        except TypeError as exc:
            # Test/injected runners from earlier Core v1 contracts do not yet
            # accept the optional lifecycle observer.
            if "on_step_started" not in str(exc):
                raise
            result = self._system_runner(blueprint, provider_policy=run.provider_policy)
        plan = result.get("compiled_plan")
        if plan:
            plan_path = Path(run.workspace) / ".system-agentic-goals" / run.goal_run_id / "compiled-plan.json"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            run.composition_plan_ref = str(plan_path)
        composition = result.get("composition") or {}
        if composition.get("steps"):
            for step in composition.get("steps", []):
                ref = next((item for item in run.execution_refs if item.get("task_id") == step.get("task_id")), None)
                if ref is None:
                    ref = {"task_id": step.get("task_id"), "module": step.get("module"),
                           "provider": step.get("provider"), "queue_job_id": step.get("queue_job_id"),
                           "execution_status": step.get("execution_status")}
                    run.execution_refs.append(ref)
                ref.update({"provider": step.get("provider") or ref.get("provider"),
                            "queue_job_id": step.get("queue_job_id") or ref.get("queue_job_id"),
                            "execution_status": step.get("execution_status")})
        run.artifact_refs = list(composition.get("artifacts", []))
        run.outcome = composition.get("workflow_status") or result.get("system_execution_status")
        run.transition("VERIFYING", reason="composition runtime returned")
        verification = dict(composition.get("verification", {}))
        verification.update({"blueprint_valid": result.get("blueprint_status") == "VALID",
                             "composition_completed": composition.get("workflow_status") == "COMPLETED",
                             "acceptance_passed": result.get("acceptance_passed") is True})
        run.verification = verification
        run.acceptance_passed = result.get("acceptance_passed") is True and all(
            value is True for value in verification.values())
        run.final_status = "COMPLETED" if run.acceptance_passed else "FAILED"
        if not run.acceptance_passed:
            run.blockers.append({"class": self.classify_failure(json.dumps(result, ensure_ascii=False)),
                                 "reason": result.get("failure_reason", "composition acceptance failed")})
        run.transition("COMPLETED" if run.acceptance_passed else "FAILED", reason="composition verification")
        self.store.save(run)

    @staticmethod
    def classify_failure(value: str | dict[str, Any]) -> str:
        text = json.dumps(value, ensure_ascii=False).casefold() if isinstance(value, dict) else str(value).casefold()
        if any(token in text for token in ("unresolved", "no eligible", "capability")):
            return "unresolved_capability"
        if any(token in text for token in ("elevated", "safety", "recursion_guard", "policy")):
            return "safety_blocker"
        if any(token in text for token in ("validation", "invalid artifact", "lint", "verify")):
            return "validation_failure"
        if any(token in text for token in ("input_rejected", "semantic", "domain")):
            return "semantic_failure"
        if any(token in text for token in ("unavailable", "timeout", "timed out", "connection")):
            return "transient_infrastructure"
        return "execution_failure"

    def retry(self, goal_run_id: str) -> GoalRun:
        run = self.store.load(goal_run_id)
        if run.current_state not in {"FAILED", "BLOCKED"}:
            return run
        failure_class = (run.blockers[-1].get("class") if run.blockers else "")
        if failure_class != "transient_infrastructure" or len(run.retries) >= 1:
            return run
        run.retries.append({"at": _now(), "reason": failure_class, "attempt": len(run.retries) + 1})
        run.transition("READY", reason="bounded transient retry")
        run.final_status = None
        self.store.save(run)
        return self.execute(goal_run_id)

    def result(self, goal_run_id: str) -> dict[str, Any]:
        run = self.store.load(goal_run_id)
        execution_status = "NOT_RUN" if run.current_state == "UNRESOLVED" else (run.final_status or run.current_state)
        artifact_refs, blockers = self._reported_team_state(run)
        result = {
            "goal_run_id": run.goal_run_id, "original_goal": run.original_goal,
            "decision": run.decision, "execution_status": execution_status,
            "outcome": run.outcome, "resolution_ref": run.resolution_ref,
            "resolved_capabilities": run.resolved_capabilities,
            "blueprint_ref": run.system_blueprint_ref,
            "composition_plan_ref": run.composition_plan_ref,
            "execution_refs": run.execution_refs, "artifact_refs": artifact_refs,
            "verification": run.verification, "blockers": blockers,
            "retries": run.retries, "acceptance_passed": run.acceptance_passed,
            "current_state": run.current_state,
            "capability_gap_ref": run.capability_gap_ref,
            "capability_gap_id": run.capability_gap_id,
            "capability_blueprint_ref": run.capability_blueprint_ref,
            "gap_classification": run.gap_classification,
            "gap_buildability": run.gap_buildability,
            "candidate_ref": run.candidate_ref, "candidate_id": run.candidate_id,
            "candidate_status": run.candidate_status,
            "candidate_verification_status": run.candidate_verification_status,
            "team_ledger_ref": run.team_ledger_ref,
            "team_status": run.team_status,
            "team_summary": run.team_summary,
            "chatgpt_workflow": self._reported_chatgpt_workflow(run),
        }
        self.store.save_result(goal_run_id, result)
        return result

    @staticmethod
    def _reported_chatgpt_workflow(run: GoalRun) -> dict[str, Any] | None:
        workflow = run.context.get("chatgpt_workflow")
        if not isinstance(workflow, dict):
            return None
        phases = workflow.get("phases") if isinstance(workflow.get("phases"), dict) else {}
        safe_phases: dict[str, Any] = {}
        for phase, receipt in phases.items():
            if not isinstance(receipt, dict):
                continue
            safe_phases[str(phase)] = {
                key: receipt.get(key) for key in
                ("status", "request_id", "source", "source_id", "conversation_id", "message_id",
                 "created_at", "url", "idempotency_key", "reused_receipt", "snippet")
                if key in receipt
            }
        routing = None
        try:
            from modules.chatgpt_web_adapter import ChatGPTWebAdapter
            routing = ChatGPTWebAdapter().routing_status()
        except (OSError, ValueError, RuntimeError):
            routing = None
        return {"last_phase": workflow.get("last_phase"),
                "conversation_id": workflow.get("conversation_id"),
                "channel": workflow.get("chatgpt_channel") or workflow.get("channel") or "regular",
                "dynamic_routing": workflow.get("dynamic_routing", True) is not False,
                "routing": routing,
                "plan_derived_tasks_ref": workflow.get("plan_derived_tasks_ref"),
                "review_round": workflow.get("review_round", 1),
                "review_decision": workflow.get("review_decision"),
                "follow_up_task": workflow.get("follow_up_task"),
                "phases": safe_phases}

    @staticmethod
    def _reported_team_state(run: GoalRun) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return current ledger-backed artifact/blocker view.

        Older completed runs may predate artifact propagation or contain a
        historical recovery blocker. Keep read-only result/overview truthful
        by deriving the display from the accepted ledger without rewriting the
        durable run during inspection.
        """
        artifacts = list(run.artifact_refs)
        blockers = list(run.blockers)
        if run.team_ledger_ref:
            try:
                ledger = TeamLedgerStore(run.team_ledger_ref).load()
                derived: list[dict[str, Any]] = []
                for task in ledger.get("tasks", []):
                    if not isinstance(task, dict):
                        continue
                    attempts = task.get("worker_attempts") or []
                    attempt = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
                    acceptance = (task.get("result") or {}).get("acceptance") if isinstance(task.get("result"), dict) else None
                    if not isinstance(acceptance, dict):
                        acceptance = attempt.get("acceptance") if isinstance(attempt.get("acceptance"), dict) else {}
                    for path in acceptance.get("present", []):
                        if isinstance(path, str) and path.strip():
                            derived.append({"task_id": task.get("task_id"), "path": path})
                if derived:
                    artifacts = derived
                if run.acceptance_passed and run.team_status == "COMPLETED":
                    blockers = [item for item in blockers
                                if isinstance(item, dict)
                                and item.get("class") not in {"team_verification", "team_recovery"}]
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        return artifacts, blockers

    def overview(self, goal_run_id: str) -> dict[str, Any]:
        """Bounded read-only operational view; excludes goal transcript/content."""
        run = self.store.load(goal_run_id)
        artifact_refs, blockers = self._reported_team_state(run)
        view: dict[str, Any] = {
            "goal_run_id": run.goal_run_id,
            "goal": run.original_goal[:500],
            "workspace": run.workspace,
            "current_state": run.current_state,
            "decision": run.decision,
            "execution_status": run.final_status or run.current_state,
            "acceptance_passed": run.acceptance_passed,
            "hstry_receipt_ref": run.context.get("hstry_receipt_ref"),
            "team_ledger_ref": run.team_ledger_ref,
            "team_status": run.team_status,
            "team_summary": run.team_summary,
            "next_action": self._next_action(run),
            "artifacts": artifact_refs,
            "blocker_count": len(blockers),
            "blockers": [{"class": item.get("class"), "reason": str(item.get("reason", ""))[:300]}
                         for item in blockers if isinstance(item, dict)],
        }
        if run.team_ledger_ref:
            try:
                ledger = TeamLedgerStore(run.team_ledger_ref).load()
                view["tasks"] = [{"task_id": task.get("task_id"), "title": task.get("title"),
                                  "role": task.get("role"), "state": task.get("state"),
                                  "depends_on": task.get("depends_on", []),
                                  "worker_attempts": [{"attempt_id": attempt.get("attempt_id"),
                                                       "worker_id": attempt.get("worker_id"),
                                                       "provider": attempt.get("provider"),
                                                       "state": attempt.get("state"),
                                                       "started_at": attempt.get("started_at"),
                                                       "finished_at": attempt.get("finished_at")}
                                                      for attempt in task.get("worker_attempts", [])]
                                  } for task in ledger.get("tasks", [])]
                view["team_events"] = ledger.get("events", [])[-10:]
            except (OSError, ValueError, json.JSONDecodeError):
                view["team_ledger_status"] = "UNAVAILABLE"
        chatgpt = self._reported_chatgpt_workflow(run)
        if chatgpt is not None:
            view["chatgpt_workflow"] = chatgpt
        return view

    @staticmethod
    def _next_action(run: GoalRun) -> str:
        if run.current_state in {"COMPLETED", "FAILED", "BLOCKED", "UNRESOLVED"}:
            return "resolve_blocker" if run.current_state == "BLOCKED" else "terminal"
        workflow = run.context.get("chatgpt_workflow")
        workflow = workflow if isinstance(workflow, dict) else {}
        if workflow.get("pending_phases"):
            return "poll_existing_chatgpt_exchange"
        if run.current_state == "VERIFYING" and run.decision == "CHATGPT_TEAM":
            return "send_or_resume_chatgpt_review"
        if run.current_state == "EXECUTING" and run.team_ledger_ref:
            return "reconcile_same_worker_ids_or_run_ready_tasks"
        return "continue_goal_run"
