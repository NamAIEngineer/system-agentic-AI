"""Strict, model-free executor for typed Tool Runtime command requests."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any


MAX_OUTPUT_BYTES = 256_000
TRUNCATION_MARKER = "\n[combined stdout/stderr exceeded the configured capture limit]\n"
MAX_COMMAND_SECONDS = 120
SAFE_EXEC_PATHS = (
    Path("/usr/bin"), Path("/bin"), Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"), Path.home() / ".local/bin",
    Path.home() / ".local/share/uv/python",
)


def _subpath(path: Path) -> str:
    return f"(subpath {json.dumps(str(path))})"


def _resolve_argv(argv: Any, workspace: Path) -> list[str]:
    if not isinstance(argv, list) or not 1 <= len(argv) <= 32:
        raise ValueError("argv must contain 1-32 strings")
    if any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 2048 for arg in argv):
        raise ValueError("argv values must be bounded strings without NUL")
    command = list(argv)
    exe = Path(command[0])
    if not exe.is_absolute() and len(exe.parts) == 1:
        found = shutil.which(command[0])
        if found is None:
            raise ValueError("executable is unavailable")
        exe = Path(found)
    workspace_executable = not exe.is_absolute() and len(exe.parts) > 1
    try:
        if workspace_executable:
            exe = workspace / exe
        resolved = exe.resolve(strict=True)
    except OSError as exc:
        raise ValueError("executable does not exist") from exc
    in_workspace = resolved.is_relative_to(workspace)
    approved_system_path = any(resolved.is_relative_to(root.resolve()) for root in SAFE_EXEC_PATHS if root.exists())
    if not (in_workspace or approved_system_path):
        raise ValueError("executable is outside approved system/tool paths")
    command[0] = str(resolved)
    return command


def _capture(pipe: Any, result: bytearray, exceeded: list[bool], total: list[int], lock: threading.Lock) -> None:
    while True:
        chunk = pipe.read(64 * 1024)
        if not chunk:
            return
        with lock:
            remaining = max(0, MAX_OUTPUT_BYTES - len(TRUNCATION_MARKER.encode()) - total[0])
            if remaining:
                result.extend(chunk[:remaining])
                total[0] += min(remaining, len(chunk))
            if len(chunk) > remaining:
                exceeded[0] = True


def execute_command(request: dict[str, Any], *, should_cancel=lambda: False) -> dict[str, Any]:
    """Execute one validated argv under a fresh strict Seatbelt profile."""
    if not isinstance(request, dict) or set(request) != {"tool", "workspace", "execution_policy", "arguments"}:
        raise ValueError("malformed command request")
    workspace = Path(request.get("workspace", "")).resolve(strict=True)
    policy = request.get("execution_policy")
    if not workspace.is_dir():
        raise ValueError("assigned workspace must be an existing directory")
    if policy == "ELEVATED":
        raise ValueError("ELEVATED_REQUIRES_APPROVAL")
    if policy not in {"READ_ONLY", "WORKSPACE_WRITE"}:
        raise ValueError("unsupported execution_policy")
    tool = request.get("tool")
    if tool not in {"workspace_exec", "run_tests"}:
        raise ValueError("unsupported typed command")
    args = request.get("arguments")
    if not isinstance(args, dict) or set(args) - {"argv", "timeout_seconds"}:
        raise ValueError("malformed command arguments")
    timeout = args.get("timeout_seconds", 30)
    if type(timeout) is not int or not 1 <= timeout <= MAX_COMMAND_SECONDS:
        raise ValueError("timeout_seconds outside allowed range")
    argv = _resolve_argv(args.get("argv"), workspace)
    sandbox = shutil.which("sandbox-exec")
    if sandbox is None:
        raise RuntimeError("OS sandbox sandbox-exec is unavailable; refusing unsandboxed execution")

    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    scratch = tempfile.TemporaryDirectory(prefix="system-agentic-tool-", dir=temp_root)
    scratch_path = Path(scratch.name).resolve(strict=True)
    system_read_roots = (
        Path("/System"), Path("/usr"), Path("/bin"), Path("/sbin"),
        # macOS 15 stores the active dyld shared cache in the read-only OS
        # Cryptex, outside the visible /System subtree. Without this read-only
        # path, sandboxed native executables abort in dyld before main().
        Path("/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld"),
        Path("/Library/Developer"), Path("/opt/homebrew"),
        Path("/usr/local/bin"), Path("/usr/local/lib"), Path("/usr/local/Cellar"),
        Path.home() / ".local/bin", Path.home() / ".local/share/uv/python",
    )
    readable = [workspace, scratch_path, *(root.resolve() for root in system_read_roots if root.exists())]
    writable = [scratch_path]
    if policy == "WORKSPACE_WRITE":
        writable.insert(0, workspace)
    profile = "\n".join((
        "(version 1)", "(deny default)", "(allow process*)",
        f'(allow file-read* {" ".join(_subpath(path) for path in readable)})',
        # dyld on current macOS probes these two system values and opens the
        # root directory while locating the shared cache. Scope sysctl access
        # to the observed names and allow only the root directory itself.
        '(allow sysctl-read (sysctl-name "kern.bootargs" "security.mac.lockdown_mode_state"))',
        '(allow file-read* (literal "/"))',
        f'(allow file-map-executable {" ".join(_subpath(path) for path in readable)})',
        f'(allow file-write* {" ".join(_subpath(path) for path in writable)})',
        "(deny network-outbound)", "(deny network-inbound)",
    ))
    env = {
        "PATH": ":".join(str(p) for p in SAFE_EXEC_PATHS),
        "HOME": str(workspace), "TMPDIR": str(scratch_path) + "/",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "GIT_TERMINAL_PROMPT": "0",
        "SYSTEM_AGENTIC_EXECUTION_ROLE": "tool-executor",
        "SYSTEM_AGENTIC_EXECUTION_POLICY": policy,
    }
    try:
        process = subprocess.Popen(
            [sandbox, "-p", profile, *argv], cwd=workspace, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        scratch.cleanup()
        return _result(tool, "FAILED", None, b"", str(exc).encode(), False)

    stdout = bytearray()
    stderr = bytearray()
    exceeded = [False]
    total = [0]
    lock = threading.Lock()
    out_thread = threading.Thread(target=_capture, args=(process.stdout, stdout, exceeded, total, lock), daemon=True)
    err_thread = threading.Thread(target=_capture, args=(process.stderr, stderr, exceeded, total, lock), daemon=True)
    out_thread.start()
    err_thread.start()
    timed_out = False
    try:
        deadline = __import__("time").monotonic() + timeout
        while process.poll() is None:
            if should_cancel():
                break
            remaining = deadline - __import__("time").monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                process.wait(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                continue
        if timed_out or should_cancel():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    except Exception:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        scratch.cleanup()
        raise
    out_thread.join(timeout=2)
    err_thread.join(timeout=2)
    if out_thread.is_alive() or err_thread.is_alive():
        for pipe in (process.stdout, process.stderr):
            if pipe:
                pipe.close()
        out_thread.join(timeout=1)
        err_thread.join(timeout=1)
    execution_status = "TIMED_OUT" if timed_out else "COMPLETED"
    code = None if timed_out else process.returncode
    error = None
    if code is not None and code < 0 and not timed_out:
        signal_number = -code
        execution_status = "FAILED"
        error = f"command terminated by signal {signal_number}"
        text = f"\n[execution error: {error}]\n".encode()
        stderr.extend(text)
    scratch.cleanup()
    return _result(tool, execution_status, code, bytes(stdout), bytes(stderr), exceeded[0], error=error)


def _result(tool: str, status: str, code: int | None, stdout: bytes, stderr: bytes, truncated: bool,
            *, error: str | None = None) -> dict[str, Any]:
    text_out = stdout.decode("utf-8", errors="replace")
    text_err = stderr.decode("utf-8", errors="replace")
    if truncated:
        text_err += TRUNCATION_MARKER
    result: dict[str, Any] = {
        "execution_status": status,
        "exit_code": code,
        "stdout": text_out,
        "stderr": text_err,
        "capture_truncated": truncated,
    }
    if error is not None:
        result["error"] = error
    if tool == "run_tests":
        result["test_outcome"] = "PASSED" if status == "COMPLETED" and code == 0 else "FAILED"
    return result
