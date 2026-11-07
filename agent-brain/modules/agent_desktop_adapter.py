"""Shared, bounded MCP adapter for vercel-labs/agent-desktop.

The adapter owns one short-lived stdio MCP server per run.  It never shells a
caller-provided command: executable and argv stay separate, startup flags are
fixed when the server starts, and every action is preceded/followed by a fresh
observation.  Native desktop actions require an explicit per-task opt-in;
READ_ONLY tasks can inspect state but cannot click, type, launch, or close apps.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from providers.runtime import ProviderTask, ProviderUnavailable


MODULE_ID = "agent-desktop"
CAPABILITY = "desktop.automation"
VERSION = "1.2.0"
PACKAGE_INTEGRITY = "sha512-sp8hHs8JfnuZe8ZQwiKZrqSOlu26SSeZXkjDog4KsU31L09G3uZJa8pITGN2vORif9uuh0iOSJHD146CEyTqag=="
MCP_PROTOCOL_VERSION = "2024-11-05"
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_BYTES = 750_000
DEFAULT_TIMEOUT_MS = 15_000
DEFAULT_START_TIMEOUT = 20.0
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

OBSERVATION_TOOLS = {
    "desktop_snapshot", "desktop_find", "desktop_get", "desktop_is", "desktop_screenshot",
    "list_windows", "list_displays", "list_apps", "list_surfaces", "status", "permissions",
    "version", "wait", "trace_show", "skills_list", "skills_get", "session_list",
}
ACTION_TOOLS = {
    "desktop_click", "desktop_double_click", "desktop_triple_click", "desktop_right_click",
    "desktop_type", "desktop_set_value", "desktop_clear", "desktop_focus", "desktop_toggle",
    "desktop_check", "desktop_uncheck", "desktop_expand", "desktop_collapse", "desktop_scroll_to",
    "desktop_select", "desktop_scroll", "desktop_press", "desktop_key_down", "desktop_key_up",
    "desktop_hover", "desktop_drag", "desktop_mouse_move", "desktop_mouse_click", "desktop_mouse_down",
    "desktop_mouse_up", "desktop_mouse_wheel", "desktop_launch", "focus_window", "resize_window",
    "move_window", "minimize_window", "maximize_window", "restore_window", "open_system_surface",
    "session_start", "session_end", "trace_export", "cursor_overlay_enable", "cursor_overlay_disable",
}
DISALLOWED_TOOLS = {
    "close_app", "clipboard_set", "clipboard_clear", "dismiss_notification", "dismiss_all",
    "notification_action",
}
REQUIRES_REF = {
    "desktop_get", "desktop_is", "desktop_click", "desktop_double_click", "desktop_triple_click",
    "desktop_right_click", "desktop_type", "desktop_set_value", "desktop_clear", "desktop_focus",
    "desktop_toggle", "desktop_check", "desktop_uncheck", "desktop_expand", "desktop_collapse",
    "desktop_scroll_to", "desktop_select", "desktop_scroll", "desktop_hover", "desktop_drag",
}


class AgentDesktopError(ProviderUnavailable):
    def __init__(self, message: str, *, code: str = "AGENT_DESKTOP_ERROR", data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _bounded(value: Any, limit: int = MAX_EVIDENCE_BYTES) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = json.dumps({"value": repr(value)}, ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= limit:
        return value
    return {"truncated": True, "bytes": len(encoded.encode("utf-8")), "preview": encoded[:4000]}


def _resolve_executable(configured: Any = None) -> str | None:
    raw = configured or os.environ.get("SYSTEM_AGENTIC_AGENT_DESKTOP_EXECUTABLE") or "agent-desktop"
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        return str(path.resolve()) if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which(raw)
    if found:
        return str(Path(found).resolve())
    candidates = (Path.home() / ".local" / "bin" / raw, Path.home() / ".npm-global" / "bin" / raw)
    return next((str(candidate.resolve()) for candidate in candidates if candidate.is_file() and os.access(candidate, os.X_OK)), None)


def _version_payload(value: Any) -> str | None:
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, dict) and isinstance(data.get("version"), str):
            return data["version"]
        for key in ("packageVersion", "version"):
            if isinstance(value.get(key), str):
                return value[key]
    if isinstance(value, str):
        match = re.search(r"\b(\d+\.\d+\.\d+)\b", value)
        return match.group(1) if match else None
    return None


def agent_desktop_health(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config if isinstance(config, dict) else {}
    executable = _resolve_executable(config.get("executable"))
    evidence: dict[str, Any] = {"expectedVersion": VERSION, "packageIntegrity": PACKAGE_INTEGRITY,
                                 "platform": os.uname().sysname.lower() if hasattr(os, "uname") else "unknown"}
    if executable is None:
        return {"status": "UNAVAILABLE", "installed": False, "executable": None,
                "evidence": {**evidence, "reason": "agent-desktop executable not found"}}
    evidence["executable"] = executable
    try:
        result = subprocess.run([executable, "version"], capture_output=True, text=True,
                                timeout=5, check=False)
        raw = (result.stdout or "") + (result.stderr or "")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = raw.strip()
        actual = _version_payload(payload)
        evidence.update({"exitCode": result.returncode, "version": actual, "rawPreview": raw[:1000]})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "DEGRADED", "installed": True, "executable": executable,
                "evidence": {**evidence, "reason": f"version probe failed: {type(exc).__name__}"}}
    if result.returncode != 0:
        return {"status": "DEGRADED", "installed": True, "executable": executable, "evidence": evidence}
    if actual and actual != VERSION:
        return {"status": "DEGRADED", "installed": True, "executable": executable,
                "evidence": {**evidence, "reason": "version mismatch"}}
    return {"status": "READY", "installed": True, "executable": executable, "evidence": evidence}


class AgentDesktopMcpSession:
    """Minimal MCP stdio transport; no shell parsing or provider fallback."""

    def __init__(self, argv: list[str], *, cwd: Path, start_timeout: float) -> None:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise AgentDesktopError("agent-desktop argv must be a non-empty string array", code="INVALID_ARGUMENT")
        self.process = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)
        self._selector = selectors.DefaultSelector()
        assert self.process.stdout is not None and self.process.stderr is not None
        self._selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        self._selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        self._buffer = bytearray()
        self._stderr = bytearray()
        self._next_id = 1
        self.tools: dict[str, dict[str, Any]] = {}
        self.initialize_result: dict[str, Any] = {}
        self.tools_list_result: dict[str, Any] = {}
        try:
            self.initialize_result = self._request("initialize", {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                                                                    "clientInfo": {"name": "system-agentic-ai", "version": "1.0"}}, start_timeout)
            self._notify("notifications/initialized", {})
            listed = self._request("tools/list", {}, start_timeout)
            self.tools_list_result = listed
            raw_tools = listed.get("tools") if isinstance(listed, dict) else None
            if not isinstance(raw_tools, list):
                raise AgentDesktopError("agent-desktop tools/list returned no tools", code="SCHEMA_INVALID")
            for tool in raw_tools:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    self.tools[tool["name"]] = tool
            required = {"desktop_snapshot", "desktop_find", "desktop_skills_get", "skills_get"} & set(self.tools)
            if "desktop_snapshot" not in self.tools or not required - {"desktop_snapshot"}:
                raise AgentDesktopError("agent-desktop MCP schema lacks required snapshot/skills tools", code="SCHEMA_INVALID",
                                        data={"tools": sorted(self.tools)})
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "AgentDesktopMcpSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _read_message(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            marker = self._buffer.find(b"\n")
            if marker >= 0:
                body = bytes(self._buffer[:marker]).rstrip(b"\r")
                del self._buffer[:marker + 1]
                if not body:
                    continue
                if len(body) > MAX_MESSAGE_BYTES:
                    raise AgentDesktopError("MCP response exceeds size limit", code="MESSAGE_TOO_LARGE")
                try:
                    value = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AgentDesktopError("MCP response is not valid newline-delimited JSON", code="PROTOCOL_INVALID") from exc
                if not isinstance(value, dict):
                    raise AgentDesktopError("MCP response must be a JSON object", code="PROTOCOL_INVALID")
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentDesktopError("agent-desktop MCP response timed out", code="TIMEOUT")
            events = self._selector.select(min(0.2, remaining))
            if not events:
                if self.process.poll() is not None:
                    raise AgentDesktopError("agent-desktop MCP process exited before response", code="PROCESS_EXITED",
                                            data={"returncode": self.process.returncode, "stderr": self._stderr.decode(errors="replace")[-4000:]})
                continue
            for key, _ in events:
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    continue
                if key.data == "stdout":
                    self._buffer.extend(chunk)
                else:
                    self._stderr.extend(chunk[:MAX_EVIDENCE_BYTES])

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self.process.stdin is None:
            raise AgentDesktopError("MCP stdin is closed", code="PROCESS_EXITED")
        request_id = self._next_id
        self._next_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.process.stdin.write(body + b"\n")
        self.process.stdin.flush()
        while True:
            response = self._read_message(timeout)
            if response.get("id") != request_id:
                continue
            if "error" in response:
                error = response.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                message = error.get("message") if isinstance(error, dict) else str(error)
                raise AgentDesktopError(str(message), code=str(code or "MCP_ERROR"), data=error)
            result = response.get("result")
            if not isinstance(result, dict):
                raise AgentDesktopError("MCP result must be an object", code="PROTOCOL_INVALID", data=response)
            return result

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self.process.stdin is None:
            return
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, separators=(",", ":")).encode()
        self.process.stdin.write(body + b"\n")
        self.process.stdin.flush()

    def call(self, tool: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        if tool not in self.tools:
            raise AgentDesktopError(f"agent-desktop tool is not in tools/list: {tool}", code="TOOL_UNAVAILABLE")
        result = self._request("tools/call", {"name": tool, "arguments": arguments}, timeout)
        if result.get("isError") is True:
            structured = result.get("structuredContent")
            payload = structured if isinstance(structured, dict) else {}
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            code = error.get("code") if isinstance(error.get("code"), str) else "TOOL_ERROR"
            message = error.get("message") if isinstance(error.get("message"), str) else "agent-desktop tool returned isError=true"
            raise AgentDesktopError(message, code=code, data=result)
        return result

    def close(self) -> None:
        try:
            self._selector.close()
        except Exception:
            pass
        if getattr(self, "process", None) is None or self.process.poll() is not None:
            for stream in (getattr(self.process, "stdin", None), getattr(self.process, "stdout", None), getattr(self.process, "stderr", None)):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except OSError:
                self.process.kill()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except OSError:
                pass


def _refs(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"ref_id", "refId"} and isinstance(item, str):
                found.add(item)
            found.update(_refs(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_refs(item))
    return found


def _snapshot_nodes(value: Any) -> list[dict[str, Any]]:
    """Collect accessibility nodes from an upstream snapshot envelope."""
    nodes_by_ref: dict[str, dict[str, Any]] = {}

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            ref = item.get("ref_id", item.get("refId"))
            if isinstance(ref, str):
                nodes_by_ref[ref] = item
            # Walk every nested container: MCP envelopes vary between
            # structuredContent.data.tree, direct tree, and list wrappers.
            for child in item.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return list(nodes_by_ref.values())


def _selector_value(node: dict[str, Any], key: str) -> Any:
    if key in node:
        return node[key]
    aliases = {"native_id": ("nativeId", "identifier", "ax_identifier"),
               "name": ("title", "label")}
    for alias in aliases.get(key, ()):
        if alias in node:
            return node[alias]
    return None


def _native_id_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        expected_value = expected.get("value")
    else:
        expected_value = expected
    if isinstance(actual, dict):
        actual_value = actual.get("value")
    else:
        actual_value = actual
    return expected_value is not None and actual_value == expected_value


def _resolve_selector_ref(snapshot: Any, selector: Any) -> str:
    if not isinstance(selector, dict) or not selector:
        raise AgentDesktopError("desktop action selector must be a non-empty object", code="INVALID_ARGUMENT")
    supported = {"name", "role", "native_id", "value", "description"}
    unknown = set(selector) - supported
    if unknown:
        raise AgentDesktopError(f"unsupported desktop selector fields: {sorted(unknown)}", code="INVALID_ARGUMENT")
    matches: list[dict[str, Any]] = []
    for node in _snapshot_nodes(snapshot):
        matched = True
        for key, expected in selector.items():
            actual = _selector_value(node, key)
            if key == "native_id":
                matched = _native_id_matches(actual, expected)
            elif isinstance(expected, str):
                matched = actual == expected
            else:
                matched = actual == expected
            if not matched:
                break
        if matched:
            matches.append(node)
    if len(matches) != 1:
        code = "AMBIGUOUS_TARGET"
        raise AgentDesktopError(
            "desktop selector did not resolve to exactly one snapshot node",
            code=code,
            data={"selector": selector, "match_count": len(matches),
                  "matches": [{"ref_id": n.get("ref_id", n.get("refId")),
                               "name": n.get("name"), "role": n.get("role"),
                               "native_id": n.get("native_id", n.get("nativeId"))}
                              for n in matches[:20]]},
        )
    ref = matches[0].get("ref_id", matches[0].get("refId"))
    if not isinstance(ref, str):
        raise AgentDesktopError("selected snapshot node has no usable ref_id", code="AMBIGUOUS_TARGET")
    return ref


def _subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _subset(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and expected == actual
    return expected == actual


def _semantic_snapshot(value: Any) -> Any:
    """Remove volatile ref/session identifiers before comparing snapshots."""
    volatile = {"ref_id", "refId", "snapshot_id", "snapshotId", "session_id", "sessionId"}
    if isinstance(value, dict):
        return {key: _semantic_snapshot(item) for key, item in value.items() if key not in volatile}
    if isinstance(value, list):
        return [_semantic_snapshot(item) for item in value]
    return value


def _config(task: ProviderTask) -> dict[str, Any]:
    raw = task.context.get("desktop")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AgentDesktopError("context.desktop must be an object", code="INVALID_ARGUMENT")
    allowed = {"enabled", "executable", "argv", "session", "headed", "trace", "max_actions", "timeout_ms",
               "target", "actions", "allow_actions", "action_authorization", "trace_output"}
    unknown = set(raw) - allowed
    if unknown:
        raise AgentDesktopError(f"unknown desktop config fields: {sorted(unknown)}", code="INVALID_ARGUMENT")
    result = dict(raw)
    if result.get("enabled", True) is not True:
        raise AgentDesktopError("agent-desktop capability is disabled by project configuration", code="DISABLED")
    result.setdefault("argv", ["mcp"])
    if not isinstance(result["argv"], list) or any(not isinstance(item, str) or not item for item in result["argv"]):
        raise AgentDesktopError("desktop.argv must be a string array", code="INVALID_ARGUMENT")
    if not result["argv"] or result["argv"][0] != "mcp":
        raise AgentDesktopError("desktop.argv must start with the MCP subcommand 'mcp'", code="INVALID_ARGUMENT")
    result.setdefault("max_actions", 20)
    result.setdefault("timeout_ms", DEFAULT_TIMEOUT_MS)
    if type(result["max_actions"]) is not int or not 0 <= result["max_actions"] <= 100:
        raise AgentDesktopError("desktop.max_actions must be an integer from 0 to 100", code="INVALID_ARGUMENT")
    if type(result["timeout_ms"]) is not int or not 100 <= result["timeout_ms"] <= 120_000:
        raise AgentDesktopError("desktop.timeout_ms must be between 100 and 120000", code="INVALID_ARGUMENT")
    if result.get("target") is not None and not isinstance(result["target"], dict):
        raise AgentDesktopError("desktop.target must be an object", code="INVALID_ARGUMENT")
    actions = result.get("actions", [])
    if not isinstance(actions, list) or any(not isinstance(item, dict) for item in actions):
        raise AgentDesktopError("desktop.actions must be an array of objects", code="INVALID_ARGUMENT")
    if len(actions) > result["max_actions"]:
        raise AgentDesktopError("desktop action limit exceeded", code="ACTION_LIMIT")
    result["actions"] = actions
    return result


def _startup_argv(config: dict[str, Any], task_id: str) -> list[str]:
    executable = _resolve_executable(config.get("executable"))
    if executable is None:
        raise AgentDesktopError("agent-desktop executable not found", code="MODULE_UNAVAILABLE")
    argv = [executable, *config["argv"]]
    session = config.get("session") or task_id
    if not isinstance(session, str) or not _SAFE_ID.fullmatch(session):
        raise AgentDesktopError("desktop.session must be a safe identifier", code="INVALID_ARGUMENT")
    argv.extend(["--session", session])
    if config.get("headed") is True:
        argv.append("--headed")
    if config.get("trace") is True:
        argv.append("--trace")
    return argv


def _inject_target(tool: str, arguments: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    if not target:
        return arguments
    result = dict(arguments)
    # App/window selectors belong to observation tools. Upstream action tools
    # intentionally accept only their ref (plus tool-specific arguments).
    target_tools = {"desktop_snapshot", "desktop_find", "desktop_screenshot", "desktop_list_windows"}
    if tool not in target_tools:
        return result
    if "app" in target and "app" not in result:
        result["app"] = target["app"]
    # launch has no window_id argument. For verification calls that explicitly
    # select another app, do not leak the caller target's window into them.
    if ("window_id" in target and "window_id" not in result and tool != "desktop_launch"
            and ("app" not in target or result.get("app") == target.get("app"))):
        result["window_id"] = target["window_id"]
    return result


def _normalize_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    value = _bounded(result)
    if isinstance(value, dict):
        structured = value.get("structuredContent")
        if isinstance(structured, dict):
            return {"raw": value, "data": _bounded(structured)}
    return {"raw": value}


def _tool_data(result: dict[str, Any]) -> dict[str, Any]:
    """Return the upstream command payload while retaining the raw MCP envelope in evidence."""
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("data"), dict):
        return structured["data"]
    return structured if isinstance(structured, dict) else result


def _tool_complete(result: dict[str, Any]) -> bool:
    data = _tool_data(result)
    return data.get("complete", True) is not False


def _error_evidence(exc: AgentDesktopError) -> dict[str, Any]:
    detail: dict[str, Any] = {"code": exc.code, "message": str(exc)}
    if isinstance(exc.data, dict):
        detail["mcp"] = _bounded(exc.data)
        structured = exc.data.get("structuredContent")
        if isinstance(structured, dict):
            if isinstance(structured.get("error"), dict):
                detail["error"] = _bounded(structured["error"])
            if isinstance(structured.get("disposition"), dict):
                detail["disposition"] = _bounded(structured["disposition"])
            if isinstance(structured.get("ok"), bool):
                detail["ok"] = structured["ok"]
    return detail


def _tool_name(session: AgentDesktopMcpSession, canonical: str) -> str | None:
    """Resolve upstream's `desktop_<command>` MCP names without guessing at call time."""
    for candidate in (canonical, f"desktop_{canonical}"):
        if candidate in session.tools:
            return candidate
    return None


def _cancelled(task: ProviderTask) -> bool:
    callback = task.context.get("isCancelled")
    return bool(task.context.get("cancelled") is True or callable(callback) and callback())


def execute(task: ProviderTask, *, session_factory: Callable[..., AgentDesktopMcpSession] = AgentDesktopMcpSession) -> dict[str, Any]:
    started = time.monotonic()
    config = _config(task)
    actions = config["actions"]
    if actions and task.execution_policy == "READ_ONLY":
        raise AgentDesktopError("desktop actions require WORKSPACE_WRITE plus explicit action_authorization", code="AUTH_REQUIRED")
    if actions and config.get("action_authorization") != "explicit-task":
        raise AgentDesktopError("desktop actions require desktop.action_authorization='explicit-task'", code="AUTH_REQUIRED")
    if any(item.get("tool") in DISALLOWED_TOOLS for item in actions):
        raise AgentDesktopError("requested desktop tool is disallowed by the shared policy", code="POLICY_BLOCKED")
    argv = _startup_argv(config, task.task_id)
    timeout = config["timeout_ms"] / 1000
    evidence: list[dict[str, Any]] = [{"type": "startup", "argv": [argv[0], *argv[1:]], "session": config.get("session") or task.task_id,
                                      "version": VERSION, "packageIntegrity": PACKAGE_INTEGRITY}]
    latest_snapshot: dict[str, Any] | None = None
    verified_count = 0
    unverified_count = 0
    status = "COMPLETED"
    errors: list[dict[str, str]] = []
    session: AgentDesktopMcpSession | None = None
    try:
        if _cancelled(task):
            return {"execution_status": "CANCELLED", "domain_outcome": "UNVERIFIED", "summary": "agent-desktop run cancelled before startup",
                    "module": MODULE_ID, "capability": CAPABILITY, "session": config.get("session") or task.task_id,
                    "evidence": evidence, "errors": [{"code": "CANCELLED", "message": "Cancellation requested before MCP startup."}], "duration_seconds": 0.0, "artifacts": []}
        session = session_factory(argv, cwd=task.workspace.resolve(strict=True), start_timeout=min(DEFAULT_START_TIMEOUT, timeout))
        evidence[0].update({"protocolVersion": session.initialize_result.get("protocolVersion"),
                            "serverInfo": _bounded(session.initialize_result.get("serverInfo")),
                            "toolCount": len(session.tools), "childPid": session.process.pid})
        skill_tool = "desktop_skills_get" if "desktop_skills_get" in session.tools else "skills_get"
        for name in ("desktop", "platform"):
            evidence.append({"type": "skill", "name": name, "result": _normalize_tool_result(session.call(skill_tool, {"name": name}, timeout))})
        for canonical in ("version", "status"):
            resolved_tool = _tool_name(session, canonical)
            if resolved_tool:
                evidence.append({"type": "runtime", "tool": resolved_tool, "result": _normalize_tool_result(session.call(resolved_tool, {}, timeout))})
        latest_snapshot = session.call("desktop_snapshot", _inject_target("desktop_snapshot", {}, config.get("target", {})), timeout)
        evidence.append({"type": "observation", "tool": "desktop_snapshot", "result": _normalize_tool_result(latest_snapshot), "complete": _tool_complete(latest_snapshot)})
        if not _tool_complete(latest_snapshot):
            status = "PARTIAL"
            errors.append({"code": "INCOMPLETE_OBSERVATION", "message": "Initial desktop snapshot was incomplete."})
        for index, spec in enumerate(actions):
            if _cancelled(task):
                status = "CANCELLED"
                errors.append({"code": "CANCELLED", "message": "Cancellation requested before the next desktop action."})
                break
            tool = spec.get("tool")
            arguments = spec.get("arguments", {})
            if not isinstance(tool, str) or tool not in ACTION_TOOLS | OBSERVATION_TOOLS:
                raise AgentDesktopError(f"unsupported desktop tool: {tool!r}", code="TOOL_NOT_ALLOWED")
            if not isinstance(arguments, dict):
                raise AgentDesktopError(f"action {index} arguments must be an object", code="INVALID_ARGUMENT")
            arguments = dict(arguments)
            selector = spec.get("selector")
            if selector is not None:
                resolved_ref = _resolve_selector_ref(latest_snapshot, selector)
                explicit_ref = arguments.get("ref_id")
                if explicit_ref is not None and explicit_ref != resolved_ref:
                    raise AgentDesktopError(
                        f"action {index} selector resolved to a different ref_id",
                        code="STALE_REF",
                        data={"selector": selector, "ref_id": explicit_ref, "resolved_ref_id": resolved_ref},
                    )
                arguments["ref_id"] = resolved_ref
                evidence.append({"type": "selector_resolution", "index": index,
                                 "selector": _bounded(selector), "ref_id": resolved_ref})
            if tool in REQUIRES_REF:
                ref = arguments.get("ref_id")
                refs = _refs(latest_snapshot)
                if not isinstance(ref, str) or ref not in refs:
                    code = "AMBIGUOUS_TARGET" if not refs else "STALE_REF"
                    raise AgentDesktopError(f"action {index} ref_id is not present in the latest snapshot", code=code,
                                            data={"ref_id": ref, "known_refs": sorted(refs)[:200]})
            call_args = _inject_target(tool, arguments, config.get("target", {}))
            before_snapshot = latest_snapshot
            attempt = 0
            while True:
                try:
                    result = session.call(tool, call_args, timeout)
                    evidence.append({"type": "action", "index": index, "tool": tool, "attempt": attempt + 1,
                                     "result": _normalize_tool_result(result), "delivery": result.get("delivery", "unknown")})
                    disposition = result.get("disposition") if isinstance(result.get("disposition"), dict) else {}
                    if result.get("complete") is False:
                        status = "PARTIAL"
                        errors.append({"code": "INCOMPLETE_RESULT", "message": f"{tool} returned an incomplete result."})
                    latest_snapshot = session.call("desktop_snapshot", _inject_target("desktop_snapshot", {}, config.get("target", {})), timeout)
                    evidence.append({"type": "verification_observation", "index": index, "result": _normalize_tool_result(latest_snapshot),
                                     "complete": _tool_complete(latest_snapshot)})
                    verify = spec.get("verify")
                    if isinstance(verify, dict):
                        verify_tool = verify.get("tool", "desktop_snapshot")
                        verify_args = verify.get("arguments", {})
                        if not isinstance(verify_tool, str) or verify_tool not in session.tools or not isinstance(verify_args, dict):
                            raise AgentDesktopError("invalid action verification request", code="INVALID_ARGUMENT")
                        observed = session.call(verify_tool, _inject_target(verify_tool, verify_args, config.get("target", {})), timeout)
                        expected = verify.get("expect")
                        passed = expected is None or _subset(expected, observed)
                        if verify.get("expect_change") is True:
                            passed = passed and _tool_complete(latest_snapshot) and (
                                _semantic_snapshot(before_snapshot) != _semantic_snapshot(latest_snapshot)
                            )
                        evidence.append({"type": "assertion", "index": index, "tool": verify_tool, "passed": passed,
                                         "expected": _bounded(expected), "expect_change": verify.get("expect_change") is True,
                                         "before": _normalize_tool_result(before_snapshot),
                                         "observed": _normalize_tool_result(observed),
                                         "verification_observation": _normalize_tool_result(latest_snapshot)})
                        if not passed:
                            status = "PARTIAL"
                            errors.append({"code": "POSTCONDITION_FAILED", "message": f"Postcondition failed for {tool}."})
                        else:
                            verified_count += 1
                    else:
                        unverified_count += 1
                        status = "PARTIAL"
                    break
                except AgentDesktopError as exc:
                    disposition = exc.data.get("disposition", {}) if isinstance(exc.data, dict) else {}
                    retry = disposition.get("retry") if isinstance(disposition, dict) else None
                    if retry == "safe" and attempt == 0:
                        attempt += 1
                        latest_snapshot = session.call("desktop_snapshot", _inject_target("desktop_snapshot", {}, config.get("target", {})), timeout)
                        continue
                    errors.append(_error_evidence(exc))
                    if exc.code in {"STALE_REF", "AMBIGUOUS_TARGET", "AUTH_REQUIRED", "POLICY_BLOCKED"}:
                        status = "BLOCKED"
                    elif exc.code == "TIMEOUT":
                        status = "UNKNOWN"
                    else:
                        status = "FAILED"
                    break
            if status in {"FAILED", "BLOCKED"}:
                break
        trace_output = config.get("trace_output")
        if trace_output:
            output = (task.workspace / trace_output).resolve(strict=False) if not Path(trace_output).is_absolute() else Path(trace_output).resolve(strict=False)
            output.relative_to(task.workspace.resolve(strict=True))
            output.parent.mkdir(parents=True, exist_ok=True)
            if "trace_export" in session.tools:
                session.call("trace_export", {"out": str(output), "limit": 10_000}, timeout)
                evidence.append({"type": "trace", "path": str(output)})
    except AgentDesktopError as exc:
        errors.append(_error_evidence(exc))
        if exc.code in {"STALE_REF", "AMBIGUOUS_TARGET", "AUTH_REQUIRED", "POLICY_BLOCKED"}:
            status = "BLOCKED"
        elif exc.code == "TIMEOUT":
            status = "UNKNOWN"
        else:
            status = "UNAVAILABLE" if exc.code in {"MODULE_UNAVAILABLE", "PROCESS_EXITED", "TOOL_UNAVAILABLE"} else "FAILED"
    except (OSError, ValueError) as exc:
        errors.append({"code": "ADAPTER_ERROR", "message": str(exc)})
        status = "FAILED"
    finally:
        if session is not None:
            session.close()
            evidence.append({"type": "lifecycle", "childPid": session.process.pid,
                             "returncode": session.process.returncode,
                             "childExited": session.process.poll() is not None})
    summary = f"agent-desktop {VERSION}: {status}; actions={len(actions)} verified={verified_count} unverified={unverified_count}"
    return {"execution_status": status, "domain_outcome": "VERIFIED" if status == "COMPLETED" else "UNVERIFIED",
            "summary": summary, "module": MODULE_ID, "capability": CAPABILITY,
            "session": config.get("session") or task.task_id, "evidence": evidence, "errors": errors,
            "duration_seconds": round(time.monotonic() - started, 3), "artifacts": []}


def execute_agent_desktop_module(task: ProviderTask) -> dict[str, Any]:
    return execute(task)
