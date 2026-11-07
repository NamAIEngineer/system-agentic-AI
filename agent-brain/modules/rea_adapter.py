"""Bounded, read-only REA MCP adapter.

The adapter owns one REA stdio process per inspection.  A failed process is
therefore isolated from the host decision plane and can be restarted simply by
creating a new adapter session.  Only local, read-only REA operations are
admitted here; runtime control, debugging, injection, mutation and external
network operations stay outside this capability.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import time
import copy
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any

from providers.runtime import ProviderResult, ProviderTask, ProviderUnavailable


REA_ROOT = Path(__file__).resolve().parents[2] / "third_party" / "rea"
REA_ENTRYPOINT = REA_ROOT / "scripts" / "rea.mjs"
REA_VERSION = "6.8.0"
REA_COMMIT = "cd68181c90792775ef4b3fd4910982084f79c285"
REA_SOURCE = "https://github.com/morluto/rea.git"
MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_MESSAGE_BYTES = 12 * 1024 * 1024
MAX_EVIDENCE_BYTES = 1_000_000
MAX_LOG_BYTES = 16_384
# Capability discovery is a health probe, not an analysis request. Keep the
# MCP handshake bounded so orchestrator planning and service shutdown cannot be
# held by a stalled REA child process. Real REA analysis keeps its separate
# task-level timeout below.
# REA's Node/MCP startup can exceed five seconds on a cold macOS process when
# several browser/desktop runtimes are active. Keep health bounded, but allow
# one complete cold start so capability discovery does not flap between
# READY and unavailable during routing.
DEFAULT_START_TIMEOUT = 12.0
DEFAULT_TOOL_TIMEOUT = 120.0
HEALTH_CACHE_TTL_SECONDS = 60.0
_HEALTH_CACHE: tuple[float, dict[str, Any]] | None = None

# This is deliberately narrower than REA's complete catalog.  Every admitted
# operation reads a caller-selected local artifact or returns retained facts.
READ_ONLY_TOOLS = {
    "analyze_javascript_application",
    "inspect_android_package",
    "inspect_desktop_application",
    "inspect_firmware_regions",
    "inspect_managed_artifact",
    "inspect_web_network_capture",
    "inspect_web_page",
    "observe_javascript_runtime",
    "observe_web_session",
    "analyze_web_bundle",
    "inspect_with_objdump",
    "inspect_native_exports",
    "inspect_binary_layout",
    "inspect_apk_signature",
    "inspect_artifact",
    "inspect_packet_capture",
}


class ReaMcpError(ProviderUnavailable):
    """A bounded, normalized REA transport or tool error."""


def _node_version(path: str) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"v(\d+)\.(\d+)\.(\d+)", result.stdout or "")
    return tuple(int(item) for item in match.groups()) if result.returncode == 0 and match else None


def _supported_node(version: tuple[int, int, int] | None) -> bool:
    if version is None:
        return False
    major, minor, patch = version
    return (major == 22 and (minor, patch) >= (23, 4)) or (major == 24 and (minor, patch) >= (22, 1)) or major >= 26


def _configured_command(env_name: str, default_name: str) -> str | None:
    configured = os.environ.get(env_name)
    if configured:
        path = Path(configured).expanduser()
        return str(path.resolve()) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(default_name)


def _configured_file(env_name: str) -> str | None:
    configured = os.environ.get(env_name)
    if not configured:
        return None
    path = Path(configured).expanduser()
    return str(path.resolve()) if path.is_file() else None


def _node_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("SYSTEM_AGENTIC_REA_NODE")
    if configured:
        candidates.append(Path(configured).expanduser())
    found = shutil.which("node")
    if found:
        candidates.append(Path(found))
    candidates.extend((
        Path("/opt/homebrew/opt/node@24/bin/node"),
        Path("/opt/homebrew/opt/node@22/bin/node"),
        Path("/usr/local/bin/node"),
    ))
    # `npm exec --package=node@...` is a supported local installation path on
    # machines without Homebrew's current Node.  Prefer a cached compatible
    # binary when one already exists; never download one from this adapter.
    candidates.extend(Path.home().glob(".npm/_npx/*/node_modules/node/bin/node"))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except OSError:
            continue
        key = str(resolved)
        if key not in seen and resolved.is_file() and os.access(resolved, os.X_OK):
            seen.add(key)
            unique.append(resolved)
    return unique


def resolve_node() -> tuple[Path | None, tuple[int, int, int] | None]:
    candidates = [(path, _node_version(str(path))) for path in _node_candidates()]
    supported = [(path, version) for path, version in candidates if _supported_node(version)]
    if supported:
        return max(supported, key=lambda item: item[1] or (0, 0, 0))
    return (candidates[0] if candidates else (None, None))


def _redact(value: Any, *, depth: int = 0) -> Any:
    """Keep evidence useful while removing obvious credential-bearing fields."""
    if depth > 12:
        return "[depth-limited]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|credential)", key_text, re.I):
                result[key_text] = "[redacted]"
            else:
                result[key_text] = _redact(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_redact(item, depth=depth + 1) for item in value[:2000]]
    if isinstance(value, str):
        # Do not attempt to classify arbitrary source text as a secret.  Only
        # redact explicit bearer/basic transport values.
        return re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", "[redacted]", value)
    return value


def _bounded_json(value: Any, limit: int = MAX_EVIDENCE_BYTES) -> Any:
    redacted = _redact(value)
    try:
        encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = json.dumps({"value": repr(redacted)}, ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= limit:
        return redacted
    # Preserve a deterministic, inspectable envelope instead of silently
    # dropping evidence.  REA's own retained evidence remains authoritative.
    digest = __import__("hashlib").sha256(encoded.encode("utf-8")).hexdigest()
    return {"truncated": True, "sha256": digest, "bytes": len(encoded.encode("utf-8")), "preview": encoded[:4000]}


def _target_path(task: ProviderTask) -> Path:
    if task.context.get("target_authorized") is not True:
        raise ReaMcpError("REA requires context.target_authorized=true for an explicitly authorized local target.")
    raw = task.context.get("target_path") or task.context.get("target") or task.context.get("artifact")
    if not isinstance(raw, str) or not raw.strip():
        raise ReaMcpError("REA requires an explicit local target_path; it will not infer or fetch a target.")
    path = Path(raw).expanduser()
    if path.is_symlink():
        raise ReaMcpError("REA refuses symlink targets to keep target provenance deterministic.")
    if not path.is_absolute():
        path = task.workspace / path
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReaMcpError(f"REA target does not exist: {raw}") from exc
    if not resolved.is_file() and not resolved.is_dir():
        raise ReaMcpError("REA target must be a regular local file or directory.")
    return resolved


def _target_type(path: Path, declared: Any = None) -> str:
    if isinstance(declared, str) and declared.strip():
        return declared.strip().lower()
    suffix = path.suffix.lower()
    if suffix in {".asar", ".js", ".mjs", ".cjs", ".ts"} or path.is_dir():
        return "javascript_application" if suffix == ".asar" or path.is_dir() else "javascript_artifact"
    if suffix in {".apk", ".aab"}:
        return "android"
    if suffix in {".pcap", ".pcapng"}:
        return "network_capture"
    return "binary"


class _McpSession:
    def __init__(self, *, start_timeout: float, tool_timeout: float) -> None:
        node, version = resolve_node()
        if node is None:
            raise ReaMcpError("Node.js is not installed; REA MCP cannot start.")
        self.node = node
        self.node_version = version
        if not _supported_node(version):
            raise ReaMcpError("REA requires Node.js 22.23.4+, 24.22.1+, or 26.12.1+.")
        if not REA_ENTRYPOINT.is_file():
            raise ReaMcpError(f"REA entrypoint is missing: {REA_ENTRYPOINT}")
        env = {
            "HOME": os.environ.get("HOME", str(Path.home())),
            "PATH": f"{node.parent}:{os.environ.get('PATH', '')}",
            "REA_LOG_LEVEL": "silent",
            "REA_MCP_INPUT_SCHEMA_PROFILE": "compact",
            "REA_MCP_OUTPUT_SCHEMA_PROFILE": "compact",
            "REA_MCP_MAX_RESPONSE_BYTES": str(MAX_MESSAGE_BYTES),
            "REA_MCP_TOOL_FAMILIES": "direct,enhanced,native,artifact,managed,firmware,android,pcap,flutter,frida,apktool,jeb,adb,browser,electron,javascript-runtime,application,session",
        }
        self.process = subprocess.Popen(
            [str(node), str(REA_ENTRYPOINT), "mcp"], cwd=REA_ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.start_timeout = start_timeout
        self.tool_timeout = tool_timeout
        self._next_id = 1
        self._buffer = bytearray()
        self._selector = selectors.DefaultSelector()
        assert self.process.stdout is not None
        self._selector.register(self.process.stdout, selectors.EVENT_READ)
        self._stderr = bytearray()
        self._stderr_selector = selectors.DefaultSelector()
        if self.process.stderr is not None:
            self._stderr_selector.register(self.process.stderr, selectors.EVENT_READ)

    def _drain_stderr(self) -> None:
        for key, _ in self._stderr_selector.select(timeout=0):
            try:
                chunk = key.fileobj.read1(MAX_LOG_BYTES)
            except (OSError, AttributeError):
                chunk = b""
            if chunk:
                self._stderr.extend(chunk)
                if len(self._stderr) > MAX_LOG_BYTES:
                    del self._stderr[:-MAX_LOG_BYTES]

    def _read_message(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain_stderr()
            # REA's current MCP SDK uses newline-delimited JSON for stdio.
            # Do not apply HTTP Content-Length framing to this transport.
            marker = self._buffer.find(b"\n")
            if marker >= 0:
                body = bytes(self._buffer[:marker]).rstrip(b"\r")
                del self._buffer[:marker + 1]
                if len(body) > MAX_MESSAGE_BYTES:
                    raise ReaMcpError("REA MCP response exceeded the bounded message limit.")
                try:
                    parsed = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ReaMcpError("REA MCP returned invalid JSON.") from exc
                if isinstance(parsed, dict):
                    return parsed
                raise ReaMcpError("REA MCP returned a non-object JSON-RPC frame.")
            remaining = max(0.01, deadline - time.monotonic())
            events = self._selector.select(timeout=min(0.25, remaining))
            if not events:
                if self.process.poll() is not None:
                    raise ReaMcpError(f"REA MCP exited before replying (code {self.process.returncode}).")
                continue
            for key, _ in events:
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    raise ReaMcpError("REA MCP closed its stdout before replying.")
                self._buffer.extend(chunk)
                if len(self._buffer) > MAX_MESSAGE_BYTES * 2:
                    raise ReaMcpError("REA MCP framing buffer exceeded the bounded limit.")
        raise ReaMcpError("REA MCP request timed out.")

    def _send(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any] | None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise ReaMcpError("REA MCP process is not running.")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_MESSAGE_BYTES:
            raise ReaMcpError("REA MCP request exceeded the bounded message limit.")
        try:
            self.process.stdin.write(body + b"\n")
            self.process.stdin.flush()
        except OSError as exc:
            raise ReaMcpError("REA MCP stdin is unavailable.") from exc
        if "id" not in payload:
            return None
        while True:
            response = self._read_message(timeout)
            if response.get("id") == payload["id"]:
                if "error" in response:
                    raise ReaMcpError(str(response["error"].get("message", "REA MCP request failed")))
                return response.get("result") if isinstance(response.get("result"), dict) else {}

    def initialize(self) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        result = self._send({"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "system-agentic-ai", "version": "1.0"},
        }}, timeout=self.start_timeout)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, timeout=self.start_timeout)
        return result or {}

    def list_tools(self) -> list[dict[str, Any]]:
        request_id = self._next_id
        self._next_id += 1
        result = self._send({"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": {}}, timeout=self.start_timeout)
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return [item for item in tools if isinstance(item, dict) and isinstance(item.get("name"), str)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        result = self._send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, timeout=self.tool_timeout)
        return result or {}

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                # REA may own helper processes; it is a session leader, so
                # terminate the whole process group without touching the host.
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    self.process.send_signal(signal.SIGTERM)
                self.process.wait(timeout=1.5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        self.process.kill()
                    self.process.wait(timeout=1.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._selector.close()
        self._stderr_selector.close()


def _health_result() -> dict[str, Any]:
    started = time.monotonic()
    node, version = resolve_node()
    result: dict[str, Any] = {
        "provider": "rea",
        "source": REA_SOURCE,
        "version": REA_VERSION,
        "commit": REA_COMMIT,
        "location": str(REA_ROOT),
        "node": str(node) if node else None,
        "node_version": ".".join(map(str, version)) if version else None,
        "status": "UNAVAILABLE",
        "mcp_status": "UNAVAILABLE",
        "tool_count": 0,
        "tools": [],
        "capabilities": {},
        "errors": [],
        "duration_ms": None,
    }
    if not REA_ROOT.is_dir() or not REA_ENTRYPOINT.is_file():
        result["status"] = "MISCONFIGURED"
        result["errors"].append("vendored REA entrypoint is missing")
    elif node is None or not _supported_node(version):
        result["status"] = "MISCONFIGURED"
        result["errors"].append("Node.js version is below REA's supported range")
    else:
        session: _McpSession | None = None
        try:
            session = _McpSession(start_timeout=DEFAULT_START_TIMEOUT, tool_timeout=DEFAULT_START_TIMEOUT)
            session.initialize()
            tools = session.list_tools()
            names = sorted(item["name"] for item in tools)
            result.update({"status": "READY", "mcp_status": "READY", "tool_count": len(names), "tools": names})
            tool_set = set(names)
            result["capabilities"] = {
                "core": "READY",
                "javascript": "READY" if "analyze_javascript_application" in tool_set else "UNAVAILABLE",
                "native": "READY" if any(name in tool_set for name in {"inspect_with_objdump", "inspect_native_exports", "inspect_binary_layout"}) else "UNAVAILABLE",
                "android": "READY" if any(name in tool_set for name in {"inspect_apk_signature", "inspect_artifact"}) else "UNAVAILABLE",
                "android_package": (
                    "READY" if "inspect_android_package" in tool_set and _configured_file("REA_JADX_MCP_JAR") and shutil.which("java")
                    else "UNAVAILABLE"
                ),
                "desktop": "READY" if "inspect_desktop_application" in tool_set else "UNAVAILABLE",
                "managed": "READY" if "inspect_managed_artifact" in tool_set else "UNAVAILABLE",
                "firmware": "READY" if "inspect_firmware_regions" in tool_set and _configured_command("REA_BINWALK_COMMAND", "binwalk") else "UNAVAILABLE",
                "browser": "READY" if any(name in tool_set for name in {"inspect_web_page", "capture_browser_scenario"}) else "UNAVAILABLE",
                "web_static": "READY" if "analyze_web_bundle" in tool_set else "UNAVAILABLE",
                "web_runtime": "READY" if "observe_javascript_runtime" in tool_set else "UNAVAILABLE",
                "network_capture": "READY" if "inspect_packet_capture" in tool_set and _configured_command("REA_TSHARK_COMMAND", "tshark") else "UNAVAILABLE",
                "web_network_capture": "READY" if "inspect_web_network_capture" in tool_set else "UNAVAILABLE",
                "native_external_engines": {
                    "ghidra": "AVAILABLE" if shutil.which("ghidra") else "UNAVAILABLE",
                    "hopper": "AVAILABLE" if shutil.which("hopper") else "UNAVAILABLE",
                    "ida": "AVAILABLE" if shutil.which("ida") or shutil.which("ida64") else "UNAVAILABLE",
                },
            }
        except (OSError, ReaMcpError, ValueError) as exc:
            result["errors"].append(type(exc).__name__ + (f": {exc}" if str(exc) else ""))
            result["status"] = "PARTIAL" if REA_ROOT.is_dir() else "UNAVAILABLE"
            result["mcp_status"] = "UNAVAILABLE"
        finally:
            if session is not None:
                session.close()
    result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def rea_health() -> dict[str, Any]:
    """Probe the actual MCP lifecycle, returning bounded machine-readable health."""
    # Tool discovery starts a disposable Node/MCP process and is comparatively
    # expensive on macOS. Reuse a recent successful READY receipt during one
    # routing burst; failed probes are never cached, so an unavailable REA
    # provider remains fail-closed and can recover on the next attempt.
    global _HEALTH_CACHE
    now = time.monotonic()
    if _HEALTH_CACHE is not None:
        cached_at, cached = _HEALTH_CACHE
        if cached.get("mcp_status") == "READY" and now - cached_at <= HEALTH_CACHE_TTL_SECONDS:
            result = copy.deepcopy(cached)
            result["health_cache"] = "recent-ready"
            result["duration_ms"] = round((now - cached_at) * 1000, 2)
            return result
    result = _health_result()
    if result.get("mcp_status") == "READY":
        _HEALTH_CACHE = (time.monotonic(), copy.deepcopy(result))
    return result


def _tool_request(target: Path, target_type: str, task: ProviderTask) -> tuple[str, dict[str, Any]]:
    if target_type in {"javascript_application", "javascript_artifact", "electron"}:
        return "analyze_javascript_application", {
            "input_path": str(target), "format": "auto", "integrity_policy": "fail", "detail": "summary",
            "analysis_timeout_ms": int(task.context.get("analysis_timeout_ms", 120_000)),
        }
    if target_type in {"android_application", "android_package"}:
        return "inspect_android_package", {"path": str(target), "detail": "summary"}
    if target_type == "android" or target.suffix.lower() in {".apk", ".aab"}:
        return "inspect_apk_signature", {"path": str(target), "detail": "summary"}
    if target_type in {"desktop_application", "native_application"}:
        return "inspect_desktop_application", {"path": str(target), "detail": "summary"}
    if target_type in {"managed", "managed_assembly", "dotnet", ".net"}:
        return "inspect_managed_artifact", {"path": str(target), "detail": "summary"}
    if target_type in {"firmware", "firmware_image"}:
        return "inspect_firmware_regions", {
            "path": str(target), "analysis_timeout_ms": int(task.context.get("analysis_timeout_ms", 120_000)),
        }
    if target_type in {"web_network_capture", "har", "mitmproxy", "fiddler_saz", "aioquic_qlog"}:
        suffix = target.suffix.lower()
        fmt = {".har": "har", ".saz": "fiddler-saz", ".qlog": "aioquic-qlog"}.get(suffix, "har")
        return "inspect_web_network_capture", {"capture_path": str(target), "format": fmt, "detail": "summary"}
    if target_type in {"web", "website", "web_application", "web_app", "web_page", "browser_page"}:
        return "inspect_web_page", _loopback_runtime_arguments(task, "cdp_endpoint", {
            "target_id": task.context.get("target_id"),
            "observation_ms": int(task.context.get("observation_ms", 0)),
            "include_script_sources": False,
        })
    if target_type in {"web_bundle", "web_runtime", "runtime_observation", "runtime_behavior", "javascript_runtime", "electron_runtime"}:
        endpoint_key = "inspector_endpoint" if target_type in {"web_runtime", "runtime_observation", "runtime_behavior", "javascript_runtime", "electron_runtime"} else "cdp_endpoint"
        tool = "observe_javascript_runtime" if endpoint_key == "inspector_endpoint" else "analyze_web_bundle"
        return tool, _loopback_runtime_arguments(task, endpoint_key, {
            "target_id": task.context.get("target_id"),
            "runtime_kind": task.context.get("runtime_kind", "node") if endpoint_key == "inspector_endpoint" else None,
            "observation_ms": int(task.context.get("observation_ms", 0)),
        })
    if target_type == "network_capture" or target.suffix.lower() in {".pcap", ".pcapng"}:
        return "inspect_packet_capture", {"path": str(target), "fields": ["frame.number"], "match_limit": 50}
    if target_type in {"native_exports", "exports"}:
        return "inspect_native_exports", {"path": str(target), "detail": "summary"}
    if target_type in {"binary_layout", "layout"}:
        return "inspect_binary_layout", {"path": str(target), "detail": "summary"}
    return "inspect_with_objdump", {"path": str(target), "operation": "file_headers", "detail": "summary"}


def _loopback_runtime_arguments(task: ProviderTask, endpoint_key: str, values: dict[str, Any]) -> dict[str, Any]:
    endpoint = task.context.get(endpoint_key)
    target_id = values.get("target_id")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ReaMcpError(f"REA {endpoint_key} is required for runtime/web observation.")
    if not re.match(r"^(?:http|ws)s?://127\.0\.0\.1(?::\d+)?(?:/|$)", endpoint):
        raise ReaMcpError("REA runtime/web observation requires a loopback endpoint.")
    if not isinstance(target_id, str) or not target_id.strip():
        raise ReaMcpError("REA target_id is required for runtime/web observation.")
    result = {endpoint_key: endpoint, "target_id": target_id}
    for key, value in values.items():
        if value is not None:
            result[key] = value
    allowed_origins = task.context.get("allowed_origins")
    if allowed_origins is not None:
        if not isinstance(allowed_origins, list) or any(not isinstance(item, str) for item in allowed_origins):
            raise ReaMcpError("REA allowed_origins must be a list of strings.")
        result["allowed_origins"] = allowed_origins
    return result


def execute(task: ProviderTask, *, wait_timeout_seconds: float = 120.0) -> ProviderResult:
    if task.execution_policy != "READ_ONLY":
        raise ReaMcpError("REA is read-only; WORKSPACE_WRITE and ELEVATED execution are refused.")
    target = _target_path(task)
    target_type = _target_type(target, task.context.get("target_type"))
    tool_name, arguments = _tool_request(target, target_type, task)
    if tool_name not in READ_ONLY_TOOLS:
        raise ReaMcpError(f"REA tool is outside the read-only allowlist: {tool_name}")
    started_at = time.time()
    session: _McpSession | None = None
    try:
        session = _McpSession(start_timeout=min(DEFAULT_START_TIMEOUT, wait_timeout_seconds), tool_timeout=wait_timeout_seconds)
        session.initialize()
        tools = session.list_tools()
        names = {item["name"] for item in tools}
        if tool_name not in names:
            raise ReaMcpError(f"REA MCP tool is not advertised: {tool_name}")
        raw = session.call_tool(tool_name, arguments)
        finished_at = time.time()
    except (OSError, ReaMcpError, ValueError) as exc:
        finished_at = time.time()
        error = {"code": "REA_MCP_ERROR", "message": str(exc)[:2000]}
        return ProviderResult(
            task.task_id, "rea", None, "FAILED", None, (),
            ({"type": "rea-execution", "provider": "rea", "target": str(target), "target_type": target_type,
              "tool": tool_name, "status": "FAILED", "limitations": ["No evidence was returned because the MCP call failed."],
              "unknowns": ["analysis unavailable"], "provenance": {"source": REA_SOURCE, "commit": REA_COMMIT}},),
            (error,), datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
            datetime.fromtimestamp(finished_at, timezone.utc).isoformat(),
            task.task_id, task.role, task.required_capabilities, "rea MCP read-only adapter",
            finished_at - started_at, task.execution_policy, task.execution_context,
        )
    finally:
        if session is not None:
            session.close()

    structured = raw.get("structuredContent") if isinstance(raw, dict) else None
    content = raw.get("content") if isinstance(raw, dict) else None
    source = structured if structured is not None else content
    normalized = _bounded_json(source)
    fact_root = normalized.get("normalized_result", normalized) if isinstance(normalized, dict) else {}
    if isinstance(normalized, dict):
        limitations = normalized.get("limitations", fact_root.get("limitations", [])) if isinstance(normalized.get("limitations", fact_root.get("limitations", [])), list) else []
        unknowns = normalized.get("unknowns", fact_root.get("unknowns", fact_root.get("unexplored_branches", [])))
        if not isinstance(unknowns, list):
            unknowns = []
    else:
        limitations, unknowns = [], []
    evidence = {
        "type": "rea-evidence",
        "provider": "rea",
        "capability": "REVERSE_ENGINEERING",
        "target": str(target),
        "target_type": target_type,
        "tool": tool_name,
        "status": "PARTIAL" if raw.get("isError") else "COMPLETED",
        "findings": normalized,
        "evidence": normalized,
        "confidence": (normalized.get("confidence", fact_root.get("confidence")) if isinstance(normalized, dict) else None),
        "limitations": limitations,
        "unknowns": unknowns,
        "artifacts": fact_root.get("artifacts", []) if isinstance(fact_root, dict) and isinstance(fact_root.get("artifacts", []), list) else [],
        "commands": [f"{REA_SOURCE}@{REA_COMMIT}:mcp tools/call {tool_name}"],
        "provenance": {"source": REA_SOURCE, "version": REA_VERSION, "commit": REA_COMMIT,
                       "entrypoint": str(REA_ENTRYPOINT), "target_path": str(target)},
    }
    evidence = _bounded_json(evidence)
    status = "PARTIAL" if raw.get("isError") else "COMPLETED"
    summary = json.dumps({"provider": "rea", "capability": "REVERSE_ENGINEERING", "target": str(target),
                          "target_type": target_type, "tool": tool_name, "status": status,
                          "evidence_count": 1, "limitations": limitations, "unknowns": unknowns}, ensure_ascii=False)
    execution_context = dict(task.execution_context)
    execution_context["rea"] = {"provider": "rea", "capability": "REVERSE_ENGINEERING", "target": str(target),
                                 "target_type": target_type, "tool": tool_name,
                                 "source": REA_SOURCE, "version": REA_VERSION, "commit": REA_COMMIT}
    execution_context["observability"] = {
        "task_id": task.task_id, "provider": "rea", "capability": "REVERSE_ENGINEERING",
        "target_type": target_type, "selected_tool": tool_name,
        "reason_selected": "target type mapped to bounded read-only REA MCP operation",
        "start_time": datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
        "end_time": datetime.fromtimestamp(finished_at, timezone.utc).isoformat(),
        "status": status, "evidence_count": 1, "error": None,
    }
    return ProviderResult(
        task.task_id, "rea", None, status, summary, (), (evidence,), (),
        datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
        datetime.fromtimestamp(finished_at, timezone.utc).isoformat(),
        task.task_id, task.role, task.required_capabilities, "rea MCP read-only adapter",
        finished_at - started_at, task.execution_policy, execution_context,
    )


class ReaMcpProviderAdapter:
    provider_id = "rea"

    def detect(self):
        from providers.runtime import discover_providers
        return next(item for item in discover_providers() if item.provider_id == "rea")

    def health(self) -> dict[str, Any]:
        return rea_health()

    def capabilities(self) -> tuple[str, ...]:
        return ("task.submit", "REVERSE_ENGINEERING", "reverse_engineering", "mcp.tools", "workspace.read")

    def execute(self, task: ProviderTask, *, wait_timeout_seconds: float = 600) -> ProviderResult:
        return execute(task, wait_timeout_seconds=wait_timeout_seconds)

    def status(self, task: ProviderTask, *, selection_reason: str = "reconciled by task ID") -> ProviderResult:
        raise ReaMcpError("REA jobs are one-shot local MCP sessions; there is no resumable queue task.")

    def normalize(self, result: object, task: ProviderTask, account_id: str | None, selection_reason: str) -> ProviderResult:
        if isinstance(result, ProviderResult):
            return replace(result, selection_reason=selection_reason)
        raise ReaMcpError("REA result is not a ProviderResult.")
