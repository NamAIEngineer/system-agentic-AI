"""Provider-neutral discovery, adapter contract, and Codex host sync."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


WORKBENCH_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = Path(__file__).with_name("registry.json")
ORCA_ACCOUNTS_ROOT = Path.home() / "Library" / "Application Support" / "orca" / "codex-accounts"
QWEN_MODEL_CACHE = Path.home() / ".cache" / "system-agentic-ai" / "Qwen3-8B-4bit"
QWEN_MODELS_URL = "http://127.0.0.1:8767/v1/models"
QUEUE_LOGS_ROOT = Path.home() / "Library" / "Application Support" / "SystemAgenticAI" / "runtime" / "logs"
QUEUE_DB_PATH = Path.home() / "Library" / "Application Support" / "SystemAgenticAI" / "runtime" / "jobs.sqlite3"
PROVIDER_PREFLIGHT_ATTEMPTS = 3
PROVIDER_PREFLIGHT_DELAY_SECONDS = 1.0


class ProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderTask:
    workspace: Path
    objective: str
    role: str = "worker"
    required_capabilities: tuple[str, ...] = ("task.submit",)
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    context: dict[str, Any] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    provider_policy: dict[str, Any] = field(default_factory=dict)
    title: str | None = None
    execution_policy: str = "READ_ONLY"
    execution_context: dict[str, Any] = field(default_factory=dict)

    def prompt(self) -> str:
        return "\n".join((
            f"Objective:\n{self.objective.strip()}",
            f"Role: {self.role}",
            f"Required capabilities: {', '.join(self.required_capabilities)}",
            "Context (JSON):\n" + json.dumps(self.context, ensure_ascii=False, sort_keys=True),
            "Constraints:\n" + ("\n".join(f"- {item}" for item in self.constraints) or "- None specified"),
            "Acceptance criteria:\n" + ("\n".join(f"- {item}" for item in self.acceptance_criteria) or "- Complete the objective and report evidence"),
            "Execution context (JSON):\n" + json.dumps({
                **self.execution_context,
                "execution_policy": self.execution_policy,
                "dispatch_depth": int(self.execution_context.get("dispatch_depth", 1)),
            }, ensure_ascii=False, sort_keys=True),
            "Execution boundary: You are the assigned worker. Execute this task directly. Do not invoke system-agentic dispatch, create a child task, or bypass the declared execution policy. Stop if the task requires a broader permission.",
            f"System task ID: {self.task_id}",
        ))


@dataclass(frozen=True)
class ProviderResult:
    task_id: str
    provider_id: str
    account_id: str | None
    status: str
    summary: str | None
    artifacts: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, str], ...]
    started_at: str | None
    finished_at: str | None
    queue_job_id: str
    role: str
    required_capabilities: tuple[str, ...]
    selection_reason: str
    duration_seconds: float | None = None
    execution_policy: str = "READ_ONLY"
    execution_context: dict[str, Any] = field(default_factory=dict)


# Kept as a source-level alias for callers using the foundation name.
ProviderReceipt = ProviderResult


@dataclass(frozen=True)
class ProviderStatus:
    provider_id: str
    name: str
    enabled: bool
    adapter_implemented: bool
    installed: bool
    authenticated: bool | None
    authentication_state: str
    available: bool
    healthy: bool | None
    execution_runtime_ready: bool
    health_signal: str
    roles: tuple[str, ...]
    capabilities: tuple[str, ...]
    model: str | None
    fallback_priority: int
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(Protocol):
    provider_id: str

    def detect(self) -> ProviderStatus: ...

    def health(self) -> dict[str, Any]: ...

    def capabilities(self) -> tuple[str, ...]: ...

    def execute(self, task: ProviderTask, *, wait_timeout_seconds: float = 600) -> ProviderResult: ...

    def status(self, task: ProviderTask, *, selection_reason: str = "reconciled by task ID") -> ProviderResult: ...

    def normalize(self, result: object, task: ProviderTask, account_id: str | None, selection_reason: str) -> ProviderResult: ...


def load_registry() -> dict[str, Any]:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if registry.get("schema_version") != 1 or not isinstance(registry.get("providers"), list):
        raise ValueError("Unsupported provider registry schema.")
    ids = [item.get("id") for item in registry["providers"] if isinstance(item, dict)]
    if len(ids) != len(registry["providers"]) or len(ids) != len(set(ids)):
        raise ValueError("Provider registry entries must have unique IDs.")
    return registry


def _find_executable(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())
    candidates = [Path.home() / ".local" / "bin" / name, Path("/opt/homebrew/bin") / name]
    return next((str(path.resolve()) for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def _codex_login(binary: str, home: Path) -> bool:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    try:
        result = subprocess.run(
            [binary, "login", "status"], env=env, capture_output=True, text=True,
            timeout=10, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _runtime_service_info() -> tuple[bool, str | None]:
    """Read queue LaunchAgent state without returning its raw environment or logs."""
    entrypoint = WORKBENCH_ROOT / "scripts/agentic-runtime"
    if not entrypoint.is_file() or not os.access(entrypoint, os.X_OK):
        return False, None
    try:
        result = subprocess.run(
            [str(entrypoint), "service", "status"], cwd=WORKBENCH_ROOT,
            capture_output=True, text=True, timeout=8, check=False,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False, None
    launchctl = payload.get("launchctl", "") if isinstance(payload, dict) else ""
    match = re.search(r"^\s*CODEX_HOME\s*=>\s*(.+?)\s*$", launchctl, flags=re.MULTILINE)
    return bool(payload.get("loaded")), match.group(1).strip() if match else None


def discover_codex_homes() -> list[dict[str, Any]]:
    """Discover existing Codex homes; account IDs are read from Orca at runtime."""
    candidates: list[tuple[Path, str, str | None]] = []
    active_value = os.environ.get("CODEX_HOME")
    if active_value:
        candidates.append((Path(active_value).expanduser(), "active", None))
    candidates.append((Path.home() / ".codex", "standalone", None))
    _worker_ready, worker_home = _runtime_service_info()
    if worker_home:
        candidates.append((Path(worker_home).expanduser(), "runtime", None))

    if ORCA_ACCOUNTS_ROOT.is_dir():
        root = ORCA_ACCOUNTS_ROOT.resolve()
        for account_dir in sorted(ORCA_ACCOUNTS_ROOT.iterdir()):
            account_home = account_dir / "home"
            try:
                resolved = account_home.resolve(strict=True)
                resolved.relative_to(root)
                account_id = (resolved / ".orca-managed-home").read_text(encoding="utf-8").strip()
            except (OSError, ValueError):
                continue
            if account_id != account_dir.name or not resolved.is_dir():
                continue
            candidates.append((resolved, "orca", account_id))

    discovered: dict[str, dict[str, Any]] = {}
    active_path = Path(active_value).expanduser().resolve(strict=False) if active_value else None
    for path, source, account_id in candidates:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        key = str(resolved)
        entry = discovered.setdefault(key, {
            "home_path": key,
            "sources": [],
            "account_id": account_id,
        })
        if source not in entry["sources"]:
            entry["sources"].append(source)
        if account_id is not None:
            entry["account_id"] = account_id
        entry["active"] = resolved == active_path

    binary = _find_executable("codex")
    result = []
    for key, entry in sorted(discovered.items()):
        auth = _codex_login(binary, Path(key)) if binary else False
        result.append({
            **entry,
            "provider_id": "codex",
            "installed": binary is not None,
            "authenticated": auth if binary else None,
            "available": bool(binary and auth),
            "healthy": bool(binary and auth),
            "health_signal": "codex login status" if binary else "codex executable not found",
        })
    return result


def _qwen_probe() -> tuple[bool, bool, list[str], str]:
    try:
        request = urllib.request.Request(QWEN_MODELS_URL, method="GET")
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read(256_000))
        models = [
            item["id"] for item in payload.get("data", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        return bool(models), True, models, "loopback /v1/models"
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        installed = QWEN_MODEL_CACHE.is_dir() and any(QWEN_MODEL_CACHE.iterdir())
        return installed, False, [], "model cache present; loopback endpoint unavailable" if installed else "model service and cache unavailable"


def discover_providers() -> list[ProviderStatus]:
    registry = load_registry()
    statuses: list[ProviderStatus] = []
    codex_binary = _find_executable("codex")
    active_home_value = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    active_home = Path(active_home_value).expanduser()
    runtime_ready, worker_home = _runtime_service_info()
    codex_home = Path(worker_home).expanduser() if worker_home else active_home
    codex_authenticated = _codex_login(codex_binary, codex_home) if codex_binary else None

    for item in registry["providers"]:
        provider_id = item["id"]
        adapter = item["adapter"]
        installed = False
        authenticated: bool | None = None
        auth_state = "unknown"
        available = False
        healthy: bool | None = None
        execution_runtime_ready = False
        health_signal = "not checked"
        model = item.get("model")
        adapter_implemented = adapter in {"codex-runtime-queue", "local-runtime-queue", "rea-mcp-local"}
        metadata: dict[str, Any] = {}
        reason: str | None = None

        if adapter == "codex-runtime-queue":
            installed = codex_binary is not None
            authenticated = codex_authenticated
            auth_state = "authenticated" if authenticated else "unauthenticated" if installed else "unknown"
            execution_runtime_ready = runtime_ready
            available = bool(installed and authenticated and execution_runtime_ready)
            healthy = available
            health_signal = "Codex login status plus queue LaunchAgent; does not test quota or model inference"
            if not installed:
                reason = "Codex CLI not installed"
            elif not authenticated:
                reason = "Queue CODEX_HOME is not authenticated"
            elif not execution_runtime_ready:
                reason = "Codex task queue worker is not running"
        elif adapter == "local-runtime-queue":
            installed, endpoint_ready, models, health_signal = _qwen_probe()
            authenticated = None
            auth_state = "not_required"
            execution_runtime_ready = runtime_ready
            available = bool(endpoint_ready and execution_runtime_ready)
            healthy = endpoint_ready
            model = models[0] if models else None
            if not installed:
                reason = "Local Qwen model is not installed"
            elif not endpoint_ready:
                reason = "Local Qwen model service is not running"
            elif not execution_runtime_ready:
                reason = "Codex task queue worker is not running"
        elif adapter == "unavailable-cli-placeholder":
            executable_name = "claude" if provider_id == "claude" else "gemini"
            installed = _find_executable(executable_name) is not None
            authenticated = None
            auth_state = "unknown" if installed else "not_checked"
            available = False
            healthy = None
            execution_runtime_ready = False
            health_signal = "authentication and execution adapter not implemented"
            reason = "Provider is disabled; no authenticated execution adapter is configured"
        elif adapter == "rea-mcp-local":
            try:
                from modules.rea_adapter import rea_health
                health = rea_health()
                metadata = health
                installed = health.get("location") is not None and Path(str(health.get("location"))).is_dir()
                authenticated = None
                auth_state = "not_required"
                execution_runtime_ready = health.get("mcp_status") == "READY"
                available = execution_runtime_ready
                healthy = execution_runtime_ready
                model = f"rea-agents@{health.get('version', item.get('version', 'unknown'))}"
                health_signal = "REA stdio MCP initialize + tools/list"
                if not available:
                    reason = "; ".join(str(error) for error in health.get("errors", [])) or "REA MCP is unavailable"
            except (OSError, RuntimeError, ValueError) as exc:
                health_signal = "REA MCP health probe failed"
                reason = f"REA health probe failed: {type(exc).__name__}"
                healthy = False
                available = False
                execution_runtime_ready = False
        statuses.append(ProviderStatus(
            provider_id=provider_id,
            name=item["name"],
            enabled=bool(item["enabled"]),
            adapter_implemented=adapter_implemented,
            installed=installed,
            authenticated=authenticated,
            authentication_state=auth_state,
            available=available,
            healthy=healthy,
            execution_runtime_ready=execution_runtime_ready,
            health_signal=health_signal,
            roles=tuple(item.get("roles", [])),
            capabilities=tuple(item.get("capabilities", [])) if adapter_implemented else (),
            model=model,
            fallback_priority=int(item.get("fallback_priority", 100)),
            reason=reason,
            metadata=metadata,
        ))
    return sorted(statuses, key=lambda item: item.fallback_priority)


def eligible_providers(
    role: str,
    required_capabilities: set[str] | None = None,
    statuses: list[ProviderStatus] | None = None,
) -> list[ProviderStatus]:
    """Return candidates only; never dispatch or choose an automatic fallback."""
    required = required_capabilities or set()
    statuses = statuses if statuses is not None else discover_providers()
    return [
        item for item in statuses
        if item.enabled and item.adapter_implemented and item.available and item.healthy is True
        and role in item.roles and required.issubset(item.capabilities)
    ]


class QueueProviderAdapter:
    def __init__(self, provider_id: str, backend: str):
        self.provider_id = provider_id
        self.backend = backend

    def detect(self) -> ProviderStatus:
        return next(item for item in discover_providers() if item.provider_id == self.provider_id)

    def health(self) -> dict[str, Any]:
        status = self.detect()
        return {"healthy": status.healthy, "signal": status.health_signal, "reason": status.reason}

    def capabilities(self) -> tuple[str, ...]:
        status = self.detect()
        return status.capabilities if status.adapter_implemented else ()

    def _account_id(self) -> str | None:
        if self.provider_id != "codex":
            return None
        _ready, worker_home = _runtime_service_info()
        if not worker_home:
            return None
        resolved = str(Path(worker_home).expanduser().resolve(strict=False))
        account = next((item for item in discover_codex_homes() if item["home_path"] == resolved), None)
        return account.get("account_id") if account else "standalone-codex-home"

    def execute(self, task: ProviderTask, *, wait_timeout_seconds: float = 600) -> ProviderResult:
        status = self.detect()
        if not status.enabled or not status.available or status.healthy is not True:
            raise ProviderUnavailable(status.reason or f"{self.provider_id} is unavailable")
        if task.role not in status.roles or not set(task.required_capabilities).issubset(status.capabilities):
            raise ProviderUnavailable("Provider does not implement the requested role/capabilities.")
        if task.execution_policy not in {"READ_ONLY", "WORKSPACE_WRITE"}:
            raise ProviderUnavailable("ELEVATED_REQUIRES_APPROVAL: the queue cannot grant elevated access.")
        context = task.execution_context
        if context.get("host_role", "orchestrator") != "orchestrator" or context.get("direct_or_dispatch", "DISPATCH") != "DISPATCH":
            raise ValueError("Provider task must originate from a host dispatch decision.")
        if type(context.get("dispatch_depth", 1)) is not int or context.get("dispatch_depth", 1) != 1:
            raise ValueError("Only host-to-worker dispatch at depth 1 is supported.")
        workspace = task.workspace.expanduser().resolve(strict=True)
        if not workspace.is_dir() or not task.objective.strip():
            raise ValueError("Provider task requires an existing workspace and non-empty objective.")
        if not task.task_id or len(task.task_id) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in task.task_id):
            raise ValueError("Task ID must be 1-128 letters, digits, dot, underscore, or hyphen.")
            raise ValueError("Task ID must be 1-128 letters, digits, dot, underscore, or hyphen.")
        started = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        selection_reason = "lowest registry fallback_priority among live eligible candidates; no execution-time fallback"
        command = [
            str(WORKBENCH_ROOT / "scripts/agentic-runtime"), "submit",
            "--workspace", str(workspace), "--backend", self.backend, "--task-id", task.task_id,
        ]
        if task.title:
            command.extend(["--title", task.title])
        command.extend(["--execution-policy", task.execution_policy])
        command.extend(["--execution-context", json.dumps(context, ensure_ascii=False, sort_keys=True)])
        command.append(task.prompt())
        try:
            result = subprocess.run(command, cwd=WORKBENCH_ROOT, capture_output=True, text=True, timeout=15, check=False)
        except subprocess.TimeoutExpired:
            return self._unknown(task, self._account_id(), selection_reason, started, "Queue submission receipt timed out; reconcile by task ID; do not retry.")
        except OSError:
            return self._unknown(task, self._account_id(), selection_reason, started, "Queue submission could not be confirmed; reconcile by task ID; do not retry.")
        if result.returncode != 0:
            # The queue may have committed before the CLI failed. Never enqueue a second copy.
            current = self.status(task, selection_reason=selection_reason)
            if current.status != "UNKNOWN":
                return self._wait(task, self._account_id(), selection_reason, started, wait_timeout_seconds, current)
            return current
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return self._unknown(task, self._account_id(), selection_reason, started, "Queue returned an unreadable receipt; reconcile by task ID; do not retry.")
        if not isinstance(payload, dict) or payload.get("id") != task.task_id or payload.get("backend") != self.backend or payload.get("status") != "queued":
            return self._unknown(task, self._account_id(), selection_reason, started, "Queue receipt did not match this task; reconcile by task ID; do not retry.")
        initial = self.normalize(payload, task, self._account_id(), selection_reason)
        return self._wait(task, self._account_id(), selection_reason, started, wait_timeout_seconds, initial)

    def _unknown(self, task: ProviderTask, account_id: str | None, selection_reason: str, started: str | None, message: str) -> ProviderResult:
        return ProviderResult(task.task_id, self.provider_id, account_id, "UNKNOWN", None, (), (), ({"code": "QUEUE_RECEIPT_AMBIGUOUS", "message": message},), started, None, task.task_id, task.role, task.required_capabilities, selection_reason, execution_policy=task.execution_policy, execution_context=task.execution_context)

    def _queue_status(self, task_id: str) -> dict[str, Any] | None:
        try:
            uri = f"file:{QUEUE_DB_PATH}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (task_id,)).fetchone()
            if row is None:
                conn.close()
                return {"id": task_id, "status": "missing", "backend": None}
            payload = dict(row)
            try:
                payload["execution_context"] = json.loads(payload.get("execution_context_json") or "{}")
            except json.JSONDecodeError:
                payload["execution_context"] = {}
            payload["attempts"] = [dict(item) for item in conn.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY number", (task_id,))]
            conn.close()
            return payload
        except (OSError, sqlite3.Error):
            return None

    @staticmethod
    def _summary_from_attempts(attempts: list[Any]) -> str | None:
        """Extract only a provider's final answer from queue-owned JSONL logs."""
        for attempt in reversed(attempts):
            if not isinstance(attempt, dict) or not isinstance(attempt.get("log_path"), str):
                continue
            try:
                path = Path(attempt["log_path"]).resolve(strict=True)
                path.relative_to(QUEUE_LOGS_ROOT.resolve(strict=True))
                if not path.is_file() or path.stat().st_size > 5_000_000:
                    continue
                events = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except (OSError, ValueError):
                continue
            for line in reversed(events):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("runtime") == "local_task_complete" and isinstance(event.get("answer"), str):
                    return event["answer"][-20_000:]
                item = event.get("item", {}) if isinstance(event, dict) else {}
                if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    return item["text"][-20_000:]
        return None

    def _wait(self, task: ProviderTask, account_id: str | None, selection_reason: str, started: str, timeout: float, current: ProviderResult) -> ProviderResult:
        deadline = time.monotonic() + max(0, timeout)
        while current.status in {"SUBMITTED", "RUNNING"}:
            if time.monotonic() >= deadline:
                return ProviderResult(task.task_id, self.provider_id, account_id, "UNKNOWN", None, (), current.evidence, ({"code": "EXECUTION_STATUS_TIMEOUT", "message": "Task may still be running; reconcile by task ID; do not retry."},), current.started_at or started, None, task.task_id, task.role, task.required_capabilities, selection_reason, execution_policy=task.execution_policy, execution_context=task.execution_context)
            time.sleep(1)
            current = self.status(task, selection_reason=selection_reason, started_at=started)
            if current.status == "UNKNOWN":
                continue
        return current

    def status(self, task: ProviderTask, *, selection_reason: str = "reconciled by task ID", started_at: str | None = None) -> ProviderResult:
        account_id = self._account_id()
        row = self._queue_status(task.task_id)
        # The queue CLI is a read-only fallback for a transient SQLite read
        # miss (and keeps older queue-worker deployments observable). It never
        # submits or resumes work.
        if isinstance(row, dict) and row.get("status") == "missing":
            try:
                probe = subprocess.run(
                    [str(WORKBENCH_ROOT / "scripts/agentic-runtime"), "status", task.task_id, "--json"],
                    cwd=WORKBENCH_ROOT, capture_output=True, text=True, timeout=8, check=False,
                )
                if probe.returncode == 0:
                    payload = json.loads(probe.stdout)
                    if isinstance(payload, dict) and payload.get("id") == task.task_id:
                        row = payload
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
                pass
        if row is None:
            return self._unknown(task, account_id, selection_reason, started_at, "Queue status is not currently available; reconcile later; do not retry.")
        return self.normalize(row, task, account_id, selection_reason)

    def normalize(self, result: object, task: ProviderTask, account_id: str | None, selection_reason: str) -> ProviderResult:
        # A durable lookup miss is a known state: the queue never accepted
        # this submission (or its record was removed).  Keep it distinct from
        # an unreadable/ambiguous backend response so recovery may make one
        # idempotent submission decision using the same task identity.
        if isinstance(result, dict) and result.get("status") == "missing" and result.get("id") == task.task_id:
            return ProviderResult(task.task_id, self.provider_id, account_id, "MISSING", None, (),
                                  ({"type": "queue", "job_id": task.task_id, "backend": self.backend},),
                                  ({"code": "QUEUE_MISSING", "message": "No durable queue record exists for this task identity."},),
                                  None, None, task.task_id, task.role, task.required_capabilities,
                                  selection_reason, execution_policy=task.execution_policy,
                                  execution_context=task.execution_context)
        if not isinstance(result, dict) or result.get("id") != task.task_id or result.get("backend") != self.backend:
            return self._unknown(task, account_id, selection_reason, None, "Queue status does not match task identity; reconcile by task ID; do not retry.")
        raw_status = result.get("status")
        status_map = {"queued": "SUBMITTED", "running": "RUNNING", "completed": "COMPLETED", "failed": "FAILED", "cancelled": "FAILED", "interrupted": "FAILED_LOST", "missing": "MISSING"}
        normalized = status_map.get(raw_status, "UNKNOWN")
        if result.get("terminal_status") == "FAILED_LOST":
            normalized = "FAILED_LOST"
        if normalized == "COMPLETED" and not (result.get("result_summary") or self._summary_from_attempts(result.get("attempts", []))):
            normalized = "FAILED"
        attempts = result.get("attempts") if isinstance(result.get("attempts"), list) else []
        evidence = ({"type": "queue", "job_id": task.task_id, "backend": self.backend, "attempt_count": len(attempts)},)
        errors: tuple[dict[str, str], ...] = ()
        if normalized in {"FAILED", "FAILED_LOST", "UNKNOWN"}:
            errors = ({"code": "QUEUE_" + str(raw_status or "UNKNOWN").upper(), "message": "Queue did not report successful task completion."},)
        started_at = result.get("started_at") if isinstance(result.get("started_at"), str) else None
        finished_at = result.get("finished_at") if isinstance(result.get("finished_at"), str) else None
        duration = None
        if started_at and finished_at:
            try:
                duration = max(0.0, (datetime.fromisoformat(finished_at.replace("Z", "+00:00")) - datetime.fromisoformat(started_at.replace("Z", "+00:00"))).total_seconds())
            except ValueError:
                duration = None
        summary = result.get("result_summary") if isinstance(result.get("result_summary"), str) else None
        if summary is None:
            summary = self._summary_from_attempts(attempts)
        context = result.get("execution_context") if isinstance(result.get("execution_context"), dict) else task.execution_context
        policy = result.get("execution_policy") if result.get("execution_policy") in {"READ_ONLY", "WORKSPACE_WRITE", "ELEVATED", "LEGACY_UNRESTRICTED"} else task.execution_policy
        return ProviderResult(task.task_id, self.provider_id, account_id, normalized, summary, (), evidence, errors, started_at, finished_at, task.task_id, task.role, task.required_capabilities, selection_reason, duration, policy, context)


class UnavailableProviderAdapter:
    def __init__(self, provider_id: str):
        self.provider_id = provider_id

    def detect(self) -> ProviderStatus:
        return next(item for item in discover_providers() if item.provider_id == self.provider_id)

    def health(self) -> dict[str, Any]:
        status = self.detect()
        return {"healthy": status.healthy, "signal": status.health_signal, "reason": status.reason}

    def capabilities(self) -> tuple[str, ...]:
        return ()

    def execute(self, task: ProviderTask, *, wait_timeout_seconds: float = 600) -> ProviderResult:
        raise ProviderUnavailable(f"{self.provider_id} has no configured execution adapter")

    def status(self, task: ProviderTask, *, selection_reason: str = "reconciled by task ID") -> ProviderResult:
        raise ProviderUnavailable(f"{self.provider_id} has no configured status adapter")

    def normalize(self, result: object, task: ProviderTask, account_id: str | None, selection_reason: str) -> ProviderResult:
        raise ProviderUnavailable(f"{self.provider_id} has no configured result normalizer")


@dataclass(frozen=True)
class ProviderSelection:
    provider_id: str
    account_id: str | None
    reason: str
    candidates: tuple[str, ...]


def select_provider(task: ProviderTask, statuses: list[ProviderStatus] | None = None) -> tuple[ProviderAdapter, ProviderSelection]:
    allowed = task.provider_policy.get("allowed_providers")
    if allowed is not None:
        if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
            raise ValueError("provider_policy.allowed_providers must be a list of provider IDs.")
    live_discovery = statuses is None
    retry_local_qwen = live_discovery and (allowed is None or "local-qwen" in allowed)
    attempts = 0
    last_statuses: list[ProviderStatus] = []
    candidates: list[ProviderStatus] = []
    while attempts < (PROVIDER_PREFLIGHT_ATTEMPTS if retry_local_qwen else 1):
        attempts += 1
        last_statuses = discover_providers() if live_discovery else list(statuses or [])
        candidates = eligible_providers(task.role, set(task.required_capabilities), last_statuses)
        if allowed is not None:
            candidates = [item for item in candidates if item.provider_id in allowed]
        if candidates or not retry_local_qwen or attempts >= PROVIDER_PREFLIGHT_ATTEMPTS:
            break
        time.sleep(PROVIDER_PREFLIGHT_DELAY_SECONDS)
    if not candidates:
        details = "; ".join(
            f"{item.provider_id}={item.reason or ('healthy' if item.healthy else 'unhealthy')}"
            for item in last_statuses
            if item.provider_id in (allowed or {"local-qwen"})
        )
        suffix = f" preflight_attempts={attempts}" + (f" providers={details[:600]}" if details else "")
        raise ProviderUnavailable("NO_PROVIDER: no enabled, healthy registry candidate matches the requested role/capabilities/policy." + suffix)
    if task.provider_policy.get("selection", "registry-priority") != "registry-priority":
        raise ValueError("Unsupported provider selection policy.")
    selected = min(candidates, key=lambda item: item.fallback_priority)
    reason = f"selected {selected.provider_id}: eligible by registry; lowest fallback_priority among {len(candidates)} candidate(s); no execution-time failover"
    account_id = None
    if selected.provider_id == "codex":
        _ready, worker_home = _runtime_service_info()
        if worker_home:
            resolved = str(Path(worker_home).expanduser().resolve(strict=False))
            account = next((item for item in discover_codex_homes() if item["home_path"] == resolved), None)
            account_id = account.get("account_id") if account else "standalone-codex-home"
    return adapter_for(selected.provider_id), ProviderSelection(selected.provider_id, account_id, reason, tuple(item.provider_id for item in candidates))


def adapter_for(provider_id: str) -> ProviderAdapter:
    items = {item["id"]: item for item in load_registry()["providers"]}
    item = items.get(provider_id)
    if item is None:
        raise ValueError(f"Unknown provider: {provider_id}")
    if item["adapter"] == "codex-runtime-queue":
        return QueueProviderAdapter(provider_id, "codex")
    if item["adapter"] == "local-runtime-queue":
        return QueueProviderAdapter(provider_id, "local")
    if item["adapter"] == "rea-mcp-local":
        from modules.rea_adapter import ReaMcpProviderAdapter
        return ReaMcpProviderAdapter()
    return UnavailableProviderAdapter(provider_id)


def provider_inventory() -> dict[str, Any]:
    statuses = discover_providers()
    registry = load_registry()
    runtime_ready, runtime_home = _runtime_service_info()
    return {
        "schema_version": 1,
        "provider_neutral_core": str(WORKBENCH_ROOT / "agent-brain" / "operating-contract.md"),
        "providers": [asdict(status) | {"roles": list(status.roles), "capabilities": list(status.capabilities)} for status in statuses],
        "codex_accounts": discover_codex_homes(),
        "runtime": {"queue_worker_loaded": runtime_ready, "codex_home": runtime_home},
        "routing": {
            "automatic_dispatch": "instruction-driven host; no prompt interception daemon",
            "queue_dispatch": "explicit provider CLI invoked by the host",
            "automatic_quota_failover": registry["account_policy"]["automatic_failover"],
            "candidate_query": "eligible_providers(role, required_capabilities)",
        },
    }


def _tree_matches(source: Path, target: Path) -> bool:
    if not source.is_dir() or not target.is_dir():
        return False
    source_items = sorted(path.relative_to(source) for path in source.rglob("*"))
    target_items = sorted(path.relative_to(target) for path in target.rglob("*"))
    if source_items != target_items:
        return False
    for relative in source_items:
        left, right = source / relative, target / relative
        if left.is_symlink() or right.is_symlink():
            if not left.is_symlink() or not right.is_symlink() or os.readlink(left) != os.readlink(right):
                return False
        elif left.is_dir() != right.is_dir() or left.is_file() != right.is_file():
            return False
        elif left.is_file() and left.read_bytes() != right.read_bytes():
            return False
    return True


def _legacy_system_brain_copy(target: Path) -> bool:
    expected = {
        Path("SKILL.md"),
        Path("agents/openai.yaml"),
        Path("references/system-knowledge.md"),
    }
    if not target.is_dir() or target.is_symlink():
        return False
    actual = {path.relative_to(target) for path in target.rglob("*") if path.is_file() or path.is_symlink()}
    directories = {path.relative_to(target) for path in target.rglob("*") if path.is_dir() and not path.is_symlink()}
    return actual == expected and directories == {Path("agents"), Path("references")}


def _ensure_skill_link(source: Path, target: Path, adopt_old_brain_copy: bool = False) -> str:
    if target.is_symlink():
        try:
            if target.resolve(strict=True) == source.resolve(strict=True):
                return "already-linked"
        except OSError:
            pass
        return "conflict"
    try:
        if target.resolve(strict=True) == source.resolve(strict=True):
            return "source"
    except OSError:
        pass
    if target.exists():
        if not _tree_matches(source, target) and not (adopt_old_brain_copy and _legacy_system_brain_copy(target)):
            return "conflict"
        backup = target.with_name(f".{target.name}.system-agentic-sync-backup")
        if backup.exists() or backup.is_symlink():
            return "conflict"
        target.rename(backup)
        try:
            target.symlink_to(source, target_is_directory=True)
        except OSError:
            backup.rename(target)
            raise
        shutil.rmtree(backup)
        return "linked-managed-copy"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source, target_is_directory=True)
    return "linked"


def _instruction_block() -> str:
    begin, end = load_registry()["codex_sync"]["instruction_markers"]
    brain_root = WORKBENCH_ROOT / "agent-brain"
    return f"""{begin}
## System Agentic AI — shared provider-neutral brain

For software, automation, and business-system tasks, read `{brain_root}/operating-contract.md` and load the `system-agentic-ai` skill from this Codex home. Provider and role policy is in `{brain_root}/providers/registry.json`; check live availability before selecting an adapter. Keep shared brain sources outside provider account homes and do not silently route task data to an unconfigured provider.

Before choosing a route or specialized module, query `{WORKBENCH_ROOT}/scripts/system-agentic decision "<prompt>"`. Quyet Tiny provides advisory DIRECT/DISPATCH, module, and execution-class choices. Low-confidence or unavailable results return to host reasoning. TypeSafe JEV escalation requires `--allow-typesafe` and explicit user authorization for that specific non-sensitive prompt; never send private prompts, workspace files, or source code. Decision output cannot grant ELEVATED access or bypass approval, registries, recursion guards, or sandbox policy. Trivial conversation may remain DIRECT without classification.

When a user asks to inspect workspace/filesystem state, change code/files, run QA, automate work, or produce an execution artifact, frame the request into the existing provider-neutral task contract and use `{WORKBENCH_ROOT}/scripts/system-agentic dispatch` without asking the user to write a task spec. Use only capability IDs exposed in the registry/live adapter; for general queue execution use `task.submit`, and do not invent capabilities such as `filesystem.read` because data access is controlled by `execution_policy`. Choose `READ_ONLY` for inspection/review and `WORKSPACE_WRITE` only for changes requested within the target workspace. Keep simple explanations, ordinary conversation, status questions, and text-only transformations direct on the host; do not create a queue task for them. Set execution context to host `orchestrator`, `dispatch_depth: 1`, `direct_or_dispatch: DISPATCH`, and identify the request as coming from the interactive host. Do not shorten dispatch's default 600-second wait. If the result is `UNKNOWN`, `SUBMITTED`, or `RUNNING`, reconcile that same task ID using `scripts/system-agentic status <id> --wait-seconds 600`; this polls without resubmitting. Do not leave background work or create another task to wait. Return the normalized result to the user. Never dispatch elevated, external-write, publishing, deployment, purchase, messaging, credential, or machine-wide tasks automatically. A worker (`SYSTEM_AGENTIC_EXECUTION_ROLE=worker`) must execute its assigned task directly and never dispatch a child task. Reconcile `UNKNOWN` by its existing task ID; never retry automatically.

For output-heavy workspace reads, logs, test output, or bounded commands, prefer the `system-agentic-tools` MCP server when available so the shared Tool Runtime can enforce its fixed policy and optimize output before it enters host context. The server defaults to `READ_ONLY`; only read-only MCP tools are approval-free. Keep native tools for small/interactive/unsupported operations. Do not pass secrets to the gateway, request ELEVATED, or treat a compact output as a substitute for its recovery reference.
{end}"""


def _update_agents_file(path: Path) -> str:
    begin, end = load_registry()["codex_sync"]["instruction_markers"]
    block = _instruction_block()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    has_begin, has_end = begin in existing, end in existing
    if has_begin != has_end:
        return "conflict"
    if has_begin:
        left, right = existing.index(begin), existing.index(end, existing.index(begin)) + len(end)
        updated = existing[:left] + block + existing[right:]
    else:
        updated = existing.rstrip() + ("\n\n" if existing.strip() else "") + block + "\n"
    if updated != existing:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
        return "updated"
    return "unchanged"


def _sync_tool_mcp(home: Path) -> str:
    """Register the shared stdio Tool Runtime in a discovered Codex home."""
    config = home / "config.toml"
    section = "[mcp_servers.system-agentic-tools]"
    command = str(Path(sys.executable).resolve())
    adapter = str(WORKBENCH_ROOT / "scripts/system-agentic-tool-mcp")
    expected_env = '{ "SYSTEM_AGENTIC_TOOL_EXECUTION_POLICY" = "READ_ONLY", "SYSTEM_AGENTIC_TOOL_CALLER" = "codex" }'
    legacy_env = '{ "SYSTEM_AGENTIC_TOOL_EXECUTION_POLICY" = "READ_ONLY" }'
    existing = config.read_text(encoding="utf-8") if config.exists() else ""
    lines = existing.splitlines()
    try:
        start = lines.index(section)
    except ValueError:
        stanza = (
            f'{section}\ncommand = {json.dumps(command)}\nargs = [{json.dumps(adapter)}]\n'
            f'env = {expected_env}\n'
            'default_tools_approval_mode = "writes"\n'
        )
        updated = existing.rstrip() + ("\n\n" if existing.strip() else "") + stanza
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(updated, encoding="utf-8")
        return "registered"
    end = next((index for index in range(start + 1, len(lines)) if re.match(r"^\s*\[[^\]]+\]\s*$", lines[index]) and lines[index].strip() != "[mcp_servers.system-agentic-tools.env]"), len(lines))
    # Codex accepts both an inline `env = {...}` value and the equivalent
    # `[mcp_servers.system-agentic-tools.env]` table. Treat both as one
    # registration so an existing valid config is adopted instead of reported
    # as a conflict. Never rewrite unrelated keys or server entries.
    env_section = "[mcp_servers.system-agentic-tools.env]"
    env_start = next((index for index in range(start + 1, end) if lines[index].strip() == env_section), None)
    env_end = next((index for index in range((env_start or end) + 1, len(lines)) if re.match(r"^\s*\[[^\]]+\]\s*$", lines[index]) and lines[index].strip() != env_section), end) if env_start is not None else end
    if env_start is not None:
        end = env_end
    values = {
        key.strip(): value.strip()
        for key, value in (line.split("=", 1) for line in lines[start + 1:end] if "=" in line and not line.lstrip().startswith("#"))
    }
    nested_env = {}
    if env_start is not None:
        nested_env = {
            key.strip(): value.strip()
            for key, value in (line.split("=", 1) for line in lines[env_start + 1:env_end] if "=" in line and not line.lstrip().startswith("#"))
        }
    command_ok = values.get("command") == json.dumps(command)
    args_ok = values.get("args") == f"[{json.dumps(adapter)}]"
    env_ok = values.get("env") in {expected_env, legacy_env} or (
        nested_env.get("SYSTEM_AGENTIC_TOOL_EXECUTION_POLICY") == '"READ_ONLY"'
        and nested_env.get("SYSTEM_AGENTIC_TOOL_CALLER") == '"codex"'
    )
    approval_ok = values.get("default_tools_approval_mode") in {None, '"writes"'}
    if command_ok and args_ok and env_ok:
        if not approval_ok:
            return "conflict"
        if (values.get("env") == expected_env or (env_start is not None and env_ok)) and values.get("default_tools_approval_mode") == '"writes"':
            return "already-registered"
        stanza_lines = lines[start:end]
        if values.get("env") == legacy_env:
            env_index = next(index for index, line in enumerate(stanza_lines) if line.lstrip().startswith("env"))
            stanza_lines[env_index] = f"env = {expected_env}"
        elif env_start is not None and nested_env.get("SYSTEM_AGENTIC_TOOL_CALLER") != '"codex"':
            caller_index = next((index for index, line in enumerate(stanza_lines) if line.strip() == env_section), None)
            if caller_index is not None:
                stanza_lines.insert(caller_index + 1, 'SYSTEM_AGENTIC_TOOL_CALLER = "codex"')
        if values.get("default_tools_approval_mode") is None:
            stanza_lines.append('default_tools_approval_mode = "writes"')
        updated_lines = lines[:start] + stanza_lines + lines[end:]
        config.write_text("\n".join(updated_lines) + ("\n" if existing.endswith("\n") else ""), encoding="utf-8")
        return "updated-tool-policy"
    return "conflict"


def sync_codex_provider() -> dict[str, Any]:
    registry = load_registry()
    accounts = discover_codex_homes()
    skills = registry["codex_sync"]["skills"]
    result: dict[str, Any] = {"provider_id": "codex", "accounts": [], "status": "complete"}
    for account in accounts:
        home = Path(account["home_path"])
        entries = []
        for skill in skills:
            source_value = skill["source"]
            source = (WORKBENCH_ROOT / source_value).resolve() if not source_value.startswith("~/") else (Path.home() / source_value[2:]).resolve()
            target = home / skill["target"]
            if not (source / "SKILL.md").is_file():
                state = "source-missing"
            else:
                state = _ensure_skill_link(
                    source,
                    target,
                    adopt_old_brain_copy=skill["id"] == "system-agentic-ai",
                )
            entries.append({"skill": skill["id"], "state": state, "target": str(target), "source": str(source)})
        skills_ok = all(entry["state"] not in {"conflict", "source-missing"} for entry in entries)
        instruction = _update_agents_file(home / "AGENTS.md") if skills_ok else "skipped"
        mcp = _sync_tool_mcp(home)
        account_result = {"home_path": str(home), "account_id": account.get("account_id"), "skills": entries, "agents_md": instruction, "tool_mcp": mcp}
        result["accounts"].append(account_result)
        if not skills_ok or instruction == "conflict" or mcp == "conflict":
            result["status"] = "partial"
    reference = WORKBENCH_ROOT / "agent-brain/skills/system-agentic-ai/references/system-knowledge.md"
    knowledge = WORKBENCH_ROOT / "agent-brain/knowledge.md"
    try:
        reference_state = _replace_file_with_link(knowledge, reference)
    except OSError:
        reference_state = "error"
        result["status"] = "partial"
    result["shared_knowledge_reference"] = {"path": str(reference), "source": str(knowledge), "state": reference_state}
    if reference_state == "conflict":
        result["status"] = "partial"
    return result


def _replace_file_with_link(source: Path, target: Path) -> str:
    source_resolved = source.resolve(strict=True)
    if target.is_symlink():
        try:
            return "already-linked" if target.resolve(strict=True) == source_resolved else "conflict"
        except OSError:
            return "conflict"
    if target.exists():
        if not target.is_file():
            return "conflict"
        # This file is the historical generated copy from install-codex-brain.sh.
        backup = target.with_name(f".{target.name}.system-agentic-sync-backup")
        if backup.exists() or backup.is_symlink():
            return "conflict"
        target.rename(backup)
        try:
            target.symlink_to(source_resolved)
        except OSError:
            backup.rename(target)
            raise
        backup.unlink()
        return "linked-generated-copy"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source_resolved)
    return "linked"


def sync_provider(provider_id: str) -> dict[str, Any]:
    registry = load_registry()
    configured = {item["id"]: item for item in registry["providers"]}
    if provider_id == "all":
        results = []
        for item in registry["providers"]:
            if item["id"] == "codex":
                results.append(sync_codex_provider())
            else:
                status = next(status for status in discover_providers() if status.provider_id == item["id"])
                results.append({
                    "provider_id": item["id"],
                    "status": "skipped",
                    "reason": status.reason or "No provider-home sync adapter is configured",
                })
        return {"status": "complete" if all(item["status"] in {"complete", "skipped"} for item in results) else "partial", "providers": results}
    if provider_id not in configured:
        raise ValueError(f"Unknown provider: {provider_id}")
    if provider_id != "codex":
        status = next(item for item in discover_providers() if item.provider_id == provider_id)
        return {"provider_id": provider_id, "status": "skipped", "reason": status.reason or "No provider-home sync adapter is configured"}
    return sync_codex_provider()
