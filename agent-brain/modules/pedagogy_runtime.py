"""Ownership-checked lifecycle for the official local Pedagogy service."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit


WORKBENCH_ROOT = Path(__file__).resolve().parents[2]
PEDAGOGY_ROOT = WORKBENCH_ROOT / "design-os-pedagogy"
ENTRYPOINT = PEDAGOGY_ROOT / "run_rehearsal.py"
DEFAULT_URL = "http://127.0.0.1:8876"
STATE_ROOT = Path.home() / "Library" / "Application Support" / "SystemAgenticAI" / "runtime" / "pedagogy"
STARTUP_TIMEOUT = 12.0
LOCK_TIMEOUT = 5.0
HEALTH_TIMEOUT = 0.5
_URL_LINE = re.compile(r"Agent Teacher: http://127\.0\.0\.1:(\d+)")
_PROCESS_HANDLES: dict[int, subprocess.Popen[str]] = {}


class PedagogyRuntimeUnavailable(RuntimeError):
    """Managed Pedagogy service could not be started or verified."""


def _compatible_python() -> Path | None:
    candidates: list[str] = []
    for minor in (13, 12, 11, 10):
        name = f"python3.{minor}"
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.extend((sys.executable, str(PEDAGOGY_ROOT / ".venv" / "bin" / "python")))
    seen: set[str] = set()
    for candidate in candidates:
        try:
            executable = Path(candidate).expanduser().resolve(strict=True)
        except OSError:
            continue
        if str(executable) in seen or not executable.is_file() or not os.access(executable, os.X_OK):
            continue
        seen.add(str(executable))
        try:
            result = subprocess.run(
                [str(executable), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        try:
            major, minor = (int(part) for part in result.stdout.strip().split(".", 1))
        except (ValueError, TypeError):
            continue
        if (major, minor) >= (3, 10):
            return executable
    return None


def _health(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        return False
    request = urllib.request.Request(url.rstrip("/") + "/api/status", method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=HEALTH_TIMEOUT) as response:
            payload = json.loads(response.read(4096))
        return response.status == 200 and payload.get("mode") == "authored_rehearsal"
    except (OSError, urllib.error.URLError, TimeoutError, ValueError, AttributeError):
        return False


def _process_command(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _owned(metadata: dict[str, object]) -> bool:
    pid = metadata.get("pid")
    port = metadata.get("port")
    command_port = metadata.get("command_port", 0)
    state_root = metadata.get("state_root")
    interpreter = metadata.get("interpreter")
    if (type(pid) is not int or type(port) is not int or type(command_port) is not int or not isinstance(state_root, str)
            or not isinstance(interpreter, str)
            or metadata.get("started_by") != "system-agentic-domain-runtime"):
        return False
    command = _process_command(pid)
    return bool(
        command
        and str(ENTRYPOINT) in command
        and str(Path(interpreter).resolve(strict=False)) in command
        and "--data-dir" in command
        and str(Path(state_root).resolve(strict=False)) in command
        and "--port" in command
        and str(command_port) in command
    )


def _read_metadata(root: Path) -> dict[str, object] | None:
    try:
        value = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("module") == "pedagogy" else None


def _state_root(root: Path) -> Path:
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = root.resolve(strict=True)
        if root.is_symlink() or not resolved.is_dir():
            raise PedagogyRuntimeUnavailable("Pedagogy runtime state root is not a safe directory.")
        os.chmod(resolved, 0o700)
        return resolved
    except PedagogyRuntimeUnavailable:
        raise
    except OSError as exc:
        raise PedagogyRuntimeUnavailable(f"Pedagogy runtime state root is unavailable: {type(exc).__name__}.") from exc


def _write_metadata(root: Path, metadata: dict[str, object]) -> None:
    target = root / "runtime.json"
    temporary = root / f".runtime-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _runtime_lock(root: Path) -> Iterator[None]:
    lock_path = root / "runtime.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + LOCK_TIMEOUT
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PedagogyRuntimeUnavailable("Timed out waiting for the Pedagogy runtime lock.") from None
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _managed_url(metadata: dict[str, object]) -> str | None:
    if not _owned(metadata):
        return None
    url = metadata.get("endpoint")
    return url if isinstance(url, str) and _health(url) else None


def inspect_runtime(root: Path = STATE_ROOT) -> dict[str, object]:
    """Probe current service without starting or stopping anything."""
    try:
        resolved = root.expanduser().resolve(strict=True)
    except OSError:
        resolved = None
    if resolved is not None:
        metadata = _read_metadata(resolved)
        if metadata:
            url = _managed_url(metadata)
            if url:
                return {**metadata, "health": "healthy", "endpoint": url, "managed": True}
    if _health(DEFAULT_URL):
        return {
            "module": "pedagogy", "health": "healthy", "endpoint": DEFAULT_URL,
            "managed": False, "started_by": "pre-existing-local-service",
            "state_root": None, "db_path": None,
        }
    return {"module": "pedagogy", "health": "inactive", "endpoint": None, "managed": False}


def is_startable() -> tuple[bool, str | None]:
    interpreter = _compatible_python()
    if interpreter is None:
        return False, "No installed Python 3.10+ interpreter is available for Pedagogy."
    if not ENTRYPOINT.is_file():
        return False, f"Official Pedagogy entrypoint is missing: {ENTRYPOINT}"
    return True, str(interpreter)


def _stop_owned(metadata: dict[str, object]) -> bool:
    if not _owned(metadata):
        return False
    pid = metadata["pid"]
    try:
        # The official entrypoint catches KeyboardInterrupt and closes its HTTP server.
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    process = _PROCESS_HANDLES.get(pid)
    if process is not None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if _owned(metadata):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    return False
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if not _owned(metadata):
                    return False
                try:
                    os.kill(pid, signal.SIGKILL)
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    return False
        finally:
            _PROCESS_HANDLES.pop(pid, None)
        return True
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if _process_command(pid) is None:
            return True
        time.sleep(0.05)
    # Only signal again while the exact managed command still owns this PID.
    if _owned(metadata):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if _process_command(pid) is None:
            return True
        time.sleep(0.05)
    if _owned(metadata):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return False
    return _process_command(pid) is None


def stop_managed(root: Path = STATE_ROOT) -> bool:
    """Stop only the process whose PID and command match our metadata."""
    resolved = _state_root(root.expanduser())
    with _runtime_lock(resolved):
        metadata = _read_metadata(resolved)
        if not metadata or not _owned(metadata):
            return False
        stopped = _stop_owned(metadata)
        if stopped:
            try:
                (resolved / "runtime.json").unlink()
            except FileNotFoundError:
                pass
        return stopped


def ensure_runtime(root: Path = STATE_ROOT) -> dict[str, object]:
    """Reuse a healthy service or start one official loopback instance."""
    ready, interpreter_value = is_startable()
    if not ready or not interpreter_value:
        raise PedagogyRuntimeUnavailable(interpreter_value or "Official Pedagogy service is not startable.")
    interpreter = Path(interpreter_value)
    resolved = _state_root(root.expanduser())
    with _runtime_lock(resolved):
        metadata = _read_metadata(resolved)
        if metadata:
            url = _managed_url(metadata)
            if url:
                return {**metadata, "endpoint": url, "health": "healthy", "reused": True}
            if _owned(metadata):
                if not _stop_owned(metadata):
                    raise PedagogyRuntimeUnavailable("Owned Pedagogy runtime is unhealthy and could not be stopped safely.")
            try:
                (resolved / "runtime.json").unlink()
            except FileNotFoundError:
                pass

        # Preserve an already running official default service, but never claim its DB is confined.
        if _health(DEFAULT_URL):
            return {
                "module": "pedagogy", "pid": None, "host": "127.0.0.1", "port": 8876,
                "endpoint": DEFAULT_URL, "started_by": "pre-existing-local-service",
                "started_at": None, "state_root": None, "interpreter": None,
                "db_path": None, "health": "healthy", "managed": False, "reused": True,
            }

        db_path = resolved / "rehearsal.sqlite3"
        command = [str(interpreter), "-B", "-u", str(ENTRYPOINT), "--port", "0", "--data-dir", str(resolved)]
        log_path = resolved / "service.log"
        try:
            with open(log_path, "w", encoding="utf-8") as log_file:
                os.chmod(log_path, 0o600)
                process = subprocess.Popen(
                    command, cwd=PEDAGOGY_ROOT, stdin=subprocess.DEVNULL,
                    stdout=log_file, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
        except OSError as exc:
            raise PedagogyRuntimeUnavailable(f"Could not start official Pedagogy service: {type(exc).__name__}.") from exc

        deadline = time.monotonic() + STARTUP_TIMEOUT
        endpoint: str | None = None
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    detail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
                    raise PedagogyRuntimeUnavailable(
                        f"Official Pedagogy process exited during startup ({process.returncode}): {detail.strip()}"
                    )
                try:
                    startup_log = log_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    startup_log = ""
                match = _URL_LINE.search(startup_log)
                if match:
                    endpoint = f"http://127.0.0.1:{int(match.group(1))}"
                    break
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            if endpoint is None:
                raise PedagogyRuntimeUnavailable("Timed out waiting for the official Pedagogy startup endpoint.")
            if not _health(endpoint):
                while time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.1)
                    if _health(endpoint):
                        break
            if process.poll() is not None or not _health(endpoint):
                raise PedagogyRuntimeUnavailable("Official Pedagogy process did not pass /api/status health check.")
            metadata: dict[str, object] = {
                "module": "pedagogy", "pid": process.pid, "host": "127.0.0.1",
                "port": int(urlsplit(endpoint).port or 0), "command_port": 0, "endpoint": endpoint,
                "started_by": "system-agentic-domain-runtime",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "state_root": str(resolved), "interpreter": str(interpreter),
                "db_path": str(db_path), "health": "healthy", "managed": True,
                "command": command, "log_path": str(log_path),
            }
            _write_metadata(resolved, metadata)
            _PROCESS_HANDLES[process.pid] = process
            return {**metadata, "reused": False, "startup_seconds": round(time.monotonic() - (deadline - STARTUP_TIMEOUT), 3)}
        except Exception:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            raise
