"""Durable, bounded execution of registered module dependency graphs."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from modules.pipeline_contracts import PipelinePlan, PipelineStep, ProductArtifact
from modules.product_contracts import ContentPackage, ContentVariant, DistributionIntent, MediaArtifact, MediaVariant, ProductRun
from modules.capability_registry import CapabilityProviderRegistry, CapabilityResolutionError, ProviderSelectionPolicy
from modules.workspace_resolver import ModuleExecutionContext, ModuleRegistry, WorkspaceResolver


APP_SUPPORT = Path.home() / "Library" / "Application Support" / "SystemAgenticAI" / "runtime"
DEFAULT_PIPELINE_DB = APP_SUPPORT / "pipelines.sqlite3"
MAX_ARTIFACT_FILES = 100
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_CONCURRENCY = 16


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class PipelineRun:
    pipeline_id: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    failure_policy: str
    cancellation_requested: bool
    error: str | None = None
    product_id: str | None = None


@dataclass(frozen=True)
class PipelineStepRun:
    pipeline_id: str
    step_id: str
    module_id: str
    capability: str
    state: str
    execution_mode: str
    task_id: str
    started_at: str | None
    finished_at: str | None
    result: dict[str, Any] | None
    error: str | None
    artifact_ids: tuple[str, ...] = ()
    resolution_evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class WorkspaceLease:
    lease_id: str
    module_id: str
    workspace_path: str
    pipeline_id: str
    step_id: str
    execution_mode: str
    owner_pid: int
    acquired_at: str


class GlobalExecutionBudget:
    """One bounded step semaphore shared by every executor in this process."""

    def __init__(self, max_global_steps: int = 1) -> None:
        if type(max_global_steps) is not int or not 1 <= max_global_steps <= MAX_CONCURRENCY:
            raise ValueError(f"max_global_steps must be between 1 and {MAX_CONCURRENCY}.")
        self.max_global_steps = max_global_steps
        self._condition = threading.Condition()
        self._active = 0
        self._active_products: dict[str, int] = {}
        self._active_modules: dict[str, int] = {}

    def acquire(self, *, wait: bool = False, timeout: float | None = None,
                product_id: str | None = None, product_limit: int = MAX_CONCURRENCY,
                module_id: str | None = None, module_limit: int = MAX_CONCURRENCY) -> bool:
        if type(product_limit) is not int or not 1 <= product_limit <= MAX_CONCURRENCY:
            raise ValueError("product_limit is outside the supported concurrency range.")
        if type(module_limit) is not int or not 1 <= module_limit <= MAX_CONCURRENCY:
            raise ValueError("module_limit is outside the supported concurrency range.")
        with self._condition:
            if not wait:
                if not self._available(product_id, product_limit, module_id, module_limit):
                    return False
                self._increment(product_id, module_id)
                return True
            deadline = None if timeout is None else time.monotonic() + timeout
            while not self._available(product_id, product_limit, module_id, module_limit):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            self._increment(product_id, module_id)
            return True

    def _available(self, product_id: str | None, product_limit: int,
                   module_id: str | None, module_limit: int) -> bool:
        return (self._active < self.max_global_steps
                and (product_id is None or self._active_products.get(product_id, 0) < product_limit)
                and (module_id is None or self._active_modules.get(module_id, 0) < module_limit))

    def _increment(self, product_id: str | None, module_id: str | None) -> None:
        self._active += 1
        if product_id is not None:
            self._active_products[product_id] = self._active_products.get(product_id, 0) + 1
        if module_id is not None:
            self._active_modules[module_id] = self._active_modules.get(module_id, 0) + 1

    def release(self, *, product_id: str | None = None, module_id: str | None = None) -> None:
        with self._condition:
            if self._active <= 0:
                raise RuntimeError("Global execution budget release without a matching acquire.")
            for values, key in ((self._active_products, product_id), (self._active_modules, module_id)):
                if key is not None and values.get(key, 0) <= 0:
                    raise RuntimeError("Global execution budget scope release without a matching acquire.")
            self._active -= 1
            for values, key in ((self._active_products, product_id), (self._active_modules, module_id)):
                if key is not None:
                    count = values[key]
                    if count == 1:
                        values.pop(key)
                    else:
                        values[key] = count - 1
            self._condition.notify_all()

    def inspect(self) -> dict[str, Any]:
        with self._condition:
            return {"maxGlobalSteps": self.max_global_steps, "activeGlobalSteps": self._active,
                    "activeProductSteps": dict(self._active_products),
                    "activeModuleSteps": dict(self._active_modules)}

    def wait_for_change(self, timeout: float | None = None) -> None:
        with self._condition:
            self._condition.wait(timeout)


class ProcessRuntimeCoordinator:
    """Process-owned shared coordination for execution budgets and stores."""

    def __init__(self, global_budget: GlobalExecutionBudget | None = None) -> None:
        self.global_budget = global_budget or GlobalExecutionBudget()
        self._lock = threading.RLock()
        self._lease_manager: Any | None = None
        self._stores: dict[str, Any] = {}

    def pipeline_store(self, store: "PipelineStore | None" = None) -> "PipelineStore":
        path = str((store.path if store is not None else DEFAULT_PIPELINE_DB).expanduser().resolve(strict=False))
        with self._lock:
            if path not in self._stores:
                self._stores[path] = store or PipelineStore(path)
            return self._stores[path]

    def lease_manager(self, store: "PipelineStore") -> "WorkspaceLeaseManager":
        with self._lock:
            # Workspace exclusivity is process-scoped, not pipeline-store-scoped.
            # Keep all executor instances on one SQLite lease table even when
            # their run/artifact stores are intentionally separate.
            if self._lease_manager is None or not self._lease_manager.store.path.parent.exists():
                self._lease_manager = WorkspaceLeaseManager(store)
            return self._lease_manager


PROCESS_RUNTIME_COORDINATOR = ProcessRuntimeCoordinator()


class PipelineExecutionError(RuntimeError):
    pass


class ModuleExecutionAdapter(Protocol):
    """Adapter boundary: module work runs here; scheduler owns lifecycle and safety."""

    def health(self, module_id: str) -> dict[str, Any]: ...

    def execute(self, step: PipelineStep, context: dict[str, Any]) -> dict[str, Any]: ...

    def normalize_result(self, result: object, step: PipelineStep) -> dict[str, Any]: ...

    def read_only_safe(self, step: PipelineStep) -> bool: ...

    def artifact_workspace(self, step: PipelineStep, context: dict[str, Any]) -> Path: ...


class PipelineStore:
    """SQLite state store for runs, steps, artifacts, events, and cross-process leases."""

    def __init__(self, path: str | Path = DEFAULT_PIPELINE_DB) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    pipeline_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED')),
                    plan_json TEXT NOT NULL,
                    failure_policy TEXT NOT NULL,
                    cancellation_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS pipeline_steps (
                    pipeline_id TEXT NOT NULL REFERENCES pipeline_runs(pipeline_id),
                    step_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('WAITING_FOR_DEPENDENCIES','RUNNABLE','CLAIMED','RUNNING','COMPLETED','FAILED','SKIPPED','CANCELLED')),
                    execution_mode TEXT NOT NULL CHECK(execution_mode IN ('READ_ONLY','MUTATING')),
                    task_id TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result_json TEXT,
                    error TEXT,
                    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    resolution_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(pipeline_id, step_id),
                    UNIQUE(task_id)
                );
                CREATE TABLE IF NOT EXISTS pipeline_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    pipeline_id TEXT NOT NULL REFERENCES pipeline_runs(pipeline_id),
                    step_id TEXT NOT NULL,
                    artifact_kind TEXT NOT NULL DEFAULT 'ProductArtifact',
                    artifact_json TEXT NOT NULL,
                    file_manifest_json TEXT NOT NULL,
                    FOREIGN KEY(pipeline_id, step_id) REFERENCES pipeline_steps(pipeline_id, step_id)
                );
                CREATE TABLE IF NOT EXISTS artifact_store (
                    artifact_id TEXT PRIMARY KEY,
                    product_id TEXT,
                    pipeline_id TEXT NOT NULL,
                    producer_module_id TEXT NOT NULL,
                    producer_step_id TEXT NOT NULL,
                    artifact_kind TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    producer_workspace TEXT NOT NULL,
                    file_manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS artifacts_by_product ON artifact_store(product_id, created_at);
                CREATE TABLE IF NOT EXISTS pipeline_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pipeline_id TEXT NOT NULL REFERENCES pipeline_runs(pipeline_id),
                    step_id TEXT,
                    module_id TEXT,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pipeline_events_by_run ON pipeline_events(pipeline_id, event_id);
                CREATE TABLE IF NOT EXISTS workspace_leases (
                    lease_id TEXT PRIMARY KEY,
                    workspace_path TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    pipeline_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    execution_mode TEXT NOT NULL CHECK(execution_mode IN ('READ_ONLY','MUTATING')),
                    owner_pid INTEGER NOT NULL,
                    acquired_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS workspace_leases_by_root ON workspace_leases(workspace_path);
                CREATE TABLE IF NOT EXISTS product_runs (
                    product_id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED')),
                    product_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS product_pipeline_links (
                    product_id TEXT NOT NULL REFERENCES product_runs(product_id),
                    pipeline_id TEXT NOT NULL REFERENCES pipeline_runs(pipeline_id),
                    PRIMARY KEY(product_id,pipeline_id)
                );
                CREATE TABLE IF NOT EXISTS product_artifact_relations (
                    source_product_id TEXT NOT NULL REFERENCES product_runs(product_id),
                    target_product_id TEXT NOT NULL REFERENCES product_runs(product_id),
                    reason TEXT NOT NULL,
                    authorized_at TEXT NOT NULL,
                    PRIMARY KEY(source_product_id,target_product_id),
                    CHECK(source_product_id <> target_product_id)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(pipeline_steps)")}
            if "resolution_json" not in columns:
                db.execute("ALTER TABLE pipeline_steps ADD COLUMN resolution_json TEXT NOT NULL DEFAULT '{}'")
            artifact_columns = {row["name"] for row in db.execute("PRAGMA table_info(pipeline_artifacts)")}
            if "artifact_kind" not in artifact_columns:
                db.execute("ALTER TABLE pipeline_artifacts ADD COLUMN artifact_kind TEXT NOT NULL DEFAULT 'ProductArtifact'")
        os.chmod(self.path, 0o600)
        self.artifact_store = ArtifactStore(self)

    def _event(self, db: sqlite3.Connection, pipeline_id: str, event_type: str,
               step_id: str | None = None, module_id: str | None = None,
               payload: dict[str, Any] | None = None) -> None:
        db.execute("INSERT INTO pipeline_events(pipeline_id,step_id,module_id,event_type,timestamp,payload_json) VALUES(?,?,?,?,?,?)",
                   (pipeline_id, step_id, module_id, event_type, utc_now(), _json(payload or {})))

    def create_run(self, plan: PipelinePlan) -> None:
        stamp = utc_now()
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO pipeline_runs(pipeline_id,status,plan_json,failure_policy,created_at) VALUES(?,?,?,?,?)",
                           (plan.pipeline_id, "PENDING", _json(plan.to_dict()), plan.failure_policy, stamp))
                if plan.product_id:
                    product = db.execute("SELECT product_json FROM product_runs WHERE product_id=?", (plan.product_id,)).fetchone()
                    if product is None:
                        value = ProductRun(plan.product_id, plan.goal, created_at=stamp).to_dict()
                        db.execute("INSERT INTO product_runs(product_id,objective,status,product_json,created_at) VALUES(?,?,?,?,?)",
                                   (plan.product_id, plan.goal, "PENDING", _json(value), stamp))
                    db.execute("INSERT INTO product_pipeline_links(product_id,pipeline_id) VALUES(?,?)", (plan.product_id, plan.pipeline_id))
                    product_row = db.execute("SELECT product_json FROM product_runs WHERE product_id=?", (plan.product_id,)).fetchone()
                    product_value = json.loads(product_row[0])
                    if plan.pipeline_id not in product_value["pipelineIds"]:
                        product_value["pipelineIds"].append(plan.pipeline_id)
                    product_value["status"] = "PENDING"
                    db.execute("UPDATE product_runs SET status='PENDING' WHERE product_id=?", (plan.product_id,))
                    db.execute("UPDATE product_runs SET product_json=? WHERE product_id=?", (_json(product_value), plan.product_id))
                for step in plan.steps:
                    state = "RUNNABLE" if not step.depends_on else "WAITING_FOR_DEPENDENCIES"
                    task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"system-agentic:{plan.pipeline_id}:{step.step_id}"))
                    db.execute("INSERT INTO pipeline_steps(pipeline_id,step_id,module_id,capability,state,execution_mode,task_id,resolution_json) VALUES(?,?,?,?,?,?,?,?)",
                               (plan.pipeline_id, step.step_id, step.module_id, step.capability, state, step.execution_mode, task_id,
                                _json(step.resolution_evidence)))
                    if state == "RUNNABLE":
                        self._event(db, plan.pipeline_id, "step_runnable", step.step_id, step.module_id,
                                    {"productId": plan.product_id, "requestedCapability": step.required_capability or step.capability,
                                     "resolvedModuleId": step.module_id})
                    self._event(db, plan.pipeline_id, "capability_provider_resolved", step.step_id, step.module_id,
                                {"productId": plan.product_id, "requestedCapability": step.required_capability or step.capability,
                                 "resolvedModuleId": step.module_id, "resolvedWorkspace": step.resolved_workspace,
                                 "resolutionEvidence": step.resolution_evidence})
                self._event(db, plan.pipeline_id, "pipeline_created", payload={"failurePolicy": plan.failure_policy,
                            "productId": plan.product_id, "templateId": plan.template_id})
                db.commit()
        except sqlite3.IntegrityError as exc:
            raise PipelineExecutionError(f"Pipeline ID {plan.pipeline_id!r} already exists; execution will not be repeated.") from exc

    def refresh_dependencies(self, plan: PipelinePlan) -> dict[str, str]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            states = {row["step_id"]: row["state"] for row in db.execute(
                "SELECT step_id,state FROM pipeline_steps WHERE pipeline_id=?", (plan.pipeline_id,))}
            by_id = {step.step_id: step for step in plan.steps}
            for step_id, state in states.items():
                if state != "WAITING_FOR_DEPENDENCIES":
                    continue
                dependency_states = [states[dependency] for dependency in by_id[step_id].depends_on]
                if all(item == "COMPLETED" for item in dependency_states):
                    db.execute("UPDATE pipeline_steps SET state='RUNNABLE' WHERE pipeline_id=? AND step_id=? AND state=?",
                               (plan.pipeline_id, step_id, state))
                    self._event(db, plan.pipeline_id, "step_runnable", step_id, by_id[step_id].module_id)
                elif any(item in {"FAILED", "SKIPPED", "CANCELLED"} for item in dependency_states):
                    db.execute("UPDATE pipeline_steps SET state='SKIPPED',finished_at=? WHERE pipeline_id=? AND step_id=? AND state=?",
                               (utc_now(), plan.pipeline_id, step_id, state))
                    self._event(db, plan.pipeline_id, "step_skipped", step_id, by_id[step_id].module_id,
                                {"reason": "DEPENDENCY_DID_NOT_COMPLETE"})
            db.commit()
        return self.step_states(plan.pipeline_id)

    def step_states(self, pipeline_id: str) -> dict[str, str]:
        with self._db() as db:
            return {row["step_id"]: row["state"] for row in db.execute(
                "SELECT step_id,state FROM pipeline_steps WHERE pipeline_id=?", (pipeline_id,))}

    def claim_step(self, pipeline_id: str, step: PipelineStep) -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT status,cancellation_requested FROM pipeline_runs WHERE pipeline_id=?", (pipeline_id,)).fetchone()
            if not run or run["cancellation_requested"] or run["status"] not in {"PENDING", "RUNNING"}:
                db.rollback()
                return False
            cursor = db.execute("UPDATE pipeline_steps SET state='CLAIMED' WHERE pipeline_id=? AND step_id=? AND state='RUNNABLE'",
                                (pipeline_id, step.step_id))
            if cursor.rowcount != 1:
                db.rollback()
                return False
            stamp = utc_now()
            run = db.execute("SELECT status FROM pipeline_runs WHERE pipeline_id=?", (pipeline_id,)).fetchone()
            db.execute("UPDATE pipeline_runs SET status='RUNNING',started_at=COALESCE(started_at,?) WHERE pipeline_id=?",
                       (stamp, pipeline_id))
            if run["status"] == "PENDING":
                self._event(db, pipeline_id, "pipeline_started")
            self._event(db, pipeline_id, "step_claimed", step.step_id, step.module_id)
            db.commit()
            return True

    def start_step(self, pipeline_id: str, step: PipelineStep) -> bool:
        stamp = utc_now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("UPDATE pipeline_steps SET state='RUNNING',started_at=? WHERE pipeline_id=? AND step_id=? AND state='CLAIMED'",
                                (stamp, pipeline_id, step.step_id))
            if cursor.rowcount == 1:
                self._event(db, pipeline_id, "step_started", step.step_id, step.module_id)
            db.commit()
            return cursor.rowcount == 1

    def complete_step(self, pipeline_id: str, step: PipelineStep, result: dict[str, Any],
                      artifacts: list[dict[str, Any]], manifests: dict[str, dict[str, str]],
                      producer_workspace: str | Path) -> None:
        stamp = utc_now()
        ids = [artifact["artifactId"] for artifact in artifacts]
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM pipeline_steps WHERE pipeline_id=? AND step_id=?", (pipeline_id, step.step_id)).fetchone()
            if not row or row["state"] != "RUNNING":
                db.rollback()
                raise PipelineExecutionError("Step lost its RUNNING claim before completion.")
            for artifact in artifacts:
                artifact_id = artifact["artifactId"]
                db.execute("INSERT INTO pipeline_artifacts(artifact_id,pipeline_id,step_id,artifact_kind,artifact_json,file_manifest_json) VALUES(?,?,?,?,?,?)",
                           (artifact_id, pipeline_id, step.step_id, artifact.get("artifactKind", "ProductArtifact"),
                            _json(artifact), _json(manifests[artifact_id])))
                db.execute("INSERT INTO artifact_store(artifact_id,product_id,pipeline_id,producer_module_id,producer_step_id,artifact_kind,artifact_json,producer_workspace,file_manifest_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (artifact_id, artifact.get("productId"), pipeline_id, step.module_id, step.step_id,
                            artifact.get("artifactKind", "ProductArtifact"), _json(artifact),
                            str(Path(producer_workspace).resolve(strict=True)), _json(manifests[artifact_id]), stamp))
                event_type = {"MediaArtifact": "media_produced", "MediaVariant": "media_variant_produced", "ContentVariant": "content_variant_produced",
                              "ContentPackage": "content_package_created",
                              "DistributionIntent": "distribution_intent_created"}.get(artifact.get("artifactKind"), "artifact_produced")
                self._event(db, pipeline_id, event_type, step.step_id, step.module_id,
                            {"artifactId": artifact_id, "productId": artifact.get("productId"),
                             "artifactType": artifact.get("productType", artifact.get("mediaType", artifact.get("format")))})
            db.execute("UPDATE pipeline_steps SET state='COMPLETED',finished_at=?,result_json=?,artifact_ids_json=? WHERE pipeline_id=? AND step_id=?",
                       (stamp, _json(result), _json(ids), pipeline_id, step.step_id))
            product_ids = {item.get("productId") for item in artifacts if item.get("productId")}
            if len(product_ids) > 1:
                db.rollback()
                raise PipelineExecutionError("One step cannot produce artifacts for multiple ProductRuns.")
            if product_ids:
                product_id = next(iter(product_ids))
                product_row = db.execute("SELECT product_json FROM product_runs WHERE product_id=?", (product_id,)).fetchone()
                if product_row is None:
                    db.rollback()
                    raise PipelineExecutionError("Artifact references an unregistered ProductRun.")
                product = json.loads(product_row[0])
                for item in artifacts:
                    ref = item["artifactId"]
                    key = {"ProductArtifact": "artifactRefs", "MediaArtifact": "mediaRefs", "MediaVariant": "mediaRefs",
                           "ContentVariant": "contentVariantRefs", "ContentPackage": "contentVariantRefs", "DistributionIntent": "distributionIntentRefs"}.get(item.get("artifactKind", "ProductArtifact"))
                    if key and ref not in product[key]:
                        product[key].append(ref)
                db.execute("UPDATE product_runs SET status='RUNNING',product_json=? WHERE product_id=?",
                           (_json(product), product_id))
            self._event(db, pipeline_id, "step_completed", step.step_id, step.module_id,
                        {"executionStatus": "COMPLETED", "artifactIds": ids})
            db.commit()

    def fail_step(self, pipeline_id: str, step: PipelineStep, error: str,
                  result: dict[str, Any] | None = None) -> None:
        stamp = utc_now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE pipeline_steps SET state='FAILED',finished_at=?,error=?,result_json=? WHERE pipeline_id=? AND step_id=? AND state IN ('CLAIMED','RUNNING')",
                       (stamp, error[:2000], _json(result) if result is not None else None, pipeline_id, step.step_id))
            self._event(db, pipeline_id, "step_failed", step.step_id, step.module_id, {"error": error[:1000]})
            db.commit()

    def set_pending_state(self, pipeline_id: str, step: PipelineStep, state: str, reason: str) -> None:
        if state not in {"SKIPPED", "CANCELLED"}:
            raise ValueError("Only terminal pending-step transitions are supported.")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE pipeline_steps SET state=?,finished_at=?,error=? WHERE pipeline_id=? AND step_id=? AND state IN ('RUNNABLE','WAITING_FOR_DEPENDENCIES')",
                       (state, utc_now(), reason, pipeline_id, step.step_id))
            self._event(db, pipeline_id, "step_skipped" if state == "SKIPPED" else "step_cancelled",
                        step.step_id, step.module_id, {"reason": reason})
            db.commit()

    def request_cancel(self, pipeline_id: str) -> bool:
        with self._db() as db:
            cursor = db.execute("UPDATE pipeline_runs SET cancellation_requested=1 WHERE pipeline_id=? AND status IN ('PENDING','RUNNING')",
                                (pipeline_id,))
            if cursor.rowcount:
                self._event(db, pipeline_id, "pipeline_cancel_requested")
            return cursor.rowcount == 1

    def cancellation_requested(self, pipeline_id: str) -> bool:
        with self._db() as db:
            row = db.execute("SELECT cancellation_requested FROM pipeline_runs WHERE pipeline_id=?", (pipeline_id,)).fetchone()
            return bool(row and row[0])

    def finalize(self, pipeline_id: str, status: str, error: str | None = None) -> None:
        if status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            raise ValueError("Pipeline final status must be terminal.")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE pipeline_runs SET status=?,finished_at=?,error=? WHERE pipeline_id=? AND status IN ('PENDING','RUNNING')",
                       (status, utc_now(), error, pipeline_id))
            links = list(db.execute("SELECT product_id FROM product_pipeline_links WHERE pipeline_id=?", (pipeline_id,)))
            for link in links:
                product_id = link["product_id"]
                row = db.execute("SELECT product_json FROM product_runs WHERE product_id=?", (product_id,)).fetchone()
                value = json.loads(row[0])
                pipeline_states = [item[0] for item in db.execute(
                    "SELECT r.status FROM pipeline_runs r JOIN product_pipeline_links l USING(pipeline_id) WHERE l.product_id=?",
                    (product_id,))]
                if any(item == "RUNNING" for item in pipeline_states):
                    product_status = "RUNNING"
                elif any(item == "PENDING" for item in pipeline_states):
                    product_status = "PENDING"
                elif any(item == "FAILED" for item in pipeline_states):
                    product_status = "FAILED"
                elif any(item == "CANCELLED" for item in pipeline_states):
                    product_status = "CANCELLED"
                else:
                    product_status = "COMPLETED"
                value["status"] = product_status
                db.execute("UPDATE product_runs SET status=?,product_json=? WHERE product_id=?",
                           (product_status, _json(value), product_id))
            self._event(db, pipeline_id, "pipeline_completed" if status == "COMPLETED" else "pipeline_failed" if status == "FAILED" else "pipeline_cancelled",
                        payload={"status": status, "error": error} if error else {"status": status})
            db.commit()

    def add_lease(self, lease: WorkspaceLease, *, read_only_safe: bool) -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = list(db.execute("SELECT execution_mode FROM workspace_leases WHERE workspace_path=?", (lease.workspace_path,)))
            allowed = (lease.execution_mode == "MUTATING" and not existing) or (
                lease.execution_mode == "READ_ONLY" and read_only_safe
                and all(row["execution_mode"] == "READ_ONLY" for row in existing))
            if not allowed:
                db.rollback()
                return False
            db.execute("INSERT INTO workspace_leases(lease_id,workspace_path,module_id,pipeline_id,step_id,execution_mode,owner_pid,acquired_at) VALUES(?,?,?,?,?,?,?,?)",
                       (lease.lease_id, lease.workspace_path, lease.module_id, lease.pipeline_id, lease.step_id,
                        lease.execution_mode, lease.owner_pid, lease.acquired_at))
            db.commit()
            return True

    def release_lease(self, lease_id: str) -> WorkspaceLease | None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,)).fetchone()
            if not row:
                db.rollback()
                return None
            lease = WorkspaceLease(row["lease_id"], row["module_id"], row["workspace_path"], row["pipeline_id"],
                                   row["step_id"], row["execution_mode"], row["owner_pid"], row["acquired_at"])
            db.execute("DELETE FROM workspace_leases WHERE lease_id=?", (lease_id,))
            db.commit()
            return lease

    def inspect_leases(self, workspace_path: str | None = None) -> list[WorkspaceLease]:
        with self._db() as db:
            if workspace_path:
                rows = db.execute("SELECT * FROM workspace_leases WHERE workspace_path=? ORDER BY acquired_at", (workspace_path,))
            else:
                rows = db.execute("SELECT * FROM workspace_leases ORDER BY acquired_at")
            return [WorkspaceLease(row["lease_id"], row["module_id"], row["workspace_path"], row["pipeline_id"],
                                   row["step_id"], row["execution_mode"], row["owner_pid"], row["acquired_at"]) for row in rows]

    def artifacts(self, pipeline_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            return [json.loads(row["artifact_json"]) for row in db.execute(
                "SELECT artifact_json FROM pipeline_artifacts WHERE pipeline_id=? ORDER BY rowid", (pipeline_id,))]

    def product_run(self, product_id: str) -> ProductRun | None:
        with self._db() as db:
            row = db.execute("SELECT product_json FROM product_runs WHERE product_id=?", (product_id,)).fetchone()
        return ProductRun.from_dict(json.loads(row[0])) if row else None

    def create_product_run(self, product: ProductRun | dict[str, Any]) -> ProductRun:
        value = product.to_dict() if isinstance(product, ProductRun) else product
        normalized = ProductRun.from_dict(value)
        try:
            with self._db() as db:
                db.execute("INSERT INTO product_runs(product_id,objective,status,product_json,created_at) VALUES(?,?,?,?,?)",
                           (normalized.product_id, normalized.objective, normalized.status, _json(normalized.to_dict()), normalized.created_at))
        except sqlite3.IntegrityError as exc:
            raise PipelineExecutionError(f"ProductRun {normalized.product_id!r} already exists.") from exc
        return normalized

    def product_runs(self) -> list[ProductRun]:
        with self._db() as db:
            rows = list(db.execute("SELECT product_json FROM product_runs ORDER BY created_at,product_id"))
        return [ProductRun.from_dict(json.loads(row[0])) for row in rows]

    def authorize_product_relation(self, source_product_id: str, target_product_id: str, reason: str) -> None:
        if not source_product_id.strip() or not target_product_id.strip() or not reason.strip():
            raise ValueError("Product artifact sharing requires source, target, and a recorded reason.")
        try:
            with self._db() as db:
                db.execute("INSERT INTO product_artifact_relations(source_product_id,target_product_id,reason,authorized_at) VALUES(?,?,?,?)",
                           (source_product_id, target_product_id, reason.strip()[:500], utc_now()))
        except sqlite3.IntegrityError as exc:
            raise PipelineExecutionError("Product artifact relation is invalid or already declared.") from exc

    def product_relation_allowed(self, source_product_id: str, target_product_id: str) -> bool:
        with self._db() as db:
            row = db.execute("SELECT 1 FROM product_artifact_relations WHERE source_product_id=? AND target_product_id=?",
                             (source_product_id, target_product_id)).fetchone()
        return row is not None

    def artifact_manifest(self, artifact_id: str) -> dict[str, str] | None:
        with self._db() as db:
            row = db.execute("SELECT file_manifest_json FROM pipeline_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def events(self, pipeline_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            return [{"pipelineId": row["pipeline_id"], "stepId": row["step_id"], "moduleId": row["module_id"],
                     "event": row["event_type"], "timestamp": row["timestamp"], "payload": json.loads(row["payload_json"])}
                    for row in db.execute("SELECT * FROM pipeline_events WHERE pipeline_id=? ORDER BY event_id", (pipeline_id,))]

    def inspect(self, pipeline_id: str) -> dict[str, Any]:
        with self._db() as db:
            run = db.execute("SELECT * FROM pipeline_runs WHERE pipeline_id=?", (pipeline_id,)).fetchone()
            if not run:
                raise PipelineExecutionError(f"Pipeline {pipeline_id!r} not found.")
            step_rows = list(db.execute("SELECT * FROM pipeline_steps WHERE pipeline_id=? ORDER BY rowid", (pipeline_id,)))
            product = db.execute("SELECT product_id FROM product_pipeline_links WHERE pipeline_id=?", (pipeline_id,)).fetchone()
        return {
            "pipeline": PipelineRun(run["pipeline_id"], run["status"], run["created_at"], run["started_at"],
                                    run["finished_at"], run["failure_policy"], bool(run["cancellation_requested"]),
                                    run["error"], product["product_id"] if product else None),
            "steps": [PipelineStepRun(row["pipeline_id"], row["step_id"], row["module_id"], row["capability"],
                                       row["state"], row["execution_mode"], row["task_id"], row["started_at"],
                                       row["finished_at"], json.loads(row["result_json"]) if row["result_json"] else None,
                                       row["error"], tuple(json.loads(row["artifact_ids_json"])),
                                       json.loads(row["resolution_json"])) for row in step_rows],
            "artifacts": self.artifacts(pipeline_id), "events": self.events(pipeline_id),
            "activeLeases": [asdict(lease) for lease in self.inspect_leases()],
        }


class ArtifactStore:
    """Immutable cross-pipeline artifact references backed by the pipeline SQLite DB."""

    def __init__(self, store: PipelineStore) -> None:
        self.store = store

    def put_artifact(self, artifact: dict[str, Any], *, producer_workspace: str | Path,
                     file_manifest: dict[str, str]) -> dict[str, Any]:
        value = dict(artifact)
        artifact_id = value.get("artifactId")
        required = ("pipelineId", "productId", "producerModuleId", "producerStepId", "artifactKind")
        if not isinstance(artifact_id, str) or not artifact_id.strip() or any(not isinstance(value.get(k), str) or not value[k].strip() for k in required):
            raise PipelineExecutionError("ArtifactStore requires a complete artifact identity and provenance.")
        root = Path(producer_workspace).resolve(strict=True)
        current_manifest = self._manifest(value, root)
        if current_manifest != file_manifest:
            raise PipelineExecutionError("ArtifactStore integrity manifest does not match producer files.")
        with self.store._db() as db:
            step = db.execute("SELECT module_id,state FROM pipeline_steps WHERE pipeline_id=? AND step_id=?",
                              (value["pipelineId"], value["producerStepId"])).fetchone()
            if not step or step["module_id"] != value["producerModuleId"] or step["state"] != "COMPLETED":
                raise PipelineExecutionError("ArtifactStore producer provenance is not a completed pipeline step.")
            try:
                db.execute("INSERT INTO artifact_store(artifact_id,product_id,pipeline_id,producer_module_id,producer_step_id,artifact_kind,artifact_json,producer_workspace,file_manifest_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (artifact_id, value["productId"], value["pipelineId"], value["producerModuleId"],
                            value["producerStepId"], value["artifactKind"], _json(value), str(root),
                            _json(file_manifest), value.get("createdAt", utc_now())))
            except sqlite3.IntegrityError as exc:
                raise PipelineExecutionError(f"Artifact reference {artifact_id!r} is immutable and already exists.") from exc
        return self.get_artifact(artifact_id)  # type: ignore[return-value]

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.store._db() as db:
            row = db.execute("SELECT * FROM artifact_store WHERE artifact_id=?", (artifact_id,)).fetchone()
        return self._record(row) if row else None

    def list_artifacts_for_product(self, product_id: str) -> list[dict[str, Any]]:
        with self.store._db() as db:
            rows = list(db.execute("SELECT * FROM artifact_store WHERE product_id=? ORDER BY created_at,artifact_id", (product_id,)))
        return [self._record(row) for row in rows]

    def validate_reference(self, artifact_id: str, *, product_id: str | None = None,
                           authorized_product_ids: tuple[str, ...] = (),
                           expected_types: tuple[str, ...] = ()) -> dict[str, Any]:
        record = self.get_artifact(artifact_id)
        if record is None:
            raise PipelineExecutionError(f"Artifact reference {artifact_id!r} is not in the canonical ArtifactStore.")
        artifact = record["artifact"]
        if (record["artifactId"] != artifact.get("artifactId")
                or record["pipelineId"] != artifact.get("pipelineId")
                or record["producerModuleId"] != artifact.get("producerModuleId")
                or record["producerStepId"] != artifact.get("producerStepId")):
            raise PipelineExecutionError("ArtifactStore provenance columns do not match the immutable artifact record.")
        source_product = record["productId"]
        if source_product != product_id:
            if (product_id is None or source_product not in authorized_product_ids
                    or not self.store.product_relation_allowed(source_product, product_id)):
                raise PipelineExecutionError("Cross-product artifact handoff is blocked without an explicit stored product relation.")
        artifact_type = PipelineExecutor._artifact_type(artifact)
        if expected_types and artifact_type not in expected_types:
            raise PipelineExecutionError(f"Artifact type {artifact_type!r} is not compatible with the consuming module.")
        root = Path(record["producerWorkspace"]).resolve(strict=True)
        current = self._manifest(artifact, root)
        if current != record["fileManifest"]:
            raise PipelineExecutionError(f"Artifact {artifact_id!r} changed after production.")
        record["producerWorkspace"] = str(root)
        record["sha256"] = current
        return record

    def resolve_input_refs(self, refs: tuple[str, ...] | list[str], *, product_id: str | None,
                           authorized_product_ids: tuple[str, ...] = (),
                           expected_types: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        return [self.validate_reference(ref, product_id=product_id,
                                        authorized_product_ids=authorized_product_ids,
                                        expected_types=expected_types) for ref in refs]

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {"artifactId": row["artifact_id"], "productId": row["product_id"],
                "pipelineId": row["pipeline_id"], "producerModuleId": row["producer_module_id"],
                "producerStepId": row["producer_step_id"], "artifactKind": row["artifact_kind"],
                "artifact": json.loads(row["artifact_json"]), "producerWorkspace": row["producer_workspace"],
                "fileManifest": json.loads(row["file_manifest_json"]), "createdAt": row["created_at"]}

    @staticmethod
    def _manifest(artifact: dict[str, Any], root: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        paths = list(artifact.get("files", [])) + list(artifact.get("previewAssets", []))
        if artifact.get("fileRef"):
            paths.append(artifact["fileRef"])
        if len(paths) > MAX_ARTIFACT_FILES:
            raise PipelineExecutionError("ArtifactStore reference exceeds the file-count limit.")
        total = 0
        for relative in paths:
            path = Path(relative)
            if path.is_absolute():
                raise PipelineExecutionError("ArtifactStore paths must be relative to the producer workspace.")
            resolved = (root / path).resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise PipelineExecutionError("ArtifactStore file escapes its producer workspace or is not a file.")
            total += resolved.stat().st_size
            if total > MAX_ARTIFACT_BYTES:
                raise PipelineExecutionError("ArtifactStore files exceed the integrity-check size limit.")
            result[str(resolved.relative_to(root))] = hashlib.sha256(resolved.read_bytes()).hexdigest()
        return result


class WorkspaceLeaseManager:
    """Atomic SQLite-backed leases shared by executor processes using the same state DB."""

    def __init__(self, store: PipelineStore) -> None:
        self.store = store

    def acquire(self, module_id: str, workspace_path: str | Path, pipeline_id: str, step_id: str,
                execution_mode: str = "MUTATING", *, read_only_safe: bool = False) -> WorkspaceLease | None:
        if execution_mode not in {"READ_ONLY", "MUTATING"}:
            raise ValueError("Execution mode must be READ_ONLY or MUTATING.")
        if execution_mode == "READ_ONLY" and not read_only_safe:
            raise ValueError("READ_ONLY lease requires an adapter/capability safety declaration.")
        root = Path(workspace_path).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Workspace lease root must be a directory.")
        lease = WorkspaceLease(str(uuid.uuid4()), module_id, str(root), pipeline_id, step_id,
                               execution_mode, os.getpid(), utc_now())
        return lease if self.store.add_lease(lease, read_only_safe=read_only_safe) else None

    def release(self, lease: WorkspaceLease | str) -> bool:
        return self.store.release_lease(lease.lease_id if isinstance(lease, WorkspaceLease) else lease) is not None

    def inspect(self, workspace_path: str | Path | None = None) -> list[WorkspaceLease]:
        root = str(Path(workspace_path).expanduser().resolve(strict=False)) if workspace_path else None
        return self.store.inspect_leases(root)


class FixtureModuleExecutionAdapter:
    """Explicit test adapter; its handlers are local callables, never providers."""

    def __init__(self, handlers: dict[str, Callable[[PipelineStep, dict[str, Any]], dict[str, Any]]] | None = None) -> None:
        self.handlers = handlers or {}

    def health(self, module_id: str) -> dict[str, Any]:
        return {"status": "READY" if module_id in self.handlers else "UNAVAILABLE", "adapter": "fixture"}

    def execute(self, step: PipelineStep, context: dict[str, Any]) -> dict[str, Any]:
        handler = self.handlers.get(step.module_id)
        if handler is None:
            raise PipelineExecutionError(f"No fixture handler for module {step.module_id!r}.")
        return handler(step, context)

    def normalize_result(self, result: object, step: PipelineStep) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise PipelineExecutionError("Fixture adapter result must be an object.")
        normalized = dict(result)
        status = normalized.get("execution_status", normalized.get("status"))
        if status not in {"COMPLETED", "FAILED", "UNKNOWN", "CANCELLED"}:
            raise PipelineExecutionError("Fixture adapter returned an unsupported execution status.")
        normalized["execution_status"] = status
        normalized["module_id"] = step.module_id
        return normalized

    def read_only_safe(self, step: PipelineStep) -> bool:
        return step.execution_mode == "READ_ONLY"

    def artifact_workspace(self, step: PipelineStep, context: dict[str, Any]) -> Path:
        return Path(context["resolvedWorkspace"])


class SystemAgenticDispatchAdapter:
    """Wrap the existing task parser, provider selection, and queue dispatch path."""

    def __init__(self, *, provider_policy: dict[str, Any] | None = None, wait_seconds: float = 600) -> None:
        self.provider_policy = dict(provider_policy or {"allowed_providers": ["local-qwen"]})
        self.wait_seconds = wait_seconds
        self.resolver = WorkspaceResolver()
        if wait_seconds < 0:
            raise ValueError("wait_seconds must be non-negative.")

    def health(self, module_id: str) -> dict[str, Any]:
        from providers.runtime import discover_providers

        statuses = discover_providers()
        local = next((item for item in statuses if item.provider_id == "local-qwen"), None)
        ready = bool(local and local.enabled and local.available and local.healthy
                     and local.execution_runtime_ready and "task.submit" in local.capabilities
                     and "worker" in local.roles
                     and "local-qwen" in self.provider_policy.get("allowed_providers", []))
        return {"status": "READY" if ready else "UNAVAILABLE", "provider_policy": self.provider_policy.copy()}

    def execute(self, step: PipelineStep, context: dict[str, Any]) -> dict[str, Any]:
        from providers.dispatch import dispatch_task, task_from_spec

        module = context["module"]
        root = context["resolvedWorkspace"]
        plan = context["plan"]
        objective = step.task.get("objective", plan.goal)
        if not isinstance(objective, str) or not objective.strip():
            raise PipelineExecutionError(f"Step {step.step_id!r} has no objective.")
        task_context = dict(step.task.get("context", {}))
        task_context.update({
            "targetModuleId": step.module_id,
            "resolvedModuleId": step.module_id,
            "requestedCapability": step.capability,
            "resolvedWorkspace": root,
            "pipelineId": plan.pipeline_id,
            "stepId": step.step_id,
            "productId": plan.product_id,
            "providerResolution": step.resolution_evidence,
            "inputArtifacts": context.get("inputArtifacts", []),
        })
        module_context = ModuleExecutionContext.resolve(
            step.module_id, step.required_capability or step.capability, resolver=self.resolver,
            pipeline_id=plan.pipeline_id, step_id=step.step_id, product_id=plan.product_id)
        task_context["moduleExecutionContext"] = module_context.to_dict()
        spec = {
            "task_id": context["taskId"], "workspace": root, "objective": objective,
            "role": "worker", "required_capabilities": ["task.submit"], "context": task_context,
            "constraints": step.task.get("constraints", []),
            "acceptance_criteria": step.task.get("acceptance_criteria", []),
            "execution_policy": "READ_ONLY" if step.execution_mode == "READ_ONLY" else "WORKSPACE_WRITE",
            "provider_policy": self.provider_policy,
            "execution_context": {"origin": "interactive-host", "host_role": "orchestrator", "dispatch_depth": 1,
                                  "direct_or_dispatch": "DISPATCH", "pipeline_id": plan.pipeline_id,
                                  "pipeline_step_id": step.step_id, "parent_task_id": plan.pipeline_id,
                                  "moduleExecutionContext": module_context.to_dict()},
            "targetModuleId": step.module_id, "requestedCapability": step.capability,
            "resolvedWorkspace": root,
        }
        task = task_from_spec(spec)
        if str(task.workspace.resolve(strict=True)) != root:
            raise PipelineExecutionError("Task parser changed the canonical resolved module workspace.")
        return dispatch_task(task, wait_seconds=self.wait_seconds)

    def normalize_result(self, result: object, step: PipelineStep) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise PipelineExecutionError("Provider dispatch did not return a normalized result object.")
        normalized = dict(result)
        status = normalized.get("execution_status", normalized.get("status"))
        if status not in {"COMPLETED", "FAILED", "UNKNOWN", "CANCELLED"}:
            raise PipelineExecutionError(f"Provider dispatch returned unsupported status {status!r}.")
        normalized["execution_status"] = status
        normalized["module_id"] = step.module_id
        return normalized

    def read_only_safe(self, step: PipelineStep) -> bool:
        return step.execution_mode == "READ_ONLY"

    def artifact_workspace(self, step: PipelineStep, context: dict[str, Any]) -> Path:
        return Path(context["resolvedWorkspace"])


class PipelineExecutor:
    def __init__(self, adapter: ModuleExecutionAdapter, *, registry: ModuleRegistry | None = None,
                 store: PipelineStore | None = None, lease_manager: WorkspaceLeaseManager | None = None,
                 coordinator: ProcessRuntimeCoordinator | None = None) -> None:
        self.adapter = adapter
        self.registry = registry or ModuleRegistry()
        self.resolver = WorkspaceResolver(self.registry)
        self.coordinator = coordinator or PROCESS_RUNTIME_COORDINATOR
        self.store = self.coordinator.pipeline_store(store)
        self.global_budget = self.coordinator.global_budget
        self.artifact_store = self.store.artifact_store
        self.leases = lease_manager or self.coordinator.lease_manager(self.store)
        self.capability_providers = CapabilityProviderRegistry(self.registry, self.adapter.health)

    def _resolve_plan(self, plan: PipelinePlan) -> PipelinePlan:
        """Resolve and freeze every capability choice before durable run creation."""
        resolved_steps = []
        for step in plan.steps:
            requested = step.required_capability or step.capability
            if step.module_id is not None:
                explicit = self.registry.get_module(step.module_id)
                if requested not in explicit.capabilities:
                    raise PipelineExecutionError(f"Unsupported capability {step.module_id}/{requested}.")
            providers = self.capability_providers.find_providers(requested, execution_mode=step.execution_mode)
            policy_config = step.selection_policy
            mode = policy_config.get("mode", "single")
            configured_priority = policy_config.get("priority", {})
            if not isinstance(configured_priority, dict):
                raise PipelineExecutionError("Provider selection priority must be an object.")
            policy = ProviderSelectionPolicy(mode, configured_priority)
            try:
                selected = policy.select(providers, explicit_module_id=step.module_id)
            except CapabilityResolutionError as exc:
                raise PipelineExecutionError(f"{exc.code}: {exc}") from exc
            module = self.registry.get_module(selected.provider.module_id)
            workspace = str(self.resolver.resolve_module_workspace(module.module_id))
            evidence = {"requestedCapability": requested, "resolvedModuleId": module.module_id,
                        "resolvedWorkspace": workspace, "policy": selected.policy,
                        **selected.evidence}
            resolved_steps.append(replace(step, module_id=module.module_id, required_capability=requested,
                                          resolved_workspace=workspace, resolution_evidence=evidence))
        return replace(plan, steps=tuple(resolved_steps))

    def _validate(self, plan: PipelinePlan) -> dict[str, Path]:
        if os.environ.get("SYSTEM_AGENTIC_EXECUTION_ROLE") == "worker":
            raise PipelineExecutionError("RECURSION_GUARD: worker tasks cannot execute pipeline graphs.")
        if not plan.pipeline_id or len(plan.steps) == 0:
            raise ValueError("Pipeline requires an ID and at least one step.")
        if plan.failure_policy not in {"FAIL_FAST", "CONTINUE_INDEPENDENT"}:
            raise ValueError("Unsupported failure policy.")
        if not 1 <= plan.concurrency.max_global_concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"max_global_concurrency must be between 1 and {MAX_CONCURRENCY}.")
        workspaces: dict[str, Path] = {}
        outputs: dict[str, str] = {}
        for step in plan.steps:
            if step.module_id is None:
                raise PipelineExecutionError("Step provider must be resolved before validation.")
            module = self.registry.get_module(step.module_id)
            if step.capability not in module.capabilities:
                raise PipelineExecutionError(f"Unsupported capability {step.module_id}/{step.capability}.")
            adapter_health = self.adapter.health(step.module_id)
            if not isinstance(adapter_health, dict) or adapter_health.get("status") != "READY":
                raise PipelineExecutionError(f"Execution adapter is not healthy for module {step.module_id!r}.")
            workspace = self.resolver.resolve_module_workspace(step.module_id)
            if step.resolved_workspace and str(workspace) != step.resolved_workspace:
                raise PipelineExecutionError("Resolved workspace changed after provider selection.")
            workspaces[step.step_id] = workspace
            limit = plan.concurrency.max_module_concurrency.get(step.module_id, module.max_concurrency)
            if type(limit) is not int or not 1 <= limit <= MAX_CONCURRENCY:
                raise ValueError(f"Invalid concurrency limit for {step.module_id!r}.")
            if step.execution_mode == "MUTATING" and plan.execution_policy != "WORKSPACE_WRITE":
                raise PipelineExecutionError("MUTATING steps require plan execution_policy WORKSPACE_WRITE.")
            if step.execution_mode == "MUTATING" and module.mutation_mode == "READ_ONLY":
                raise PipelineExecutionError(f"Module {step.module_id!r} is registered as READ_ONLY.")
            if step.execution_mode == "READ_ONLY" and not self.adapter.read_only_safe(step):
                raise PipelineExecutionError(f"Adapter does not attest READ_ONLY for {step.module_id}/{step.capability}.")
            for ref in step.output_refs:
                if ref in outputs:
                    raise PipelineExecutionError(f"Duplicate pipeline artifact reference {ref!r}.")
                outputs[ref] = step.step_id
        for step in plan.steps:
            ancestry: set[str] = set(step.depends_on)
            for dependency in step.depends_on:
                ancestry.update(self._ancestors(plan, dependency))
            for ref in step.input_refs:
                producer = outputs.get(ref)
                if producer is not None:
                    if producer not in ancestry:
                        raise PipelineExecutionError(f"Input reference {ref!r} is not an output of a dependency for step {step.step_id!r}.")
                    continue
                consumer = self.registry.get_module(step.module_id)
                self.artifact_store.validate_reference(
                    ref, product_id=plan.product_id,
                    authorized_product_ids=plan.authorized_product_ids,
                    expected_types=consumer.input_artifact_types)
        return workspaces

    def _input_artifacts(self, plan: PipelinePlan, step: PipelineStep, workspace_roots: dict[str, Path]) -> list[dict[str, Any]]:
        requested = set(step.input_refs)
        if not requested:
            return []
        records = {item["artifactId"]: item for item in self.store.artifacts(plan.pipeline_id)}
        refs: dict[str, str] = {}
        consumer = self.registry.get_module(step.module_id)
        for producer_step in plan.steps:
            for ref in producer_step.output_refs:
                refs[ref] = producer_step.step_id
        result = []
        for ref in step.input_refs:
            artifact_id = ref
            artifact = records.get(artifact_id)
            if artifact is None:
                record = self.artifact_store.validate_reference(
                    artifact_id, product_id=plan.product_id,
                    authorized_product_ids=plan.authorized_product_ids,
                    expected_types=consumer.input_artifact_types)
                result.append({"artifact": record["artifact"], "producerWorkspace": record["producerWorkspace"],
                               "sha256": record["fileManifest"], "crossPipeline": True})
                continue
            producer_step_id = artifact["producerStepId"]
            if producer_step_id not in step.depends_on and producer_step_id not in self._ancestors(plan, step.step_id):
                raise PipelineExecutionError("Artifact producer is not a dependency of the consumer.")
            record = self.artifact_store.validate_reference(
                artifact_id, product_id=plan.product_id,
                authorized_product_ids=plan.authorized_product_ids,
                expected_types=consumer.input_artifact_types)
            root = Path(record["producerWorkspace"])
            manifest = record["fileManifest"]
            current = record["sha256"]
            if plan.product_id and artifact.get("productId") != plan.product_id:
                raise PipelineExecutionError("Cross-product artifact handoff is blocked.")
            result.append({"artifact": artifact, "producerWorkspace": str(root), "sha256": current})
        return result

    @staticmethod
    def _artifact_type(artifact: dict[str, Any]) -> str:
        kind = artifact.get("artifactKind", "ProductArtifact")
        if kind == "ProductArtifact":
            return artifact.get("productType", "")
        if kind == "MediaArtifact":
            return artifact.get("mediaType", "")
        if kind == "MediaVariant":
            fmt = artifact.get("format", "")
            return fmt if "/" in fmt else f"video/{fmt}"
        if kind == "ContentPackage":
            return "content-package"
        if kind == "ContentVariant":
            return artifact.get("format", "")
        return "distribution_intent" if kind == "DistributionIntent" else kind

    @staticmethod
    def _ancestors(plan: PipelinePlan, step_id: str) -> set[str]:
        by_id = {step.step_id: step for step in plan.steps}
        found: set[str] = set()
        todo = list(by_id[step_id].depends_on)
        while todo:
            current = todo.pop()
            if current not in found:
                found.add(current)
                todo.extend(by_id[current].depends_on)
        return found

    @staticmethod
    def _artifact_manifest(artifact: dict[str, Any], root: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        total = 0
        paths = list(artifact.get("files", [])) + list(artifact.get("previewAssets", []))
        if artifact.get("fileRef"):
            paths.append(artifact["fileRef"])
        if len(paths) > MAX_ARTIFACT_FILES:
            raise PipelineExecutionError("Artifact must reference a bounded set of files.")
        for value in paths:
            path = Path(value)
            if path.is_absolute():
                raise PipelineExecutionError("ProductArtifact paths must be relative to the producer workspace.")
            resolved = (root / path).resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise PipelineExecutionError("ProductArtifact file escapes its producer workspace or is not a file.")
            total += resolved.stat().st_size
            if total > MAX_ARTIFACT_BYTES:
                raise PipelineExecutionError("ProductArtifact exceeds the bounded verification size.")
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            result[str(resolved.relative_to(root))] = digest
        return result

    def _validate_result_artifacts(self, plan: PipelinePlan, step: PipelineStep, result: dict[str, Any],
                                   workspace: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
        contracts = (("product_artifacts", ProductArtifact, "ProductArtifact", "artifactId"),
                     ("media_artifacts", MediaArtifact, "MediaArtifact", "mediaArtifactId"),
                     ("media_variants", MediaVariant, "MediaVariant", "mediaVariantId"),
                     ("content_variants", ContentVariant, "ContentVariant", "contentVariantId"),
                     ("content_packages", ContentPackage, "ContentPackage", "contentPackageId"),
                     ("distribution_intents", DistributionIntent, "DistributionIntent", "distributionIntentId"))
        artifacts: list[dict[str, Any]] = []
        manifests: dict[str, dict[str, str]] = {}
        for result_key, contract, kind, id_field in contracts:
            raw = result.get(result_key, [])
            if not isinstance(raw, list):
                raise PipelineExecutionError(f"Adapter {result_key} must be an array.")
            for item in raw:
                value_raw = item.to_dict() if hasattr(item, "to_dict") else item
                value = contract.from_dict(value_raw).to_dict()
                value["artifactKind"] = kind
                artifact_id = value[id_field]
                value["artifactId"] = artifact_id
                if (value.get("producerModuleId") != step.module_id or value.get("producerStepId") != step.step_id
                        or value.get("pipelineId") != plan.pipeline_id):
                    raise PipelineExecutionError(f"{kind} provenance does not match its executing pipeline step.")
                if plan.product_id and value.get("productId") != plan.product_id:
                    raise PipelineExecutionError(f"{kind} productId does not match its ProductRun.")
                module = self.registry.get_module(step.module_id)
                artifact_type = self._artifact_type(value)
                if module.output_artifact_types and artifact_type not in module.output_artifact_types:
                    raise PipelineExecutionError(f"Module {module.module_id!r} cannot produce artifact type {artifact_type!r}.")
                if artifact_id in manifests:
                    raise PipelineExecutionError(f"Duplicate artifact ID {artifact_id!r} in step result.")
                sources = (set(value.get("sourceArtifactRefs", [])) | set(value.get("sourceMediaRefs", []))
                           | set(value.get("mediaRefs", [])) | set(value.get("contentVariantRefs", []))
                           | set(value.get("mediaVariantRefs", [])))
                if not sources.issubset(set(step.input_refs)):
                    raise PipelineExecutionError(f"{kind} source references must be explicit step inputRefs.")
                manifests[artifact_id] = self._artifact_manifest(value, workspace)
                metadata = dict(value.get("metadata", {}))
                if "fileSha256" in metadata:
                    raise PipelineExecutionError("fileSha256 is reserved for executor integrity evidence.")
                metadata["fileSha256"] = manifests[artifact_id]
                value["metadata"] = metadata
                artifacts.append(value)
        expected = set(step.output_refs)
        actual = {artifact["artifactId"] for artifact in artifacts}
        if actual != expected:
            raise PipelineExecutionError(f"Step outputs do not match declared outputRefs; expected {sorted(expected)}, got {sorted(actual)}.")
        return artifacts, manifests

    def _release_lease(self, plan: PipelinePlan, step: PipelineStep, lease: WorkspaceLease, reason: str) -> None:
        if self.leases.release(lease):
            with self.store._db() as db:
                self.store._event(db, plan.pipeline_id, "lease_released", step.step_id, step.module_id,
                                  {"leaseId": lease.lease_id, "workspace": lease.workspace_path, "reason": reason})

    def _artifact_workspace(self, step: PipelineStep, workspace: Path, context: dict[str, Any]) -> Path:
        provider = getattr(self.adapter, "artifact_workspace", None)
        root = provider(step, context) if callable(provider) else workspace
        root = Path(root).expanduser().resolve(strict=False)
        if not root.is_dir():
            raise PipelineExecutionError("Adapter artifact workspace must be a directory.")
        return root

    def _execute_claimed(self, plan: PipelinePlan, step: PipelineStep, workspace: Path,
                         input_artifacts: list[dict[str, Any]], task_id: str,
                         lease: WorkspaceLease) -> bool:
        try:
            if not self.store.start_step(plan.pipeline_id, step):
                raise PipelineExecutionError("Claimed step could not transition to RUNNING.")
            context = {"plan": plan, "module": self.registry.get_module(step.module_id),
                       "resolvedWorkspace": str(workspace), "taskId": task_id,
                       "targetModuleId": step.module_id,
                       "requestedCapability": step.required_capability or step.capability,
                       "productId": plan.product_id,
                       "inputArtifacts": input_artifacts,
                       "isCancelled": lambda: self.store.cancellation_requested(plan.pipeline_id)}
            artifact_workspace = self._artifact_workspace(step, workspace, context)
            context["artifactWorkspace"] = str(artifact_workspace)
            raw = self.adapter.execute(step, context)
            result = self.adapter.normalize_result(raw, step)
            if result.get("execution_status") != "COMPLETED":
                self.store.fail_step(plan.pipeline_id, step,
                                     f"MODULE_{result.get('execution_status')}: {result.get('summary', '')}", result)
                return False
            for key, expected_value in step.expected.items():
                if key not in result or result[key] != expected_value:
                    raise PipelineExecutionError(f"Step {step.step_id!r} result did not meet expected field {key!r}.")
            artifacts, manifests = self._validate_result_artifacts(plan, step, result, artifact_workspace)
            self.store.complete_step(plan.pipeline_id, step, result, artifacts, manifests, artifact_workspace)
            return True
        except Exception as exc:
            self.store.fail_step(plan.pipeline_id, step, f"{type(exc).__name__}: {exc}")
            return False
        finally:
            self._release_lease(plan, step, lease, "STEP_FINISHED")

    def cancel(self, pipeline_id: str) -> bool:
        return self.store.request_cancel(pipeline_id)

    def run_many(self, plans: list[PipelinePlan], *, max_product_concurrency: int = 1) -> list[dict[str, Any]]:
        """Run independent pipelines under a batch cap and shared step budget.

        The batch cap limits simultaneous pipeline runners. The process-global
        budget then limits actual steps across every run and executor instance.
        """
        if type(max_product_concurrency) is not int or not 1 <= max_product_concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"max_product_concurrency must be between 1 and {MAX_CONCURRENCY}.")
        if len({plan.pipeline_id for plan in plans}) != len(plans):
            raise ValueError("A batch must contain unique pipeline IDs.")
        if not plans:
            return []
        with ThreadPoolExecutor(max_workers=max_product_concurrency,
                                thread_name_prefix="system-agentic-product-pipeline") as pool:
            futures = [pool.submit(self.run, plan) for plan in plans]
            return [future.result() for future in futures]

    def run(self, plan: PipelinePlan) -> dict[str, Any]:
        plan = self._resolve_plan(plan)
        workspaces = self._validate(plan)
        self.store.create_run(plan)
        active: dict[Future[bool], tuple[PipelineStep, WorkspaceLease]] = {}
        active_by_module: dict[str, int] = {}
        max_workers = plan.concurrency.max_global_concurrency
        failure_seen = False
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="system-agentic-pipeline") as pool:
            while True:
                states = self.store.refresh_dependencies(plan)
                cancelled = self.store.cancellation_requested(plan.pipeline_id)
                if cancelled or (failure_seen and plan.failure_policy == "FAIL_FAST"):
                    reason = "PIPELINE_CANCELLED" if cancelled else "FAIL_FAST_AFTER_STEP_FAILURE"
                    for step in plan.steps:
                        if states.get(step.step_id) in {"RUNNABLE", "WAITING_FOR_DEPENDENCIES"}:
                            self.store.set_pending_state(plan.pipeline_id, step, "CANCELLED", reason)
                    states = self.store.step_states(plan.pipeline_id)
                slots = max_workers - len(active)
                budget_blocked = False
                if slots > 0 and not cancelled and not (failure_seen and plan.failure_policy == "FAIL_FAST"):
                    for step in plan.steps:
                        if slots <= 0:
                            break
                        if states.get(step.step_id) != "RUNNABLE":
                            continue
                        module_limit = plan.concurrency.max_module_concurrency.get(
                            step.module_id, self.registry.get_module(step.module_id).max_concurrency)
                        if active_by_module.get(step.module_id, 0) >= module_limit:
                            continue
                        if not self.global_budget.acquire(product_id=plan.product_id,
                                                         product_limit=plan.concurrency.max_product_concurrency,
                                                         module_id=step.module_id, module_limit=module_limit):
                            budget_blocked = True
                            break
                        try:
                            inputs = self._input_artifacts(plan, step, workspaces)
                            safe_read = self.adapter.read_only_safe(step)
                            lease = self.leases.acquire(step.module_id, workspaces[step.step_id], plan.pipeline_id,
                                                       step.step_id, step.execution_mode, read_only_safe=safe_read)
                        except Exception as exc:
                            self.global_budget.release(product_id=plan.product_id, module_id=step.module_id)
                            self.store.set_pending_state(plan.pipeline_id, step, "SKIPPED", f"PRE_DISPATCH_REFUSED:{type(exc).__name__}:{exc}")
                            failure_seen = True
                            continue
                        if lease is None:
                            self.global_budget.release(product_id=plan.product_id, module_id=step.module_id)
                            # Another executor in this process may hold the same
                            # workspace lease while it runs from a different
                            # pipeline store. Wait for that shared execution to
                            # release rather than treating contention as a dead
                            # lease or skipping the step.
                            if self.global_budget.inspect()["activeGlobalSteps"] > 0:
                                budget_blocked = True
                            continue
                        if not self.store.claim_step(plan.pipeline_id, step):
                            self._release_lease(plan, step, lease, "STEP_CLAIM_LOST")
                            self.global_budget.release(product_id=plan.product_id, module_id=step.module_id)
                            continue
                        with self.store._db() as db:
                            self.store._event(db, plan.pipeline_id, "lease_acquired", step.step_id, step.module_id,
                                              {"leaseId": lease.lease_id, "workspace": lease.workspace_path,
                                               "executionMode": lease.execution_mode})
                        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"system-agentic:{plan.pipeline_id}:{step.step_id}"))
                        try:
                            future = pool.submit(self._execute_claimed, plan, step, workspaces[step.step_id], inputs, task_id, lease)
                        except Exception:
                            self._release_lease(plan, step, lease, "DISPATCH_SUBMISSION_FAILED")
                            self.global_budget.release(product_id=plan.product_id, module_id=step.module_id)
                            self.store.fail_step(plan.pipeline_id, step, "DISPATCH_SUBMISSION_FAILED")
                            failure_seen = True
                            continue
                        active[future] = (step, lease)
                        active_by_module[step.module_id] = active_by_module.get(step.module_id, 0) + 1
                        slots -= 1
                        states[step.step_id] = "RUNNING"
                if active:
                    done, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                    for future in done:
                        step, _lease = active.pop(future)
                        self.global_budget.release(product_id=plan.product_id, module_id=step.module_id)
                        active_by_module[step.module_id] -= 1
                        try:
                            success = future.result()
                        except Exception as exc:
                            self.store.fail_step(plan.pipeline_id, step, f"EXECUTOR_EXCEPTION:{type(exc).__name__}:{exc}")
                            success = False
                        failure_seen = failure_seen or not success
                    continue
                states = self.store.refresh_dependencies(plan)
                if all(state in {"COMPLETED", "FAILED", "SKIPPED", "CANCELLED"} for state in states.values()):
                    break
                if not any(state == "RUNNABLE" for state in states.values()):
                    raise PipelineExecutionError("Pipeline made no progress; unresolved steps remain non-runnable.")
                if budget_blocked:
                    self.global_budget.wait_for_change(0.1)
                    continue
                # With no local work in flight, a still-held external workspace lease
                # is a deterministic pre-dispatch refusal, not a reason to spin.
                for step in plan.steps:
                    if states.get(step.step_id) == "RUNNABLE":
                        self.store.set_pending_state(plan.pipeline_id, step, "SKIPPED", "WORKSPACE_LEASE_CONFLICT")
                failure_seen = True
        states = self.store.step_states(plan.pipeline_id)
        if self.store.cancellation_requested(plan.pipeline_id):
            final = "CANCELLED"
        elif all(state == "COMPLETED" for state in states.values()):
            final = "COMPLETED"
        else:
            final = "FAILED"
        self.store.finalize(plan.pipeline_id, final)
        result = self.store.inspect(plan.pipeline_id)
        result["activeLeases"] = [asdict(lease) for lease in self.leases.inspect()]
        return result
