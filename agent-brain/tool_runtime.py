"""Provider-neutral, workspace-bounded tool execution runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from typing import Protocol
import time
import uuid
from pathlib import Path
from typing import Any

from optimization import optimize_text, retrieve_recovered_text


MAX_FILE_BYTES = 128_000
MAX_TRAVERSAL_FILES = 10_000
MAX_TRAVERSAL_SECONDS = 2
MAX_COMMAND_OUTPUT_BYTES = 256_000
MAX_COMMAND_SECONDS = 120
EXCLUDED_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next", ".cache"}
BLOCKED_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


class ToolRuntimeError(ValueError):
    pass


class ExecutionBackend(Protocol):
    def execute(self, request: dict[str, Any]) -> dict[str, Any]: ...


class InlineExecutionBackend:
    """Owns bounded in-process workspace file operations only."""

    def __init__(self, runtime: "ToolRuntime") -> None:
        self.runtime = runtime

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        name, args = request["tool"], request["arguments"]
        if name == "workspace_list":
            output = self.runtime._list(args.get("path", "."))
        elif name == "workspace_read":
            output = self.runtime._read(args["path"])
        elif name == "workspace_search":
            output = self.runtime._search(args["query"], args.get("path", "."))
        else:
            raise ToolRuntimeError("inline backend only supports workspace file operations")
        return {"execution_status": "COMPLETED", "exit_code": 0, "stdout": output, "stderr": ""}


class UnavailableExecutionBackend:
    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("typed queue execution backend is not configured; refusing inline execution")


TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "workspace_list",
        "description": "List files in the assigned workspace, skipping generated and dependency folders.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "workspace_read",
        "description": "Read a UTF-8 text file in the assigned workspace. Credential and key paths are blocked.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "workspace_search",
        "description": "Search text in UTF-8 files in the assigned workspace; returns bounded path and line matches.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "minLength": 1}, "path": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "workspace_exec",
        "description": "Run a bounded local command as argv without a shell. Network is denied; writes are limited by execution policy.",
        "inputSchema": {"type": "object", "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 32},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_COMMAND_SECONDS},
        }, "required": ["argv"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "run_tests",
        "description": "Run a deterministic bounded test command as argv. Test outcome is reported separately from execution health.",
        "inputSchema": {"type": "object", "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 32},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_COMMAND_SECONDS},
        }, "required": ["argv"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "tool_recover",
        "description": "Retrieve exact original output for a compacted result from this MCP process.",
        "inputSchema": {"type": "object", "properties": {"recovery_reference": {"type": "string", "minLength": 1}}, "required": ["recovery_reference"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
)
_DEFINITIONS = {item["name"]: item for item in TOOL_DEFINITIONS}


def _is_secret(path: Path) -> bool:
    return (
        path.name.lower() in BLOCKED_NAMES
        or path.suffix.lower() in BLOCKED_SUFFIXES
        or any(part.lower() in {".ssh", ".gnupg", ".aws", ".config"} for part in path.parts)
    )


def _workspace_path(root: Path, value: str, *, allow_root: bool = False) -> Path:
    if not isinstance(value, str) or "\x00" in value:
        raise ToolRuntimeError("path must be relative text without NUL")
    raw = Path(value or ".")
    if raw.is_absolute():
        raise ToolRuntimeError("absolute paths are not allowed")
    root = root.resolve(strict=True)
    try:
        target = (root / raw).resolve(strict=True)
        target.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ToolRuntimeError("path does not exist inside the assigned workspace") from exc
    if not allow_root and _is_secret(target):
        raise ToolRuntimeError("access to credential/key paths is blocked")
    if ".git" in raw.parts:
        raise ToolRuntimeError(".git internals are blocked")
    return target


def _validate_arguments(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(name, str):
        raise ToolRuntimeError("tool name must be text")
    definition = _DEFINITIONS.get(name)
    if definition is None:
        raise ToolRuntimeError(f"unknown tool: {name}")
    if not isinstance(arguments, dict):
        raise ToolRuntimeError("tool arguments must be an object")
    schema = definition["inputSchema"]
    properties = schema["properties"]
    unknown = set(arguments) - set(properties)
    if unknown:
        raise ToolRuntimeError(f"unknown argument: {sorted(unknown)[0]}")
    missing = set(schema.get("required", ())) - set(arguments)
    if missing:
        raise ToolRuntimeError(f"missing required argument: {sorted(missing)[0]}")
    for key, value in arguments.items():
        spec = properties[key]
        kind = spec["type"]
        valid = (isinstance(value, str) if kind == "string" else
                 isinstance(value, list) if kind == "array" else
                 isinstance(value, int) and not isinstance(value, bool) if kind == "integer" else False)
        if not valid:
            raise ToolRuntimeError(f"{key} must be {kind}")
        if kind == "string" and "minLength" in spec and len(value) < spec["minLength"]:
            raise ToolRuntimeError(f"{key} must not be empty")
        if kind == "array":
            if len(value) < spec.get("minItems", 0) or len(value) > spec.get("maxItems", 1_000):
                raise ToolRuntimeError(f"{key} has an invalid item count")
            if any(not isinstance(item, str) or "\x00" in item or len(item) > 2048 for item in value):
                raise ToolRuntimeError(f"{key} items must be bounded strings without NUL")
        if kind == "integer" and not spec.get("minimum", -1_000_000) <= value <= spec.get("maximum", 1_000_000):
            raise ToolRuntimeError(f"{key} is outside the allowed range")
    return arguments


class ToolRuntime:
    """Executes a small tool registry under an explicit local OS sandbox policy."""

    def __init__(self, workspace: str | Path, execution_policy: str = "READ_ONLY", *,
                 inline_backend: ExecutionBackend | None = None,
                 worker_backend: ExecutionBackend | None = None,
                 worker_wait_seconds: int = 130) -> None:
        if execution_policy == "ELEVATED":
            raise ToolRuntimeError("ELEVATED_REQUIRES_APPROVAL")
        if execution_policy not in {"READ_ONLY", "WORKSPACE_WRITE"}:
            raise ToolRuntimeError("execution_policy must be READ_ONLY or WORKSPACE_WRITE")
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ToolRuntimeError("workspace must be an existing directory")
        self.execution_policy = execution_policy
        self._recoveries: dict[str, tuple[str, str]] = {}
        self.inline_backend = inline_backend or InlineExecutionBackend(self)
        self.worker_backend = worker_backend or UnavailableExecutionBackend()
        self.worker_wait_seconds = worker_wait_seconds

    def list_tools(self) -> list[dict[str, Any]]:
        tools = [json.loads(json.dumps(item)) for item in TOOL_DEFINITIONS]
        # READ_ONLY permits writes only to ephemeral temp storage and never to
        # the assigned workspace. In WORKSPACE_WRITE mode the host must retain
        # its normal approval gate for command execution.
        if self.execution_policy != "READ_ONLY":
            for tool_name in ("workspace_exec", "run_tests"):
                next(item for item in tools if item["name"] == tool_name).pop("annotations", None)
        return tools

    def materialize_artifact(self, relative_path: str, content: bytes) -> dict[str, Any]:
        """Store an explicit artifact without overwriting; internal callers share tool policy.

        This is a file operation, not a new MCP tool or a command execution backend.
        """
        if self.execution_policy != "WORKSPACE_WRITE":
            raise ToolRuntimeError("Artifact materialization requires explicit WORKSPACE_WRITE")
        raw = Path(relative_path)
        if (raw.is_absolute() or not raw.parts or any(p in {"..", ".git"} for p in raw.parts)
                or _is_secret(raw) or not isinstance(content, bytes) or len(content) > MAX_FILE_BYTES):
            raise ToolRuntimeError("Artifact path/content violates workspace bounds")
        parent = self.workspace
        # Use directory descriptors and O_NOFOLLOW to avoid symlink races while creating parents.
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in raw.parts[:-1]:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(raw.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(directory)
        return {"path": str(self.workspace / raw), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}

    def call(self, name: str, arguments: Any, *, trace_context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = dict(trace_context or {})
        context.setdefault("adapter", "local")
        context.setdefault("caller", "unknown")
        context["_started_monotonic"] = time.monotonic()
        try:
            args = _validate_arguments(name, arguments)
            if name in {"workspace_list", "workspace_read", "workspace_search"}:
                raw_result = self.inline_backend.execute({"tool": name, "arguments": args})
                content_type = "json" if name in {"workspace_list", "workspace_search"} else "terminal"
                return self._normalize_backend_result(name, raw_result, content_type=content_type, trace_context=context)
            if name in {"workspace_exec", "run_tests"}:
                request = {"tool": name, "workspace": str(self.workspace),
                           "execution_policy": self.execution_policy, "arguments": args,
                           "wait_seconds": self.worker_wait_seconds}
                raw_result = self.worker_backend.execute(request)
                return self._normalize_backend_result(name, raw_result, trace_context=context)
            if name == "tool_recover":
                return self._recover(args["recovery_reference"], trace_context=context)
            raise ToolRuntimeError(f"unknown tool: {name}")
        except (ToolRuntimeError, OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
            status = "TIMED_OUT" if isinstance(exc, TimeoutError) else "FAILED" if isinstance(exc, (RuntimeError, subprocess.SubprocessError)) else ("REFUSED" if isinstance(exc, ToolRuntimeError) else "FAILED")
            return self._normalized(
                name, status, None, str(exc),
                error=str(exc), trace_context=context,
            )

    def _list(self, relative: str) -> str:
        start = _workspace_path(self.workspace, relative, allow_root=True)
        if not start.is_dir():
            raise ToolRuntimeError("path is not a directory")
        files: list[str] = []
        visited = 0
        started = time.monotonic()
        truncated = False
        for candidate in self._walk_candidates(start):
            if time.monotonic() - started >= MAX_TRAVERSAL_SECONDS:
                truncated = True
                break
            if candidate is None:
                continue
            if visited >= MAX_TRAVERSAL_FILES:
                truncated = True
                break
            visited += 1
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(self.workspace)
            except (OSError, ValueError):
                if time.monotonic() - started >= MAX_TRAVERSAL_SECONDS:
                    truncated = True
                    break
                continue
            if not _is_secret(resolved):
                files.append(str(resolved.relative_to(self.workspace)))
                if len(files) == 250:
                    truncated = True
                    break
            if time.monotonic() - started >= MAX_TRAVERSAL_SECONDS:
                truncated = True
                break
        return json.dumps({"files": files, "visited_files": visited, "truncated": truncated})

    @staticmethod
    def _walk_candidates(start: Path):
        """Yield file candidates incrementally, with directory markers for time checks."""
        if start.is_file():
            yield start
            return
        stack: list[tuple[Path, Any]] = []

        def descend(directory: Path) -> bool:
            try:
                stack.append((directory, os.scandir(directory)))
                return True
            except OSError:
                return False

        if not descend(start):
            return
        try:
            yield None
            while stack:
                current, entries = stack[-1]
                try:
                    entry = next(entries)
                except StopIteration:
                    entries.close()
                    stack.pop()
                    continue
                except OSError:
                    entries.close()
                    stack.pop()
                    continue
                candidate = current / entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in EXCLUDED_DIRS and descend(candidate):
                            yield None
                        continue
                    if entry.is_symlink() and entry.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                yield candidate
        finally:
            for _, entries in stack:
                entries.close()

    def _read(self, relative: str) -> str:
        path = _workspace_path(self.workspace, relative)
        if not path.is_file():
            raise ToolRuntimeError("path is not a file")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ToolRuntimeError(f"file exceeds {MAX_FILE_BYTES} bytes")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolRuntimeError("file is not UTF-8 text") from exc

    def _search(self, query: str, relative: str) -> str:
        start = _workspace_path(self.workspace, relative, allow_root=True)
        if not query:
            raise ToolRuntimeError("query must not be empty")
        matches = []
        needle = query.casefold()
        visited = 0
        started = time.monotonic()
        truncated = False
        for candidate in self._walk_candidates(start):
            if time.monotonic() - started >= MAX_TRAVERSAL_SECONDS:
                truncated = True
                break
            if candidate is None:
                continue
            if visited >= MAX_TRAVERSAL_FILES:
                truncated = True
                break
            visited += 1
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(self.workspace)
                if _is_secret(resolved) or resolved.stat().st_size > MAX_FILE_BYTES:
                    if time.monotonic() - started >= MAX_TRAVERSAL_SECONDS:
                        truncated = True
                        break
                    continue
                for line_no, line in enumerate(resolved.read_text(encoding="utf-8").splitlines(), 1):
                    if needle in line.casefold():
                        matches.append({"path": str(resolved.relative_to(self.workspace)), "line": line_no, "text": line[:500]})
                        if len(matches) == 100:
                            truncated = True
                            break
            except (OSError, UnicodeDecodeError, ValueError):
                if time.monotonic() - started >= MAX_TRAVERSAL_SECONDS:
                    truncated = True
                    break
                continue
            if len(matches) == 100:
                break
            if time.monotonic() - started >= MAX_TRAVERSAL_SECONDS:
                truncated = True
                break
        return json.dumps({"matches": matches, "visited_files": visited, "truncated": truncated}, ensure_ascii=False)

    def _normalize_backend_result(self, name: str, raw_result: dict[str, Any], *,
                                  content_type: str = "terminal",
                                  trace_context: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw_result, dict):
            raise ToolRuntimeError("execution backend returned a malformed result")
        stdout, stderr = raw_result.get("stdout", ""), raw_result.get("stderr", "")
        status, code = raw_result.get("execution_status"), raw_result.get("exit_code")
        if (not isinstance(stdout, str) or not isinstance(stderr, str)
                or not isinstance(status, str) or status not in {"COMPLETED", "TIMED_OUT", "FAILED"}
                or (code is not None and (not isinstance(code, int) or isinstance(code, bool)))):
            raise ToolRuntimeError("execution backend returned invalid process fields")
        raw = stdout + (("\n[stderr]\n" if stdout else "") + stderr if stderr else "")
        error = raw_result.get("error")
        if error is not None and not isinstance(error, str):
            raise ToolRuntimeError("execution backend returned invalid error detail")
        normalized = self._normalized(name, status, code, raw, content_type=content_type,
                                      error=error, trace_context=trace_context)
        normalized["stdout_bytes"] = len(stdout.encode("utf-8"))
        normalized["stderr_bytes"] = len(stderr.encode("utf-8"))
        normalized["capture_truncated"] = bool(raw_result.get("capture_truncated", False))
        if "test_outcome" in raw_result:
            if not isinstance(raw_result["test_outcome"], str) or raw_result["test_outcome"] not in {"PASSED", "FAILED"}:
                raise ToolRuntimeError("execution backend returned invalid test outcome")
            normalized["test_outcome"] = raw_result["test_outcome"]
        for field in ("backend", "queue_job_id", "workspace"):
            value = raw_result.get(field)
            if value is not None:
                if not isinstance(value, str):
                    raise ToolRuntimeError(f"execution backend returned invalid {field}")
                normalized[field] = value
        duration_ms = raw_result.get("duration_ms")
        if duration_ms is not None:
            if not isinstance(duration_ms, (int, float)) or isinstance(duration_ms, bool) or duration_ms < 0:
                raise ToolRuntimeError("execution backend returned invalid duration_ms")
            normalized["execution_duration_ms"] = round(duration_ms, 2)
        if "warnings" in raw_result:
            if not isinstance(raw_result["warnings"], list) or any(not isinstance(item, str) for item in raw_result["warnings"]):
                raise ToolRuntimeError("execution backend returned invalid warnings")
            normalized["warnings"] = raw_result["warnings"]
        if name == "workspace_exec" and status == "COMPLETED":
            normalized["command_outcome"] = "PASSED" if code == 0 else "FAILED"
        if status == "TIMED_OUT":
            normalized["status"] = "TIMED_OUT"
        return normalized

    def _normalized(
        self,
        name: str,
        status: str,
        exit_code: int | None,
        raw: str,
        *,
        content_type: str = "terminal",
        error: str | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_bytes = len(raw.encode("utf-8"))
        output, optimization = optimize_text(raw, content_type=content_type)
        recovery_reference = None
        handle = optimization.get("recovery_handle")
        if optimization.get("recovery_verified") is True and isinstance(handle, str):
            recovery_reference = str(uuid.uuid4())
            self._recoveries[recovery_reference] = (handle, hashlib.sha256(raw.encode("utf-8")).hexdigest())
        encoded = output.encode("utf-8")
        if len(encoded) > MAX_COMMAND_OUTPUT_BYTES:
            output = encoded[:MAX_COMMAND_OUTPUT_BYTES].decode("utf-8", errors="ignore") + "\n[Output truncated at the configured limit.]"
            optimization["truncated_after_optimization"] = True
        context = trace_context or {}
        optimized = optimization.get("optimization_used") is True
        fallback = optimization.get("fallback_used") is True
        optimization_status = "NOT_APPLICABLE" if name == "tool_recover" else "COMPACTED" if optimized else "FALLBACK" if fallback else "BYPASSED"
        output_bytes = len(output.encode("utf-8"))
        started = context.get("_started_monotonic")
        result: dict[str, Any] = {
            "request_id": context.get("request_id"),
            "tool": name,
            "adapter": context.get("adapter", "local"),
            "caller": context.get("caller", "unknown"),
            "execution_policy": self.execution_policy,
            "execution_status": status,
            "status": status,
            "exit_code": exit_code,
            "output": output,
            "optimization_used": optimized,
            "optimization_status": optimization_status,
            "input_bytes": raw_bytes,
            "output_bytes": output_bytes,
            "reduction_ratio": round(max(0.0, 1 - output_bytes / raw_bytes), 4) if raw_bytes else 0.0,
            "raw_size_bytes": raw_bytes,
            "output_size_bytes": output_bytes,
            "optimization": optimization,
            "recovery_reference": recovery_reference,
            "recovery_available": recovery_reference is not None,
            "latency_ms": round((time.monotonic() - started) * 1000, 2) if isinstance(started, (int, float)) else None,
        }
        if error:
            result["error"] = error
        return result

    def _recover(self, reference: str, *, trace_context: dict[str, Any]) -> dict[str, Any]:
        entry = self._recoveries.get(reference)
        if entry is None:
            return self._normalized("tool_recover", "REFUSED", None, "recovery reference is unknown or expired", error="unknown recovery reference", trace_context=trace_context)
        handle, expected_digest = entry
        try:
            raw = retrieve_recovered_text(handle)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            return self._normalized("tool_recover", "FAILED", None, "", error=f"Caveman recovery failed: {type(exc).__name__}", trace_context=trace_context)
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected_digest:
            return self._normalized("tool_recover", "FAILED", None, "", error="Caveman recovery digest mismatch", trace_context=trace_context)
        return self._normalized("tool_recover", "COMPLETED", 0, raw, content_type="structured", trace_context=trace_context)
