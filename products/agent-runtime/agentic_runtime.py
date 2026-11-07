#!/usr/bin/env python3
"""Local persistent task queue backed by SQLite and local-first agent execution."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent-brain"))
from providers.runtime import provider_inventory

LABEL = "ai.systemagentic.runtime"
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "SystemAgenticAI" / "runtime"
MAX_PROMPT_BYTES = 1_000_000
POLL_SECONDS = 2
STOP_REQUESTED = False


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _job_terminal_state(exit_code: int | None, stop_requested: bool) -> tuple[str, str]:
    """Map a worker exit to durable queue state.

    A graceful worker completion can race with LaunchAgent shutdown. The
    worker has already produced its terminal result when ``exit_code == 0``;
    the shutdown flag must not relabel that completed job as ``FAILED_LOST``.
    Non-zero exits remain interrupted only when shutdown was requested.
    """
    if exit_code == 0:
        return "completed", "COMPLETED"
    if stop_requested:
        return "failed", "FAILED_LOST"
    return "failed", "FAILED"


def secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def database(path: Path | None = None) -> sqlite3.Connection:
    root = path or APP_SUPPORT
    secure_dir(root)
    db = root / "jobs.sqlite3"
    conn = sqlite3.connect(db, timeout=30)
    os.chmod(db, 0o600)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            workspace TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','interrupted','cancelled')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            last_exit_code INTEGER,
            last_session_id TEXT,
            pending_mode TEXT NOT NULL DEFAULT 'new',
            backend TEXT NOT NULL DEFAULT 'codex',
            conversation_json TEXT,
            result_summary TEXT,
            execution_policy TEXT NOT NULL DEFAULT 'LEGACY_UNRESTRICTED',
            execution_context_json TEXT NOT NULL DEFAULT '{}',
            logical_task_id TEXT,
            execution_attempt_id TEXT,
            idempotency_key TEXT,
            worker_identity TEXT,
            terminal_status TEXT,
            result_ref TEXT
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            number INTEGER NOT NULL,
            mode TEXT NOT NULL,
            session_id TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            exit_code INTEGER,
            log_path TEXT NOT NULL,
            UNIQUE(job_id, number)
        );
        CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
        CREATE TABLE IF NOT EXISTS tool_jobs (
            id TEXT PRIMARY KEY,
            workspace TEXT NOT NULL,
            execution_policy TEXT NOT NULL,
            request_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            result_json TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS tool_jobs_queue ON tool_jobs(status, created_at);
        CREATE TABLE IF NOT EXISTS worker_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            heartbeat REAL NOT NULL,
            pid INTEGER NOT NULL,
            busy INTEGER NOT NULL
        );
        """
    )
    conn.execute("BEGIN IMMEDIATE")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "backend" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN backend TEXT NOT NULL DEFAULT 'codex'")
    if "conversation_json" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN conversation_json TEXT")
    if "result_summary" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN result_summary TEXT")
    if "execution_policy" not in columns:
        # Existing Codex sessions were launched without an enforceable sandbox.
        conn.execute("ALTER TABLE jobs ADD COLUMN execution_policy TEXT NOT NULL DEFAULT 'LEGACY_UNRESTRICTED'")
    if "execution_context_json" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN execution_context_json TEXT NOT NULL DEFAULT '{}'")
    for name in ("logical_task_id", "execution_attempt_id", "idempotency_key", "worker_identity", "terminal_status", "result_ref"):
        if name not in columns:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL")
    conn.commit()
    return conn


def discover_codex() -> str:
    found = shutil.which("codex")
    if found:
        return str(Path(found).resolve())
    candidates = [Path.home() / ".local" / "bin" / "codex", Path("/opt/homebrew/bin/codex")]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise RuntimeError("Không tìm thấy Codex CLI trong PATH.")


def codex_login_ready(binary: str, codex_home: str | None = None) -> bool:
    """Check login without exposing CLI output, which may contain account details."""
    env = os.environ.copy()
    if codex_home:
        env["CODEX_HOME"] = codex_home
    try:
        result = subprocess.run(
            [binary, "login", "status"], env=env, capture_output=True, text=True,
            timeout=10, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def submit(args: argparse.Namespace) -> None:
    if os.environ.get("SYSTEM_AGENTIC_EXECUTION_ROLE") in {"worker", "tool-executor"}:
        raise ValueError("RECURSION_GUARD: dispatched workers cannot enqueue provider or runtime child tasks.")
    workspace = Path(args.workspace).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace phải là một thư mục tồn tại.")
    prompt = args.prompt
    if args.file:
        prompt = Path(args.file).expanduser().read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError("Task trống.")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError(f"Task vượt quá {MAX_PROMPT_BYTES} bytes.")
    execution_policy = getattr(args, "execution_policy", None)
    if execution_policy is None:
        execution_policy = "READ_ONLY" if args.backend == "codex" else "WORKSPACE_WRITE"
    if execution_policy not in {"READ_ONLY", "WORKSPACE_WRITE", "ELEVATED"}:
        raise ValueError("execution_policy must be READ_ONLY, WORKSPACE_WRITE, or ELEVATED.")
    if execution_policy == "ELEVATED":
        raise ValueError("ELEVATED_REQUIRES_APPROVAL: queue dispatch cannot grant unrestricted or external-write access.")
    raw_execution_context = getattr(args, "execution_context", "{}")
    try:
        execution_context = json.loads(raw_execution_context) if isinstance(raw_execution_context, str) else raw_execution_context
    except json.JSONDecodeError as exc:
        raise ValueError("execution_context must be valid JSON.") from exc
    if not isinstance(execution_context, dict):
        raise ValueError("execution_context must be a JSON object.")
    module_context = execution_context.get("moduleExecutionContext")
    if module_context is not None:
        from modules.workspace_resolver import ModuleExecutionContext
        validated = ModuleExecutionContext.validate(module_context, workspace)
        execution_context["moduleExecutionContext"] = validated.to_dict()
    execution_context["assignedWorkspace"] = str(workspace)
    execution_context["workspaceAuthority"] = "SYSTEM_AGENTIC_QUEUE"
    if execution_context and (
        execution_context.get("host_role", "orchestrator") != "orchestrator"
        or type(execution_context.get("dispatch_depth", 1)) is not int
        or execution_context.get("dispatch_depth", 1) != 1
        or execution_context.get("direct_or_dispatch", "DISPATCH") != "DISPATCH"
    ):
        raise ValueError("Queue accepts only host-to-worker dispatch context at depth 1.")
    job_id = getattr(args, "task_id", None) or str(uuid.uuid4())
    if not job_id or len(job_id) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in job_id):
        raise ValueError("Task ID must be 1-128 letters, digits, dot, underscore, or hyphen.")
    stamp = now()
    title = args.title or " ".join(prompt.strip().split())[:96]
    conn = database()
    try:
        execution_context = dict(execution_context)
        logical_task_id = str(execution_context.get("logical_task_id") or job_id)
        execution_attempt_id = str(execution_context.get("execution_attempt_id") or f"{logical_task_id}-attempt-1")
        idempotency_key = str(execution_context.get("idempotency_key") or f"{logical_task_id}:{execution_attempt_id}")
        execution_context.update({"logical_task_id": logical_task_id, "execution_attempt_id": execution_attempt_id,
                                  "idempotency_key": idempotency_key})
    except (TypeError, ValueError) as exc:
        conn.close()
        raise ValueError("execution identity metadata must be string-compatible") from exc
    existing = conn.execute("SELECT id,status,backend,workspace,logical_task_id,execution_attempt_id,idempotency_key FROM jobs WHERE id=? OR idempotency_key=?", (job_id, idempotency_key)).fetchone()
    if existing is not None:
        if existing["backend"] != args.backend or existing["workspace"] != str(workspace):
            conn.close()
            raise ValueError("idempotency identity already belongs to a different provider task")
        print(json.dumps({"id": existing["id"], "status": existing["status"], "backend": existing["backend"],
                          "workspace": existing["workspace"], "logical_task_id": existing["logical_task_id"] or logical_task_id,
                          "execution_attempt_id": existing["execution_attempt_id"] or execution_attempt_id,
                          "idempotency_key": existing["idempotency_key"] or idempotency_key,
                          "reused": True}, ensure_ascii=False))
        conn.close()
        return
    if args.backend == "codex":
        codex = discover_codex()
        runtime = provider_inventory().get("runtime", {})
        codex_home = runtime.get("codex_home")
        if not runtime.get("queue_worker_loaded") or not isinstance(codex_home, str):
            raise RuntimeError("Codex queue worker/account is unavailable; task was not queued.")
        if not codex_login_ready(codex, codex_home):
            raise RuntimeError("Queue Codex account is not authenticated; task was not queued.")
    conn.execute(
        "INSERT INTO jobs(id,title,workspace,prompt,status,created_at,updated_at,backend,execution_policy,execution_context_json,logical_task_id,execution_attempt_id,idempotency_key,terminal_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, title, str(workspace), prompt, "queued", stamp, stamp, args.backend, execution_policy, json.dumps(execution_context, ensure_ascii=False, sort_keys=True), logical_task_id, execution_attempt_id, idempotency_key, "QUEUED"),
    )
    conn.commit()
    print(json.dumps({"id": job_id, "status": "queued", "backend": args.backend, "workspace": str(workspace),
                      "logical_task_id": logical_task_id, "execution_attempt_id": execution_attempt_id,
                      "idempotency_key": idempotency_key, "reused": False}, ensure_ascii=False))


def show_jobs(args: argparse.Namespace) -> None:
    conn = database()
    job_id = getattr(args, "id", None)
    if job_id:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"Không có task {job_id}.")
        job_data = dict(row)
        job_data.pop("conversation_json", None)
        raw_context = job_data.pop("execution_context_json", "{}")
        try:
            job_data["execution_context"] = json.loads(raw_context)
        except (TypeError, json.JSONDecodeError):
            job_data["execution_context"] = {}
        attempts = conn.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY number", (job_id,)).fetchall()
        if getattr(args, "json_output", False):
            job_data["attempts"] = [dict(attempt) for attempt in attempts]
            print(json.dumps(job_data, ensure_ascii=False))
            return
        print(json.dumps(job_data, ensure_ascii=False, indent=2))
        for attempt in attempts:
            print(json.dumps(dict(attempt), ensure_ascii=False))
        return
    rows = conn.execute(
        "SELECT id,title,status,backend,execution_policy,workspace,created_at,updated_at,last_exit_code,last_session_id FROM jobs ORDER BY created_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    for row in rows:
        print(json.dumps(dict(row), ensure_ascii=False))


def show_logs(args: argparse.Namespace) -> None:
    conn = database()
    rows = conn.execute("SELECT log_path FROM attempts WHERE job_id=? ORDER BY number", (args.id,)).fetchall()
    if not rows:
        raise ValueError(f"Chưa có log cho task {args.id}.")
    for row in rows:
        path = Path(row["log_path"])
        print(f"\n--- {path.name} ---")
        if path.exists():
            sys.stdout.write(path.read_text(encoding="utf-8", errors="replace"))


def resume(args: argparse.Namespace) -> None:
    conn = database()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT status,last_session_id,backend,conversation_json,execution_policy FROM jobs WHERE id=?", (args.id,)).fetchone()
    if row is None:
        conn.rollback()
        raise ValueError(f"Không có task {args.id}.")
    if row["status"] == "running":
        conn.rollback()
        raise ValueError("Task vẫn đang chạy.")
    if row["status"] == "queued":
        conn.rollback()
        raise ValueError("Task đã nằm trong hàng đợi.")
    if row["status"] in {"completed", "cancelled"} and not args.new_session:
        conn.rollback()
        raise ValueError("Task đã hoàn tất/hủy; dùng --new-session nếu thực sự muốn chạy lại từ đầu.")
    if row["backend"] == "codex" and row["execution_policy"] not in {"READ_ONLY", "WORKSPACE_WRITE"}:
        conn.rollback()
        raise ValueError("Task Codex cũ không có sandbox policy được xác minh; không thể resume an toàn. Tạo task mới qua system-agentic dispatch.")
    session_id = row["last_session_id"]
    if row["backend"] == "codex" and not session_id and not args.new_session:
        conn.rollback()
        raise ValueError("Task chưa có Codex session để resume; dùng --new-session để xác nhận chạy lại từ đầu.")
    if row["backend"] in {"local", "hybrid"} and not row["conversation_json"] and not args.new_session:
        conn.rollback()
        raise ValueError("Task local/hybrid chưa lưu hội thoại để resume; dùng --new-session để bắt đầu lại.")
    mode = "new" if args.new_session else "resume"
    conn.execute(
        "UPDATE jobs SET status='queued',updated_at=?,finished_at=NULL,last_exit_code=NULL,pending_mode=?,last_session_id=CASE WHEN ?='new' THEN NULL ELSE last_session_id END,conversation_json=CASE WHEN backend IN ('local','hybrid') AND ?='new' THEN NULL ELSE conversation_json END WHERE id=?",
        (now(), mode, mode, mode, args.id),
    )
    conn.commit()
    print(f"Đã đưa {args.id} vào hàng đợi ({mode}).")


def cancel(args: argparse.Namespace) -> None:
    conn = database()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (args.id,)).fetchone()
    if row is None:
        conn.rollback()
        raise ValueError(f"Không có task {args.id}.")
    if row["status"] != "queued":
        conn.rollback()
        raise ValueError(f"Chỉ hủy được task queued; trạng thái hiện tại là {row['status']}.")
    conn.execute("UPDATE jobs SET status='cancelled',updated_at=?,finished_at=? WHERE id=?", (now(), now(), args.id))
    conn.commit()
    print(f"Đã hủy task {args.id} nếu nó đang queued.")


def claim(conn: sqlite3.Connection) -> sqlite3.Row | None:
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT id, 'provider' AS kind, created_at FROM jobs WHERE status='queued' "
        "UNION ALL SELECT id, 'tool' AS kind, created_at FROM tool_jobs WHERE status='queued' "
        "ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        conn.commit()
        return None
    stamp = now()
    if row["kind"] == "tool":
        conn.execute("UPDATE tool_jobs SET status='running',started_at=?,finished_at=NULL,updated_at=? WHERE id=? AND status='queued'", (stamp, stamp, row["id"]))
        conn.execute("UPDATE worker_state SET heartbeat=?,pid=?,busy=1 WHERE singleton=1", (time.time(), os.getpid()))
        conn.commit()
        return conn.execute("SELECT *, 'tool' AS kind FROM tool_jobs WHERE id=?", (row["id"],)).fetchone()
    conn.execute("UPDATE jobs SET status='running',terminal_status='RUNNING',worker_identity=?,started_at=?,finished_at=NULL,updated_at=? WHERE id=? AND status='queued'", (str(os.getpid()), stamp, stamp, row["id"]))
    conn.execute("UPDATE worker_state SET heartbeat=?,pid=?,busy=1 WHERE singleton=1", (time.time(), os.getpid()))
    conn.commit()
    return conn.execute("SELECT *, 'provider' AS kind FROM jobs WHERE id=?", (row["id"],)).fetchone()


def enqueue_tool_execution(workspace: str, policy: str, tool: str, arguments: dict, wait_seconds: int) -> dict:
    """Submit one typed request only to a live, idle existing worker, then wait boundedly."""
    if os.environ.get("SYSTEM_AGENTIC_EXECUTION_ROLE") in {"worker", "tool-executor"}:
        raise ValueError("RECURSION_GUARD: execution workers cannot enqueue tool or provider work")
    if policy == "ELEVATED":
        raise ValueError("ELEVATED_REQUIRES_APPROVAL")
    if policy not in {"READ_ONLY", "WORKSPACE_WRITE"}:
        raise ValueError("unsupported execution policy")
    if tool not in {"workspace_exec", "run_tests"}:
        raise ValueError("unsupported tool job kind")
    if type(wait_seconds) is not int or not 1 <= wait_seconds <= 130:
        raise ValueError("worker wait must be between 1 and 130 seconds")
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("assigned workspace must be an existing directory")
    job_id = str(uuid.uuid4())
    request = {"tool": tool, "workspace": str(root), "execution_policy": policy, "arguments": arguments}
    conn = database()
    conn.execute("BEGIN IMMEDIATE")
    state = conn.execute("SELECT heartbeat,busy FROM worker_state WHERE singleton=1").fetchone()
    if state is None or time.time() - state["heartbeat"] > 10:
        conn.rollback()
        conn.close()
        raise RuntimeError("existing queue worker is unavailable; refusing inline execution")
    if state["busy"] or conn.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone() or conn.execute("SELECT 1 FROM tool_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
        conn.rollback()
        conn.close()
        raise RuntimeError("existing queue worker is busy; refusing inline execution")
    stamp = now()
    conn.execute(
        "INSERT INTO tool_jobs(id,workspace,execution_policy,request_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (job_id, str(root), policy, json.dumps(request, ensure_ascii=False), "queued", stamp, stamp),
    )
    conn.commit()
    deadline = time.monotonic() + wait_seconds
    result = None
    while time.monotonic() < deadline:
        row = conn.execute("SELECT status,result_json FROM tool_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            break
        if row["status"] in {"completed", "failed", "cancelled"}:
            result = json.loads(row["result_json"] or "{}")
            break
        time.sleep(0.1)
    if result is None:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE tool_jobs SET status='cancelled',finished_at=?,updated_at=? WHERE id=? AND status='queued'", (now(), now(), job_id))
        conn.execute("UPDATE tool_jobs SET cancel_requested=1,updated_at=? WHERE id=? AND status='running'", (now(), job_id))
        conn.commit()
        conn.close()
        raise TimeoutError("tool execution worker did not complete within the bounded wait")
    conn.close()
    return result


def process_tool_job(conn: sqlite3.Connection, job: sqlite3.Row) -> None:
    started = time.monotonic()
    try:
        request = json.loads(job["request_json"])
        if not isinstance(request, dict) or request.get("workspace") != job["workspace"] or request.get("execution_policy") != job["execution_policy"]:
            raise ValueError("typed request does not match its queue record")
        import importlib
        executor = importlib.import_module("tool_executor")
        executor = importlib.reload(executor)
        result = executor.execute_command(request, should_cancel=lambda: bool(
            conn.execute("SELECT cancel_requested FROM tool_jobs WHERE id=?", (job["id"],)).fetchone()[0]
        ))
        status = "completed"
    except Exception as exc:
        result = {"execution_status": "FAILED", "exit_code": None, "stdout": "", "stderr": "", "error": f"{type(exc).__name__}: {exc}"}
        if isinstance(locals().get("request"), dict) and request.get("tool") == "run_tests":
            result["test_outcome"] = "FAILED"
        status = "failed"
    result.update({
        "backend": "sqlite-launchagent-seatbelt",
        "queue_job_id": job["id"],
        "execution_policy": job["execution_policy"],
        "workspace": job["workspace"],
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
    })
    stamp = now()
    conn.execute("UPDATE tool_jobs SET status=?,result_json=?,updated_at=?,finished_at=? WHERE id=?", (status, json.dumps(result, ensure_ascii=False), stamp, stamp, job["id"]))
    conn.execute("UPDATE worker_state SET heartbeat=?,pid=?,busy=0 WHERE singleton=1", (time.time(), os.getpid()))
    conn.commit()


def process_job(conn: sqlite3.Connection, job: sqlite3.Row, codex: str) -> None:
    execution_policy = job["execution_policy"] if "execution_policy" in job.keys() else "LEGACY_UNRESTRICTED"
    codex_sandbox = {"READ_ONLY": "read-only", "WORKSPACE_WRITE": "workspace-write"}.get(execution_policy)
    if job["backend"] == "codex" and codex_sandbox is None:
        stamp = now()
        conn.execute(
            "UPDATE jobs SET status='failed',terminal_status='FAILED',updated_at=?,finished_at=?,last_exit_code=126,result_summary=? WHERE id=?",
            (stamp, stamp, "Refused: task has no supported sandbox execution policy.", job["id"]),
        )
        conn.commit()
        return
    try:
        execution_context = json.loads(job["execution_context_json"] or "{}")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Queued execution context is malformed; refusing worker start.") from exc
    if not isinstance(execution_context, dict):
        raise RuntimeError("Queued execution context is not an object; refusing worker start.")
    if execution_context.get("moduleExecutionContext") is not None:
        from modules.workspace_resolver import ModuleExecutionContext
        ModuleExecutionContext.validate(execution_context["moduleExecutionContext"], job["workspace"])
    root = APP_SUPPORT
    logs = root / "logs"
    secure_dir(logs)
    number = conn.execute("SELECT COALESCE(MAX(number),0)+1 FROM attempts WHERE job_id=?", (job["id"],)).fetchone()[0]
    log_path = logs / f"{job['id']}-{number}.jsonl"
    log_path.touch(mode=0o600, exist_ok=False)
    mode = job["pending_mode"]
    previous_session = job["last_session_id"]
    if mode == "resume":
        command = [codex, "exec", "resume", "--json", previous_session, "-"]
        prompt = (
            "Continue as the assigned System Agentic worker from this exact Codex session, under its original sandbox policy. "
            "Do not invoke provider dispatch or create child tasks. Inspect prior tool results and the current workspace before acting. "
            "Determine whether any side effect already happened; do not repeat a completed external or destructive action. "
            "Finish remaining work, verify the final state, and report any approval gate you cannot cross."
        )
    else:
        command = [codex, "exec", "--json", "--skip-git-repo-check", "--sandbox", codex_sandbox or "read-only", "--cd", job["workspace"], "-"]
        prompt = (
            "You are a dispatched System Agentic AI worker, not the interactive host/orchestrator. Follow the workspace instructions and task below. "
            "Work only within the enforced sandbox. Do not invoke provider dispatch, create child tasks, or try to expand permissions. "
            "If the objective requires broader access, stop and report that approval is required. Verify the result and do not claim completion without evidence.\n\n"
            f"Execution policy: {execution_policy}\n"
            f"Execution context (JSON): {json.dumps(execution_context, ensure_ascii=False, sort_keys=True)}\n"
            f"Task title: {job['title']}\n\nWorkspace: {job['workspace']}\n\nTask:\n{job['prompt']}"
        )
    attempt_id = conn.execute(
        "INSERT INTO attempts(job_id,number,mode,session_id,started_at,log_path) VALUES(?,?,?,?,?,?)",
        (job["id"], number, mode, previous_session if mode == "resume" else None, now(), str(log_path)),
    ).lastrowid
    conn.commit()
    if job["backend"] in {"local", "hybrid"}:
        exit_code: int | None = None
        try:
            import importlib
            local_agent = importlib.import_module("local_agent")
            local_agent = importlib.reload(local_agent)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps({"runtime": "attempt_started", "backend": job["backend"], "job_id": job["id"], "attempt": number, "mode": mode, "at": now()}, ensure_ascii=False) + "\n")
            exit_code = local_agent.run_local_job(conn, job, attempt_id, log_path, mode)
        except Exception as exc:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps({"runtime": "local_worker_error", "error": str(exc), "at": now()}, ensure_ascii=False) + "\n")
            exit_code = 1
        stamp = now()
        status, terminal = _job_terminal_state(exit_code, STOP_REQUESTED)
        conn.execute("UPDATE jobs SET status=?,terminal_status=?,updated_at=?,finished_at=?,last_exit_code=? WHERE id=?", (status, terminal, stamp, stamp, exit_code, job["id"]))
        conn.execute("UPDATE attempts SET finished_at=?,exit_code=? WHERE id=?", (stamp, exit_code, attempt_id))
        conn.commit()
        return
    env = os.environ.copy()
    env["CODEX_HOME"] = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["SYSTEM_AGENTIC_JOB_ID"] = job["id"]
    env["SYSTEM_AGENTIC_EXECUTION_ROLE"] = "worker"
    env["SYSTEM_AGENTIC_DISPATCH_DEPTH"] = str(execution_context.get("dispatch_depth", 1))
    env["SYSTEM_AGENTIC_EXECUTION_CONTEXT"] = json.dumps(execution_context, ensure_ascii=False, sort_keys=True)
    exit_code: int | None = None
    process: subprocess.Popen[str] | None = None
    output_thread: threading.Thread | None = None
    try:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"runtime": "attempt_started", "job_id": job["id"], "attempt": number, "mode": mode, "execution_policy": execution_policy, "execution_context": execution_context, "at": now()}, ensure_ascii=False) + "\n")
        process = subprocess.Popen(
            command,
            cwd=job["workspace"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdin is not None and process.stdout is not None

        def collect_output() -> None:
            reader_conn = database()
            with log_path.open("a", encoding="utf-8") as log:
                for line in process.stdout:  # type: ignore[union-attr]
                    log.write(line)
                    log.flush()
                    try:
                        event = json.loads(line)
                        if event.get("type") == "thread.started":
                            session_id = event.get("thread_id")
                            if session_id:
                                reader_conn.execute("UPDATE jobs SET last_session_id=?,updated_at=? WHERE id=?", (session_id, now(), job["id"]))
                                reader_conn.execute("UPDATE attempts SET session_id=? WHERE id=?", (session_id, attempt_id))
                                reader_conn.commit()
                        elif event.get("type") == "item.completed":
                            item = event.get("item", {})
                            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                                reader_conn.execute(
                                    "UPDATE jobs SET result_summary=?,updated_at=? WHERE id=?",
                                    (item["text"][-20_000:], now(), job["id"]),
                                )
                                reader_conn.commit()
                    except (json.JSONDecodeError, AttributeError):
                        pass
            reader_conn.close()

        output_thread = threading.Thread(target=collect_output, name=f"codex-output-{job['id']}", daemon=True)
        output_thread.start()
        process.stdin.write(prompt)
        process.stdin.close()
        while process.poll() is None:
            if STOP_REQUESTED:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                break
            time.sleep(0.25)
        exit_code = process.wait()
        output_thread.join(timeout=10)
    except Exception as exc:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if output_thread is not None:
            output_thread.join(timeout=10)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"runtime": "worker_error", "error": str(exc), "at": now()}, ensure_ascii=False) + "\n")
        exit_code = 127
    stamp = now()
    status, terminal = _job_terminal_state(exit_code, STOP_REQUESTED)
    conn.execute("UPDATE jobs SET status=?,terminal_status=?,updated_at=?,finished_at=?,last_exit_code=? WHERE id=?", (status, terminal, stamp, stamp, exit_code, job["id"]))
    conn.execute("UPDATE attempts SET finished_at=?,exit_code=? WHERE id=?", (stamp, exit_code, attempt_id))
    conn.commit()


def worker(args: argparse.Namespace) -> None:
    global STOP_REQUESTED
    secure_dir(APP_SUPPORT)
    lock_path = APP_SUPPORT / "worker.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError("Worker khác đang giữ khóa; không khởi chạy bản thứ hai.") from exc
    conn = database()
    stale = conn.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
    for row in stale:
        stamp = now()
        conn.execute("UPDATE jobs SET status='failed',terminal_status='FAILED_LOST',updated_at=?,finished_at=?,result_summary=? WHERE id=?", (stamp, stamp, "FAILED_LOST: worker disappeared before terminal result", row["id"]))
    conn.commit()
    conn.execute(
        "UPDATE tool_jobs SET status='failed',updated_at=?,finished_at=?,result_json=? WHERE status='running'",
        (now(), now(), json.dumps({"execution_status": "FAILED", "exit_code": None, "stdout": "", "stderr": "", "error": "worker restarted during execution"})),
    )
    conn.execute("INSERT OR REPLACE INTO worker_state(singleton,heartbeat,pid,busy) VALUES(1,?,?,0)", (time.time(), os.getpid()))
    conn.commit()
    codex = None
    def stop(_signum: int, _frame: object) -> None:
        global STOP_REQUESTED
        STOP_REQUESTED = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"Agent runtime ready; database={APP_SUPPORT / 'jobs.sqlite3'}", flush=True)
    while not STOP_REQUESTED:
        conn.execute("UPDATE worker_state SET heartbeat=?,pid=? WHERE singleton=1", (time.time(), os.getpid()))
        conn.commit()
        job = claim(conn)
        if job is None:
            time.sleep(POLL_SECONDS)
            continue
        if job["kind"] == "tool":
            process_tool_job(conn, job)
            continue
        if job["backend"] == "codex" and codex is None:
            try:
                codex = discover_codex()
            except RuntimeError as exc:
                stamp = now()
                conn.execute("UPDATE jobs SET status='failed',updated_at=?,finished_at=?,last_exit_code=127 WHERE id=?", (stamp, stamp, job["id"]))
                conn.execute("UPDATE worker_state SET heartbeat=?,pid=?,busy=0 WHERE singleton=1", (time.time(), os.getpid()))
                conn.commit()
                print(f"Task Codex {job['id']} failed: {exc}", file=sys.stderr, flush=True)
                continue
        process_job(conn, job, codex or "")
        conn.execute("UPDATE worker_state SET heartbeat=?,pid=?,busy=0 WHERE singleton=1", (time.time(), os.getpid()))
        conn.commit()
    conn.close()


def service(args: argparse.Namespace) -> None:
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    if args.action == "install":
        model_service = Path(__file__).resolve().parents[2] / "scripts" / "local-agent-model-service.py"
        subprocess.run([sys.executable, str(model_service), "install"], check=True)
        secure_dir(plist.parent)
        env_path = f"/opt/homebrew/bin:/usr/local/bin:{Path.home() / '.local/bin'}:/usr/bin:/bin:/usr/sbin:/sbin"
        env = {
            "HOME": str(Path.home()),
            "PATH": env_path,
            "CODEX_HOME": os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
        }
        payload = {
            "Label": LABEL,
            "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "worker"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "WorkingDirectory": str(Path.home()),
            "EnvironmentVariables": env,
            "StandardOutPath": str(APP_SUPPORT / "launchd.stdout.log"),
            "StandardErrorPath": str(APP_SUPPORT / "launchd.stderr.log"),
        }
        secure_dir(APP_SUPPORT)
        import plistlib
        new_contents = plistlib.dumps(payload)
        loaded = subprocess.run(["launchctl", "print", target], capture_output=True, text=True).returncode == 0
        if loaded:
            if plist.exists() and plist.read_bytes() == new_contents:
                print(f"LaunchAgent already installed and active: {target}; backend=local")
                return
            raise RuntimeError("LaunchAgent đang chạy với cấu hình cũ. Chạy service stop rồi service install để nạp cấu hình mới an toàn.")
        plist.write_bytes(new_contents)
        os.chmod(plist, 0o600)
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
            if state.returncode == 0 and "state = running" in state.stdout:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError(f"LaunchAgent đã nạp nhưng worker chưa vào trạng thái running: {state.stderr.strip()}")
        print(f"LaunchAgent installed and started: {target}; backend=local")
    elif args.action == "status":
        result = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
        conn = database()
        counts = {row["status"]: row["n"] for row in conn.execute("SELECT status,COUNT(*) AS n FROM jobs GROUP BY status")}
        model_service = Path(__file__).resolve().parents[2] / "scripts" / "local-agent-model-service.py"
        model_result = subprocess.run([sys.executable, str(model_service), "status"], capture_output=True, text=True)
        try:
            model_status = json.loads(model_result.stdout)
        except json.JSONDecodeError:
            model_status = {"error": model_result.stderr.strip() or model_result.stdout.strip()}
        print(json.dumps({"service": LABEL, "loaded": result.returncode == 0, "launchctl": result.stdout[:5000] if result.returncode == 0 else result.stderr.strip(), "local_model": model_status, "jobs": counts, "database": str(APP_SUPPORT / "jobs.sqlite3")}, ensure_ascii=False, indent=2))
    elif args.action == "stop":
        subprocess.run(["launchctl", "bootout", target], check=False)
        print(f"LaunchAgent stopped: {target}")
    elif args.action == "uninstall":
        subprocess.run(["launchctl", "bootout", target], check=False)
        plist.unlink(missing_ok=True)
        print("LaunchAgent uninstalled; task database and logs retained.")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Persistent local queue for System Agentic AI tasks")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("submit", help="queue a task")
    s.add_argument("prompt", nargs="?", default="")
    s.add_argument("--file")
    s.add_argument("--workspace", required=True)
    s.add_argument("--title")
    s.add_argument("--backend", choices=("local", "hybrid", "codex"), default="local", help="local is offline; hybrid explicitly sends bounded browser metadata to TypeSafe; codex uses OpenAI")
    s.add_argument("--task-id", help="optional caller-assigned unique task ID for reconciliation")
    s.add_argument("--execution-policy", choices=("READ_ONLY", "WORKSPACE_WRITE", "ELEVATED"), help="Codex sandbox or local workspace mutation policy")
    s.add_argument("--execution-context", default="{}", help="provider-neutral dispatch origin/role/depth metadata as JSON")
    s.set_defaults(func=submit)
    l = sub.add_parser("list", help="list tasks")
    l.add_argument("--limit", type=int, default=50)
    l.set_defaults(func=show_jobs)
    st = sub.add_parser("status", help="show a task")
    st.add_argument("id")
    st.add_argument("--json", dest="json_output", action="store_true", help="emit one machine-readable object including attempts")
    st.set_defaults(func=show_jobs)
    lg = sub.add_parser("logs", help="show task output")
    lg.add_argument("id")
    lg.set_defaults(func=show_logs)
    rs = sub.add_parser("resume", help="continue an interrupted/failed Codex session")
    rs.add_argument("id")
    rs.add_argument("--new-session", action="store_true")
    rs.set_defaults(func=resume)
    ca = sub.add_parser("cancel", help="cancel a queued task")
    ca.add_argument("id")
    ca.set_defaults(func=cancel)
    pr = sub.add_parser("providers", help="show configured execution providers without exposing credentials")
    pr.set_defaults(func=lambda _args: print(json.dumps(provider_inventory(), ensure_ascii=False, indent=2)))
    wk = sub.add_parser("worker", help="run queue worker")
    wk.set_defaults(func=worker)
    sv = sub.add_parser("service", help="manage the per-user LaunchAgent")
    sv.add_argument("action", choices=["install", "status", "stop", "uninstall"])
    sv.set_defaults(func=service)
    return p


def main() -> int:
    if sys.version_info < (3, 11):
        print("Yêu cầu Python 3.11 trở lên; dùng ./scripts/agentic-runtime để tự chọn phiên bản phù hợp.", file=sys.stderr)
        return 2
    args = parser().parse_args()
    try:
        args.func(args)
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(f"Lỗi: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
