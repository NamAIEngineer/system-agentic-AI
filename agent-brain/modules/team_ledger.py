"""Durable bounded team planning and worker fan-out.

This module owns only team task state. Provider selection and execution stay in
the existing provider-neutral dispatch path. Every task has a stable identity,
an explicit ownership scope, and a persisted worker attempt before submission.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
from typing import Any, Callable

from providers.dispatch import dispatch_task, reconcile_task, task_from_spec
from providers.runtime import QUEUE_LOGS_ROOT, adapter_for


TEAM_SCHEMA_VERSION = 1
TERMINAL = {"COMPLETED", "FAILED", "REFUSED", "NO-OP", "BLOCKED"}
REFUSAL_STATUSES = {"REFUSED", "UNAVAILABLE"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value: Any, limit: int = 600) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _required_artifacts(task: dict[str, Any]) -> list[str]:
    """Return the artifacts a worker must leave behind before acceptance.

    Team completion is an evidence claim.  Queue state alone cannot prove that
    a worker performed its assignment, so every durable team task must name at
    least one file in its own scope.  Callers may provide an explicit list;
    the default task definitions do so below.
    """
    value = task.get("required_artifacts", [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _validate_grounded_artifact_schema(task: dict[str, Any], value: dict[str, Any], relative: str) -> list[str]:
    """Validate the bounded JSON contract emitted by a grounded worker.

    Queue completion proves only that the provider exited.  This validator is
    the artifact boundary: malformed or live-eligible marketing evidence stays
    in the workspace for audit but can never advance the team wave.
    """
    errors: list[str] = []
    required_flags = {"DRAFT_ONLY": True, "rehearsal": True, "liveEligible": False,
                      "externalSideEffects": False, "unknownRetryAllowed": False}
    for key, expected in required_flags.items():
        if value.get(key) is not expected:
            errors.append(f"{relative}: {key} must be {expected!r}")

    kind = str(task.get("artifact_kind") or "").strip().lower()
    if kind == "measurement":
        required = {"verified_publication_count", "channel_metrics", "attribution",
                    "evidence_refs", "live_eligibility"}
        missing = sorted(key for key in required if key not in value)
        errors.extend(f"{relative}: missing {key}" for key in missing)
        count = value.get("verified_publication_count")
        if count != "UNKNOWN" and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
            errors.append(f"{relative}: verified_publication_count must be a nonnegative integer or 'UNKNOWN'")
        for key in ("channel_metrics", "attribution", "evidence_refs"):
            item = value.get(key)
            if item != "UNKNOWN" and not isinstance(item, (dict, list)):
                errors.append(f"{relative}: {key} must be an object, array, or 'UNKNOWN'")
        eligibility = value.get("live_eligibility")
        if eligibility not in (False, "UNKNOWN"):
            errors.append(f"{relative}: live_eligibility must be false or 'UNKNOWN'")
    elif kind == "copy":
        required = {"draft_text", "audience", "claims"}
        errors.extend(f"{relative}: missing {key}" for key in sorted(required - value.keys()))
        if not isinstance(value.get("draft_text"), str) or not value.get("draft_text", "").strip():
            errors.append(f"{relative}: draft_text must be non-empty text")
        if not isinstance(value.get("audience"), str) or not value.get("audience", "").strip():
            errors.append(f"{relative}: audience must be non-empty text")
        if value.get("claims") != "UNKNOWN" and not isinstance(value.get("claims"), (list, str)):
            errors.append(f"{relative}: claims must be an array, text, or 'UNKNOWN'")
        if task.get("substantive_claims_required"):
            claims = value.get("claims")
            concrete = []
            if isinstance(claims, list):
                concrete = [item for item in claims
                            if isinstance(item, str) and item.strip() and item.strip().upper() != "UNKNOWN"]
            elif isinstance(claims, str) and claims.strip().upper() != "UNKNOWN":
                concrete = [claims]
            if not concrete:
                errors.append(f"{relative}: substantive claims required; claims cannot be UNKNOWN")
            draft = value.get("draft_text")
            if isinstance(draft, str) and draft.strip().lower() in {
                "bounded draft grounded in the files",
                "bounded corrected copy grounded in observed input",
                "bounded corrected copy grounded in observed inputs",
            }:
                errors.append(f"{relative}: draft_text must contain substantive source-grounded content")
    elif kind == "integration":
        for key in ("input_refs", "source_refs"):
            if key not in value or not isinstance(value.get(key), list) or not value.get(key):
                errors.append(f"{relative}: {key} must be a non-empty array")
    elif kind == "verification":
        if value.get("verdict") != "PASS_BOUNDED":
            errors.append(f"{relative}: verdict must be PASS_BOUNDED")
        if not isinstance(value.get("checked_artifacts"), list) or not value.get("checked_artifacts"):
            errors.append(f"{relative}: checked_artifacts must be a non-empty array")
        if not isinstance(value.get("evidenceRefs"), list) or not value.get("evidenceRefs"):
            errors.append(f"{relative}: evidenceRefs must be a non-empty array")
        if task.get("verification_input_refs"):
            checked = value.get("checked_artifacts") if isinstance(value.get("checked_artifacts"), list) else []
            missing = [item for item in task["verification_input_refs"] if item not in checked]
            errors.extend(f"{relative}: checked_artifacts missing declared input: {item}" for item in missing)
        if task.get("verification_source_evidence_required"):
            evidence = value.get("evidenceRefs") if isinstance(value.get("evidenceRefs"), list) else []
            missing = [item for item in task.get("verification_source_refs", []) if item not in evidence]
            errors.extend(f"{relative}: evidenceRefs missing source evidence: {item}" for item in missing)
    return errors


def _validate_worker_result(task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Validate a terminal provider result against the task's evidence scope."""
    status = result.get("execution_status", result.get("status", "UNKNOWN"))
    if status in REFUSAL_STATUSES:
        return {"accepted": False, "classification": "REFUSED", "reason": _safe_text(
            result.get("reason") or ((result.get("errors") or [{}])[0].get("message")), 500)}
    if status != "COMPLETED":
        return {"accepted": False, "classification": "FAILED", "reason": _safe_text(
            result.get("reason") or ((result.get("errors") or [{}])[0].get("message")) or
            f"provider status {status}", 500)}

    required = _required_artifacts(task)
    if not required:
        return {"accepted": False, "classification": "NO-OP",
                "reason": "team task has no required artifact evidence"}
    workspace = Path(task["workspace"]).resolve(strict=True)
    present: list[str] = []
    missing: list[str] = []
    invalid: list[str] = []
    for relative in required:
        candidate = (workspace / relative).resolve(strict=False)
        try:
            candidate.relative_to(workspace)
        except ValueError:
            invalid.append(relative)
            continue
        if candidate.is_file() and candidate.stat().st_size > 0:
            present.append(relative)
        else:
            missing.append(relative)
    if invalid:
        return {"accepted": False, "classification": "FAILED",
                "reason": f"required artifact escapes workspace: {', '.join(invalid)}",
                "required_artifacts": required, "present": present, "missing": missing}
    if missing:
        return {"accepted": False, "classification": "NO-OP",
                "reason": f"worker completed without required artifact: {', '.join(missing)}",
                "required_artifacts": required, "present": present, "missing": missing}
    if task.get("grounding_required"):
        grounding_errors: list[str] = []
        for relative in present:
            candidate = (workspace / relative).resolve(strict=True)
            if candidate.suffix.lower() != ".json":
                grounding_errors.append(f"{relative}: grounding contract requires JSON")
                continue
            try:
                value = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                grounding_errors.append(f"{relative}: invalid JSON")
                continue
            if not isinstance(value, dict):
                grounding_errors.append(f"{relative}: JSON object required")
                continue
            refs = value.get("source_refs")
            if not isinstance(refs, list) or not refs or any(not isinstance(item, str) or not item.strip() for item in refs):
                grounding_errors.append(f"{relative}: source_refs missing")
            expected_refs = task.get("grounding_source_refs")
            if isinstance(expected_refs, list) and expected_refs:
                expected = [item for item in expected_refs if isinstance(item, str) and item.strip()]
                missing_refs = [item for item in expected if item not in (refs if isinstance(refs, list) else [])]
                if missing_refs:
                    grounding_errors.append(
                        f"{relative}: source_refs missing declared grounding refs: {', '.join(missing_refs)}"
                    )
            grounding_errors.extend(_validate_grounded_artifact_schema(task, value, relative))
        if grounding_errors:
            return {"accepted": False, "classification": "FAILED",
                    "reason": "; ".join(grounding_errors)[:500],
                    "required_artifacts": required, "present": present, "missing": []}
    return {"accepted": True, "classification": "ACCEPTED",
            "required_artifacts": required, "present": present, "missing": []}


def _failed_artifact_recovery(task: dict[str, Any], attempt: dict[str, Any],
                              provider_status: str) -> dict[str, Any] | None:
    """Recover one artifact-only worker after a post-write no-progress stop.

    A local worker can persist the declared artifact, then exit nonzero when
    the model repeats the same write. Treat that narrow case as accepted
    evidence only when the immutable pre-dispatch snapshot, current artifact
    schema, queue log write receipt, and no-progress terminal event all agree.
    Other provider failures remain failures and are never retried here.
    """
    if provider_status not in {"FAILED", "FAILED_LOST"}:
        return None
    snapshot = attempt.get("artifact_snapshot")
    if not _complete_artifact_snapshot(task, snapshot):
        return None
    validation = _validate_worker_result(task, {"execution_status": "COMPLETED"})
    if not validation.get("accepted"):
        return None
    unchanged = _unchanged_artifacts(task, snapshot, validation.get("present", []))
    if unchanged:
        return None

    raw_log = attempt.get("log_path")
    if isinstance(raw_log, str) and raw_log.strip():
        log_path = Path(raw_log).expanduser().resolve(strict=False)
        try:
            log_path.relative_to(QUEUE_LOGS_ROOT.resolve(strict=True))
        except (OSError, ValueError):
            return None
    else:
        attempt_id = str(attempt.get("attempt_id") or "")
        match = re.search(r"-attempt-(\d+)$", attempt_id)
        number = match.group(1) if match else "1"
        log_path = (QUEUE_LOGS_ROOT / f"{task.get('task_id')}-{number}.jsonl").resolve(strict=False)
    try:
        log_path.relative_to(QUEUE_LOGS_ROOT.resolve(strict=True))
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return None

    write_receipt = False
    no_progress_stop = False
    required = set(validation.get("present", []))
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (event.get("runtime") == "local_tool_result"
                and event.get("tool") == "write_file"):
            try:
                result = json.loads(event.get("result", ""))
            except (TypeError, json.JSONDecodeError):
                result = {}
            if isinstance(result, dict) and result.get("written") in required:
                write_receipt = True
        if event.get("runtime") == "no_progress_guard_failed_closed":
            no_progress_stop = True
    if not (write_receipt and no_progress_stop):
        return None
    return {
        **validation,
        "accepted": True,
        "classification": "ACCEPTED_AFTER_PROVIDER_FAILURE",
        "reason": "declared artifact write was logged before the local no-progress guard stopped the worker",
        "provider_status": provider_status,
        "evidence": {"queue_log": str(log_path), "write_receipt": True,
                      "no_progress_guard": True},
    }


def _artifact_snapshot(task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Capture declared artifacts before a worker starts."""
    workspace = Path(task["workspace"]).resolve(strict=True)
    snapshot: dict[str, dict[str, Any]] = {}
    for relative in _required_artifacts(task):
        candidate = (workspace / relative).resolve(strict=False)
        try:
            candidate.relative_to(workspace)
        except ValueError:
            snapshot[relative] = {"invalid": True}
            continue
        if candidate.is_file():
            stat = candidate.stat()
            snapshot[relative] = {
                "exists": True,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            }
        else:
            snapshot[relative] = {"exists": False}
    return snapshot


def _unchanged_artifacts(task: dict[str, Any], before: dict[str, dict[str, Any]] | None,
                         present: list[str]) -> list[str]:
    if not before:
        return []
    workspace = Path(task["workspace"]).resolve(strict=True)
    unchanged: list[str] = []
    for relative in present:
        previous = before.get(relative, {})
        candidate = (workspace / relative).resolve(strict=False)
        stat = candidate.stat()
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if (previous.get("exists") and previous.get("size") == stat.st_size
                and previous.get("mtime_ns") == stat.st_mtime_ns
                and previous.get("sha256") == digest):
            unchanged.append(relative)
    return unchanged


def _complete_artifact_snapshot(task: dict[str, Any], snapshot: Any) -> bool:
    """Require a structurally complete pre-dispatch snapshot for recovery.

    A recovery receipt is allowed to accept an artifact only when the ledger
    records what that artifact looked like before dispatch.  An empty or
    partial mapping would otherwise let a pre-existing file masquerade as
    worker evidence.
    """
    required = _required_artifacts(task)
    if not required or not isinstance(snapshot, dict):
        return not required
    for relative in required:
        entry = snapshot.get(relative)
        if not isinstance(entry, dict):
            return False
        if entry.get("invalid") is True:
            continue
        exists = entry.get("exists")
        if not isinstance(exists, bool):
            return False
        if exists and (
            not isinstance(entry.get("size"), int)
            or not isinstance(entry.get("mtime_ns"), int)
            or not isinstance(entry.get("sha256"), str)
            or len(entry["sha256"]) != 64
        ):
            return False
    return True


class TeamLedgerStore:
    """Atomic JSON ledger for a single GoalRun's team."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, value: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=".team.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def load(self) -> dict[str, Any]:
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != TEAM_SCHEMA_VERSION:
            raise ValueError("Invalid team ledger schema")
        return value


def _default_tasks(goal: str, workspace: str, run_id: str) -> list[dict[str, Any]]:
    """Create a safe two-worker baseline for broad team goals.

    Workers write separate evidence files. They do not edit product code until
    a later task supplies explicit disjoint ownership scopes.
    """
    report_root = Path(workspace) / ".system-agentic-goals" / run_id / "team"
    rel_root = report_root.relative_to(Path(workspace).resolve())
    return [
        {
            "task_id": f"{run_id}-architect",
            "title": "Tech lead reconnaissance",
            "role": "tech-lead",
            "objective": (
                f"Inspect the workspace for this goal: {goal}. Write a concise, source-grounded "
                f"architecture note to {rel_root / 'architect.md'}. Include observed entrypoints, "
                "candidate files, risks, and exactly one recommended next step. Do not edit product code."
            ),
            "workspace": workspace,
            "scope": str(rel_root / "architect.md"),
            "required_artifacts": [str(rel_root / "architect.md")],
            "depends_on": [],
            "execution_policy": "WORKSPACE_WRITE",
            "acceptance_criteria": ["architect.md exists", "findings cite observed paths", "no product code changed"],
        },
        {
            "task_id": f"{run_id}-qa",
            "title": "Independent QA reconnaissance",
            "role": "reviewer",
            "objective": (
                f"Independently inspect the workspace for this goal: {goal}. Write a concise QA note "
                f"to {rel_root / 'qa.md'}. List available checks, likely failure modes, and evidence needed "
                "for acceptance. Do not edit product code or read secrets."
            ),
            "workspace": workspace,
            "scope": str(rel_root / "qa.md"),
            "required_artifacts": [str(rel_root / "qa.md")],
            "depends_on": [],
            "execution_policy": "WORKSPACE_WRITE",
            "acceptance_criteria": ["qa.md exists", "checks are concrete", "no product code changed"],
        },
        {
            "task_id": f"{run_id}-integrator",
            "title": "Integrator synthesis",
            "role": "integrator",
            "objective": (
                f"Read {rel_root / 'architect.md'} and {rel_root / 'qa.md'}. Synthesize a bounded integration "
                f"note at {rel_root / 'integration.md'} for goal: {goal}. Preserve disagreements and name "
                "the next executable task. Do not edit product code."
            ),
            "workspace": workspace,
            "scope": str(rel_root / "integration.md"),
            "required_artifacts": [str(rel_root / "integration.md")],
            "depends_on": [f"{run_id}-architect", f"{run_id}-qa"],
            "execution_policy": "WORKSPACE_WRITE",
            "acceptance_criteria": ["integration.md exists", "both worker notes referenced", "next task named"],
        },
        {
            "task_id": f"{run_id}-verifier",
            "title": "Independent final verifier",
            "role": "verifier",
            "objective": (
                f"Independently reread {rel_root / 'architect.md'}, {rel_root / 'qa.md'}, and "
                f"{rel_root / 'integration.md'}. Write a final verification receipt to "
                f"{rel_root / 'verification.md'} for goal: {goal}. State PASS/FAIL, cite each file, "
                "and list any unresolved finding. Do not edit product code."
            ),
            "workspace": workspace,
            "scope": str(rel_root / "verification.md"),
            "required_artifacts": [str(rel_root / "verification.md")],
            "depends_on": [f"{run_id}-integrator"],
            "execution_policy": "WORKSPACE_WRITE",
            "acceptance_criteria": ["verification.md exists", "all three notes checked", "PASS or FAIL stated"],
        },
    ]


def _normalize_tasks(raw: Any, *, goal: str, workspace: str, run_id: str) -> list[dict[str, Any]]:
    if raw is None:
        return _default_tasks(goal, workspace, run_id)
    if not isinstance(raw, list) or not raw:
        raise ValueError("team_tasks must be a non-empty array")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"team_tasks[{index}] must be an object")
        task_id = _safe_text(item.get("task_id"), 128) or f"{run_id}-task-{index + 1}"
        if task_id in ids or not task_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"duplicate or invalid team task ID: {task_id!r}")
        ids.add(task_id)
        task = dict(item)
        task.update({
            "task_id": task_id,
            "title": _safe_text(item.get("title"), 200) or task_id,
            "role": _safe_text(item.get("role"), 80) or "worker",
            # Worker prompts carry the orchestration preamble, bounded evidence
            # paths, and acceptance/constraint text.  A 3,000-character cap
            # could cut the prompt between the safety boundary and the actual
            # artifact path, causing a worker to loop on discovery and never
            # produce its declared evidence. Keep the durable ledger bounded
            # while leaving enough room for the complete dispatch contract.
            "objective": _safe_text(item.get("objective"), 12000),
            "workspace": str(Path(item.get("workspace", workspace)).expanduser().resolve(strict=True)),
            "scope": _safe_text(item.get("scope"), 500),
            "required_artifacts": list(item.get("required_artifacts", []))
                if isinstance(item.get("required_artifacts", []), list) else [],
            "depends_on": list(item.get("depends_on", [])),
            "execution_policy": item.get("execution_policy", "WORKSPACE_WRITE"),
            "acceptance_criteria": list(item.get("acceptance_criteria", [])),
            "grounding_required": bool(item.get("grounding_required", False)),
            "grounding_source_refs": list(item.get("grounding_source_refs", []))
                if isinstance(item.get("grounding_source_refs", []), list) else [],
            # Keep artifact-specific acceptance metadata in the durable
            # ledger.  Recovery used to copy the task object through
            # normalization but silently drop these fields, allowing a
            # placeholder copy or a verifier with no source-evidence contract
            # to be accepted after a provider reported COMPLETED.
            "artifact_kind": _safe_text(item.get("artifact_kind"), 80).lower(),
            "substantive_claims_required": bool(item.get("substantive_claims_required", False)),
            "verification_input_refs": list(item.get("verification_input_refs", []))
                if isinstance(item.get("verification_input_refs", []), list) else [],
            "verification_source_evidence_required": bool(
                item.get("verification_source_evidence_required", False)
            ),
            "verification_source_refs": list(item.get("verification_source_refs", []))
                if isinstance(item.get("verification_source_refs", []), list) else [],
            "input_refs": list(item.get("input_refs", []))
                if isinstance(item.get("input_refs", []), list) else [],
            "tool_allowlist": list(item.get("tool_allowlist", []))
                if isinstance(item.get("tool_allowlist", []), list) else [],
        })
        if not task["objective"]:
            raise ValueError(f"team task {task_id!r} requires objective")
        if not task["required_artifacts"] and task["scope"]:
            task["required_artifacts"] = [task["scope"]]
        if any(not isinstance(path, str) or not path.strip() for path in task["required_artifacts"]):
            raise ValueError(f"team task {task_id!r} has invalid required_artifacts")
        if task["execution_policy"] not in {"READ_ONLY", "WORKSPACE_WRITE"}:
            raise ValueError(f"team task {task_id!r} has unsupported execution policy")
        if any(not isinstance(tool, str) or not tool.strip() for tool in task["tool_allowlist"]):
            raise ValueError(f"team task {task_id!r} has invalid tool_allowlist")
        if any(not isinstance(dep, str) or dep not in ids and dep not in {x.get("task_id") for x in result} for dep in task["depends_on"]):
            # Forward references are allowed; validate after the complete ID set.
            pass
        result.append(task)
    known = {item["task_id"] for item in result}
    for task in result:
        if any(dep not in known or dep == task["task_id"] for dep in task["depends_on"]):
            raise ValueError(f"team task {task['task_id']!r} has unknown/self dependency")
    return result


def create_ledger(*, goal: str, workspace: str, run_id: str, raw_tasks: Any = None,
                  path: str | Path) -> dict[str, Any]:
    tasks = _normalize_tasks(raw_tasks, goal=goal, workspace=workspace, run_id=run_id)
    ledger = {
        "schema_version": TEAM_SCHEMA_VERSION,
        "team_run_id": f"team-{run_id}",
        "goal_run_id": run_id,
        "goal": goal,
        "workspace": workspace,
        "created_at": _now(),
        "updated_at": _now(),
        "status": "READY",
        "tasks": [
            {**task, "state": "PENDING", "worker_attempts": [], "result": None, "error": None}
            for task in tasks
        ],
        "events": [],
    }
    TeamLedgerStore(path).save(ledger)
    return ledger


class TeamRunner:
    """Run ready tasks concurrently, then continue dependency waves."""

    def __init__(self, ledger_path: str | Path, *, dispatch: Callable[..., dict[str, Any]] = dispatch_task,
                 max_workers: int = 2, wait_seconds: float = 600,
                 provider_policy: dict[str, Any] | None = None,
                 shared_context: dict[str, Any] | None = None):
        self.store = TeamLedgerStore(ledger_path)
        self.dispatch = dispatch
        self.max_workers = max(1, min(int(max_workers), 8))
        self.wait_seconds = max(0.0, float(wait_seconds))
        self.provider_policy = dict(provider_policy or {"allowed_providers": ["local-qwen"]})
        self.shared_context = dict(shared_context or {})
        self._ledger_lock = threading.RLock()

    def run(self) -> dict[str, Any]:
        ledger = self.store.load()
        self._adopt_recoverable_lost_attempts(ledger)
        self._recover_running(ledger)
        self._unblock_completed_dependencies(ledger)
        ledger["status"] = "RUNNING"
        self._event(ledger, "team_started")
        while True:
            # A dependency can be reconciled by a prior wave or by startup
            # recovery. Re-open only the narrow stale blocker emitted by this
            # runner; intentional/manual BLOCKED states remain terminal.
            self._unblock_completed_dependencies(ledger)
            ready = self._ready(ledger)
            if not ready:
                if all(task["state"] in TERMINAL for task in ledger["tasks"]):
                    break
                # An accepted provider job may still be running after the
                # bounded dispatch wait. Keep dependants pending until the
                # same provider task ID is reconciled; blocking them here
                # would turn a resumable timeout into a false failure.
                if any(task["state"] == "RUNNING" for task in ledger["tasks"]):
                    break
                # Dependencies failed or the ledger was malformed.
                for task in ledger["tasks"]:
                    if task["state"] == "PENDING":
                        task["state"] = "BLOCKED"
                        task["error"] = "dependency did not complete"
                break
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(ready)),
                                    thread_name_prefix="system-agentic-team") as pool:
                futures = {pool.submit(self._run_task, ledger, task["task_id"]): task["task_id"] for task in ready}
                for future in as_completed(futures):
                    task_id = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        task = self._task(ledger, task_id)
                        with self._ledger_lock:
                            task["state"] = "FAILED"
                            task["error"] = _safe_text(str(exc), 500)
                            self._event(ledger, "task_runner_exception", task_id, {"error": task["error"]})
                    with self._ledger_lock:
                        self.store.save(ledger)
        # A provider timeout is not a provider failure.  Keep the durable team
        # run resumable when an accepted queue job is still running; recovery
        # will observe the same task ID and either adopt its result or mark a
        # genuine loss.  Do not block dependent tasks or create a duplicate
        # attempt while this state is in flight.
        if any(task["state"] == "RUNNING" for task in ledger["tasks"]):
            ledger["status"] = "RUNNING"
            self._event(ledger, "team_waiting_for_provider")
        else:
            ledger["status"] = "COMPLETED" if all(task["state"] == "COMPLETED" for task in ledger["tasks"]) else "FAILED"
        ledger["updated_at"] = _now()
        self._event(ledger, "team_finished" if ledger["status"] != "RUNNING" else "team_paused",
                    payload={"status": ledger["status"]})
        self.store.save(ledger)
        return ledger

    def prepare_resume(self) -> dict[str, Any]:
        """Adopt a durable queue job for a lost attempt without submitting.

        A service can die after the queue commits a task but before the
        provider receipt is copied into the team ledger. Reconcile the stable
        task ID first, then let the normal runner recover it. Never create a
        second attempt here.
        """
        ledger = self.store.load()
        changed = self._adopt_completed_artifacts(ledger)
        changed = self._adopt_recoverable_lost_attempts(ledger) or changed
        changed = self._unblock_completed_dependencies(ledger) or changed
        if changed:
            self.store.save(ledger)
        return ledger

    def _adopt_completed_artifacts(self, ledger: dict[str, Any]) -> bool:
        """Adopt a completed queue job whose controller lost its terminal receipt.

        A worker may persist its declared artifact, then the controller can
        record a provider failure or disappear before copying the terminal
        result into the team ledger. Accept only when the same stable queue
        task ID now reports COMPLETED, the pre-dispatch snapshot proves the
        artifact was absent or changed, and the normal artifact validator
        passes. No provider submission occurs here.
        """
        changed = False
        for task in ledger.get("tasks", []):
            if not isinstance(task, dict) or task.get("state") not in {"FAILED", "NO-OP", "REFUSED"}:
                continue
            attempts = task.get("worker_attempts") or []
            attempt = attempts[-1] if attempts and isinstance(attempts[-1], dict) else None
            if not isinstance(attempt, dict) or not isinstance(attempt.get("task_spec"), dict):
                continue
            status, provider = self._existing_provider_status(task, attempt)
            if status != "COMPLETED" or not provider:
                continue
            validation = _validate_worker_result(task, {"execution_status": "COMPLETED"})
            if not validation.get("accepted"):
                continue
            unchanged = _unchanged_artifacts(task, attempt.get("artifact_snapshot"),
                                             validation.get("present", []))
            if unchanged:
                continue
            attempt.update({"state": "COMPLETED", "provider": provider,
                            "provider_status": "COMPLETED", "provider_terminal_status": status,
                            "finished_at": _now(), "acceptance": validation,
                            "summary": "adopted persisted artifact after controller recovery",
                            "evidence": [{"reconciled": True, "provider": provider}]})
            task["state"] = "COMPLETED"
            task["result"] = {"status": "COMPLETED", "provider_status": "COMPLETED",
                               "summary": attempt["summary"], "evidence": attempt["evidence"],
                               "acceptance": validation}
            task["error"] = None
            self._event(ledger, "task_reconciled_after_controller_recovery", task.get("task_id"),
                        {"provider": provider, "attempt_id": attempt.get("attempt_id")})
            changed = True
        if changed:
            ledger["status"] = "RUNNING"
        return changed

    def _unblock_completed_dependencies(self, ledger: dict[str, Any]) -> bool:
        """Reopen stale dependency blockers after recovery.

        ``run()`` historically marked pending dependants BLOCKED when an
        upstream task was not terminal yet. Once that upstream task later
        completed, the dependant stayed terminal forever because ``_ready``
        only considers PENDING tasks. Reopen only that exact runner-owned
        blocker, and only when it has a non-empty dependency list whose every
        dependency is COMPLETED. Other BLOCKED states require their original
        recovery path.
        """
        states = {task.get("task_id"): task.get("state")
                  for task in ledger.get("tasks", []) if isinstance(task, dict)}
        changed = False
        for task in ledger.get("tasks", []):
            if not isinstance(task, dict):
                continue
            if task.get("state") != "BLOCKED" or task.get("error") != "dependency did not complete":
                continue
            dependencies = task.get("depends_on")
            if not isinstance(dependencies, list) or not dependencies:
                continue
            if not all(states.get(dependency) == "COMPLETED" for dependency in dependencies):
                continue
            task["state"] = "PENDING"
            task["error"] = None
            self._event(ledger, "task_unblocked_after_dependency_recovery", task.get("task_id"),
                        {"dependencies": list(dependencies)})
            changed = True
        if changed:
            ledger["status"] = "RUNNING"
        return changed

    def _ready(self, ledger: dict[str, Any]) -> list[dict[str, Any]]:
        states = {task["task_id"]: task["state"] for task in ledger["tasks"]}
        return [task for task in ledger["tasks"]
                if task["state"] == "PENDING" and all(states.get(dep) == "COMPLETED" for dep in task["depends_on"])]

    def _task(self, ledger: dict[str, Any], task_id: str) -> dict[str, Any]:
        return next(task for task in ledger["tasks"] if task["task_id"] == task_id)

    def _provider_candidates(self, attempt: dict[str, Any]) -> list[str]:
        provider = attempt.get("provider")
        if isinstance(provider, str) and provider.strip():
            return [provider]
        allowed = self.provider_policy.get("allowed_providers")
        if isinstance(allowed, list):
            return [item for item in allowed if isinstance(item, str) and item.strip()]
        return ["local-qwen"]

    def _existing_provider_status(self, task: dict[str, Any], attempt: dict[str, Any]) -> tuple[str, str | None]:
        spec = attempt.get("task_spec")
        if not isinstance(spec, dict):
            return "MISSING", None
        try:
            parsed = task_from_spec(spec)
        except (OSError, RuntimeError, ValueError):
            return "MISSING", None
        saw_unknown = False
        for provider in self._provider_candidates(attempt):
            try:
                result = adapter_for(provider).status(parsed)
                status = result.status
            except (OSError, RuntimeError, ValueError):
                status = "UNKNOWN"
            if status in {"SUBMITTED", "RUNNING", "COMPLETED"}:
                return status, provider
            if status == "UNKNOWN":
                saw_unknown = True
        return ("UNKNOWN" if saw_unknown else "MISSING"), None

    def _adopt_recoverable_lost_attempts(self, ledger: dict[str, Any]) -> bool:
        """Recover queue-backed FAILED_LOST attempts before normal execution."""
        changed = False
        for task in ledger.get("tasks", []):
            if task.get("state") not in {"FAILED", "FAILED_LOST"}:
                continue
            attempts = task.get("worker_attempts") or []
            attempt = attempts[-1] if attempts else None
            if not isinstance(attempt, dict) or attempt.get("state") != "FAILED_LOST":
                continue
            status, provider = self._existing_provider_status(task, attempt)
            if status not in {"SUBMITTED", "RUNNING", "COMPLETED"} or not provider:
                continue
            attempt.update({"provider": provider, "provider_status": status,
                            "worker_id": attempt.get("worker_id") or task.get("task_id"),
                            "state": "RUNNING", "recovered_at": _now()})
            task["state"] = "RUNNING"
            task["error"] = "provider task recovered by stable task ID; no new attempt submitted"
            self._event(ledger, "task_recovered_from_lost_attempt", task.get("task_id"),
                        {"provider": provider, "provider_status": status,
                         "attempt_id": attempt.get("attempt_id")})
            changed = True
        if changed:
            ledger["status"] = "RUNNING"
        return changed

    def _recover_running(self, ledger: dict[str, Any]) -> None:
        """Reconcile work that was in flight when the service stopped.

        Existing provider jobs are observed by their stable task ID. A
        completed job is adopted without a new attempt. A missing or lost job
        is marked terminal so recovery never blindly submits a duplicate. A
        still-running job is polled for the bounded runner wait interval.
        """
        for task in ledger.get("tasks", []):
            if task.get("state") != "RUNNING":
                continue
            attempts = task.get("worker_attempts") or []
            attempt = attempts[-1] if attempts else None
            if not isinstance(attempt, dict):
                task["state"] = "FAILED"
                task["error"] = "FAILED_LOST: running task had no persisted attempt"
                self._event(ledger, "task_recovery_failed", task.get("task_id"), {"reason": task["error"]})
                continue
            spec = attempt.get("task_spec")
            provider = attempt.get("provider")
            if not isinstance(spec, dict) or not isinstance(provider, str) or not provider:
                attempt.update({"state": "FAILED_LOST", "finished_at": _now()})
                task["state"] = "FAILED"
                task["error"] = "FAILED_LOST: provider identity or task spec was not persisted"
                self._event(ledger, "task_recovery_failed", task.get("task_id"), {"reason": task["error"]})
                continue
            try:
                parsed = task_from_spec(spec)
            except (OSError, ValueError, RuntimeError) as exc:
                attempt.update({"state": "FAILED_LOST", "finished_at": _now(),
                                "errors": [{"code": "RECOVERY_SPEC_INVALID", "message": _safe_text(str(exc), 500)}]})
                task["state"] = "FAILED"
                task["error"] = "FAILED_LOST: persisted task spec is invalid"
                self._event(ledger, "task_recovery_failed", task.get("task_id"), {"reason": task["error"]})
                continue
            status = "UNKNOWN"
            deadline = time.monotonic() + self.wait_seconds
            while status in {"SUBMITTED", "RUNNING", "UNKNOWN"}:
                try:
                    result = reconcile_task(parsed, provider_id=provider)
                    status = result.get("execution_status", result.get("status", "UNKNOWN"))
                    attempt.update({"worker_id": result.get("queue_job_id") or attempt.get("worker_id") or task.get("task_id"),
                                    "provider": result.get("provider") or provider,
                                    "last_reconciled_at": _now()})
                    if status == "COMPLETED":
                        snapshot = attempt.get("artifact_snapshot")
                        if not _complete_artifact_snapshot(task, snapshot):
                            validation = {
                                "accepted": False,
                                "classification": "FAILED_LOST",
                                "reason": "recovery lacks a complete pre-dispatch artifact snapshot",
                                "required_artifacts": _required_artifacts(task),
                                "present": [],
                                "missing": _required_artifacts(task),
                            }
                        else:
                            validation = _validate_worker_result(task, result)
                            unchanged = _unchanged_artifacts(
                                task, snapshot, validation.get("present", []),
                            )
                            if unchanged and validation.get("accepted"):
                                validation.update({
                                    "accepted": False,
                                    "classification": "NO-OP",
                                    "reason": f"worker completed without changing required artifact: {', '.join(unchanged)}",
                                    "unchanged": unchanged,
                                })
                        accepted_state = "COMPLETED" if validation.get("accepted") else validation.get("classification", "FAILED")
                        attempt.update({"state": accepted_state, "provider_status": status, "finished_at": _now(),
                                        "summary": _safe_text(result.get("summary"), 1200),
                                        "evidence": result.get("evidence", []) if isinstance(result.get("evidence"), list) else [],
                                        "acceptance": validation,
                                        "errors": result.get("errors", []) if isinstance(result.get("errors"), list) else []})
                        task["state"] = accepted_state
                        task["result"] = {"status": accepted_state, "provider_status": status,
                                           "summary": attempt.get("summary"), "evidence": attempt.get("evidence", []),
                                           "acceptance": validation}
                        task["error"] = None if accepted_state == "COMPLETED" else _safe_text(validation.get("reason"), 500)
                        self._event(ledger, "task_reconciled", task.get("task_id"), {"status": status,
                                                                                         "accepted_state": accepted_state,
                                                                                         "attempt_id": attempt.get("attempt_id")})
                        break
                    if status in {"FAILED", "FAILED_LOST", "MISSING", "REFUSED", "UNAVAILABLE"}:
                        recovered = _failed_artifact_recovery(task, attempt, status)
                        if recovered is not None:
                            attempt.update({"state": "COMPLETED", "provider_status": "COMPLETED",
                                            "provider_terminal_status": status,
                                            "finished_at": _now(),
                                            "summary": recovered.get("reason"),
                                            "evidence": [recovered.get("evidence", {})],
                                            "acceptance": recovered})
                            task["state"] = "COMPLETED"
                            task["result"] = {"status": "COMPLETED", "provider_status": "COMPLETED",
                                               "provider_terminal_status": status,
                                               "summary": attempt.get("summary"),
                                               "evidence": attempt.get("evidence", []),
                                               "acceptance": recovered}
                            task["error"] = None
                            self._event(ledger, "task_reconciled_after_artifact_write", task.get("task_id"),
                                        {"status": status, "attempt_id": attempt.get("attempt_id")})
                            break
                        terminal_state = ("FAILED_LOST" if status in {"FAILED_LOST", "MISSING"}
                                          else "REFUSED" if status in {"REFUSED", "UNAVAILABLE"}
                                          else "FAILED")
                        attempt.update({"state": terminal_state, "provider_status": status,
                                        "finished_at": _now(),
                                        "errors": result.get("errors", []) if isinstance(result.get("errors"), list) else []})
                        task["state"] = terminal_state
                        task["result"] = {"status": terminal_state, "provider_status": status,
                                           "errors": attempt.get("errors", [])}
                        task["error"] = f"{terminal_state}: provider job did not complete"
                        self._event(ledger, "task_recovery_failed", task.get("task_id"), {"status": status,
                                                                                             "attempt_id": attempt.get("attempt_id")})
                        break
                except (OSError, RuntimeError, ValueError) as exc:
                    status = "UNKNOWN"
                    attempt["last_reconcile_error"] = _safe_text(str(exc), 500)
                if time.monotonic() >= deadline:
                    attempt.update({"state": "FAILED_LOST", "finished_at": _now()})
                    task["state"] = "FAILED"
                    task["error"] = "FAILED_LOST: bounded recovery wait expired"
                    self._event(ledger, "task_recovery_failed", task.get("task_id"), {"reason": task["error"]})
                    break
                threading.Event().wait(min(1.0, max(0.0, deadline - time.monotonic())))
            self.store.save(ledger)

    def _run_task(self, ledger: dict[str, Any], task_id: str) -> None:
        with self._ledger_lock:
            task = self._task(ledger, task_id)
            if task["state"] == "COMPLETED":
                return
            attempt_no = len(task["worker_attempts"]) + 1
        attempt_id = f"{task_id}-attempt-{attempt_no}"
        spec = {
            "task_id": task_id,
            "title": task["title"],
            "workspace": task["workspace"],
            "objective": task["objective"],
            "role": "worker",
            "required_capabilities": ["task.submit"],
            "context": {"team_run_id": ledger["team_run_id"], "team_task_id": task_id,
                        "ownership_scope": task["scope"],
                        "required_artifacts": list(task["required_artifacts"]),
                        "grounding_required": bool(task.get("grounding_required", False)),
                        "grounding_source_refs": list(task.get("grounding_source_refs", [])),
                        **self.shared_context},
            "constraints": ["Do not read credentials or full chat transcripts.", f"Own only scope: {task['scope']}"],
            "acceptance_criteria": task["acceptance_criteria"],
            "execution_policy": task["execution_policy"],
            "provider_policy": self.provider_policy,
            "execution_context": {
                "origin": "external-orchestrator", "host_role": "orchestrator", "dispatch_depth": 1,
                "direct_or_dispatch": "DISPATCH", "parent_task_id": ledger["goal_run_id"],
                "logical_task_id": task_id, "execution_attempt_id": attempt_id,
                "idempotency_key": f"{ledger['team_run_id']}:{task_id}:{attempt_no}",
                "artifact_paths": list(task["required_artifacts"]),
                "artifact_kind": task.get("artifact_kind"),
                "artifact_completion_policy": "accept-after-write-file-empty-turn",
            },
        }
        if task.get("artifact_kind") == "integration":
            spec["execution_context"]["artifact_input_refs"] = list(task.get("input_refs", []))
        if task.get("tool_allowlist"):
            spec["execution_context"]["tool_allowlist"] = list(task["tool_allowlist"])
        # Persist the attempt before submitting. Recovery can reconcile this ID.
        artifact_snapshot = _artifact_snapshot(task)
        spec["execution_context"]["artifact_snapshot"] = artifact_snapshot
        attempt = {"attempt_id": attempt_id, "worker_id": None, "provider": None,
                   "state": "PREPARED", "started_at": _now(), "finished_at": None,
                   "execution_context": spec["execution_context"], "task_spec": spec,
                   "artifact_snapshot": artifact_snapshot}
        with self._ledger_lock:
            task["worker_attempts"].append(attempt)
            task["state"] = "RUNNING"
            self._event(ledger, "task_prepared", task_id, {"attempt_id": attempt_id})
            self.store.save(ledger)
        parsed = task_from_spec(spec)
        result = self.dispatch(parsed, wait_seconds=self.wait_seconds)
        with self._ledger_lock:
            provider_status = result.get("execution_status", result.get("status", "UNKNOWN"))
            if provider_status in {"UNKNOWN", "SUBMITTED", "RUNNING"}:
                # The queue owns the task after dispatch.  Preserve the
                # prepared attempt and let the next recovery pass reconcile
                # this exact provider task ID; never submit a second copy.
                attempt.update({
                    "worker_id": result.get("queue_job_id") or result.get("task_id") or task_id,
                    "provider": result.get("provider"),
                    "provider_status": provider_status,
                    "state": "RUNNING",
                    "last_reconciled_at": _now(),
                    "summary": _safe_text(result.get("summary"), 1200),
                    "evidence": result.get("evidence", []) if isinstance(result.get("evidence"), list) else [],
                    "errors": result.get("errors", []) if isinstance(result.get("errors"), list) else [],
                })
                task["state"] = "RUNNING"
                task["result"] = {
                    "status": "RUNNING", "provider_status": provider_status,
                    "summary": attempt.get("summary"), "evidence": attempt.get("evidence", []),
                }
                task["error"] = "provider task remains active; recovery will reconcile the same task ID"
                self._event(ledger, "task_waiting_for_provider", task_id, {
                    "attempt_id": attempt_id, "provider_status": provider_status,
                    "provider": attempt.get("provider"), "worker_id": attempt.get("worker_id"),
                })
                return
            validation = _validate_worker_result(task, result)
            unchanged = _unchanged_artifacts(task, attempt.get("artifact_snapshot"),
                                             validation.get("present", []))
            if unchanged and validation.get("accepted"):
                validation.update({
                    "accepted": False,
                    "classification": "NO-OP",
                    "reason": f"worker completed without changing required artifact: {', '.join(unchanged)}",
                    "unchanged": unchanged,
                })
            accepted_state = "COMPLETED" if validation.get("accepted") else validation.get("classification", "FAILED")
            attempt.update({"worker_id": result.get("queue_job_id") or result.get("task_id") or task_id,
                            "provider": result.get("provider"), "provider_status": provider_status,
                            "state": accepted_state,
                            "finished_at": _now(), "summary": _safe_text(result.get("summary"), 1200),
                            "evidence": result.get("evidence", []) if isinstance(result.get("evidence"), list) else [],
                            "acceptance": validation,
                            "errors": result.get("errors", []) if isinstance(result.get("errors"), list) else []})
            task["state"] = accepted_state
            task["result"] = {"status": accepted_state, "provider_status": provider_status,
                              "summary": attempt.get("summary"), "evidence": attempt.get("evidence", []),
                              "acceptance": validation}
            task["error"] = None if accepted_state == "COMPLETED" else _safe_text(
                validation.get("reason") or ((attempt.get("errors") or [{"message": "worker failed"}])[0].get("message")),
                500)
            self._event(ledger, "task_finished", task_id, {"attempt_id": attempt_id,
                                                            "provider_status": provider_status,
                                                            "status": task["state"]})

    def _event(self, ledger: dict[str, Any], event: str, task_id: str | None = None,
               payload: dict[str, Any] | None = None) -> None:
        ledger.setdefault("events", []).append({"at": _now(), "event": event, "task_id": task_id, "payload": payload or {}})
        ledger["updated_at"] = _now()
