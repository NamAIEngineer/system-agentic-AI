"""Provider-neutral adapter for the official Design OS Pedagogy rehearsal API."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import urlsplit

from modules.pedagogy_runtime import PedagogyRuntimeUnavailable, ensure_runtime


SESSION_ID = re.compile(r"^[0-9a-f-]{36}$")
CAPABILITY_ACTIONS = {
    "pedagogy.rehearse": {"create", "advance"},
    "pedagogy.feedback": {"feedback"},
    "pedagogy.session": {"get", "events", "replay", "export"},
}


class PedagogyUnavailable(RuntimeError):
    """The official local rehearsal service could not complete the request."""


def service_url() -> str:
    value = os.environ.get("SYSTEM_AGENTIC_PEDAGOGY_URL", "http://127.0.0.1:8876").rstrip("/")
    parsed = urlsplit(value)
    try:
        loopback = parsed.hostname is not None and ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if (parsed.scheme != "http" or not loopback or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Pedagogy API URL must be a plain loopback HTTP origin.")
    return value


def request(method: str, path: str, payload: dict[str, Any] | None = None,
            *, base_url: str | None = None) -> tuple[int, Any]:
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request((base_url or service_url()) + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=3) as response:
            body = response.read(32 * 1024 + 1)
            if len(body) > 32 * 1024:
                raise PedagogyUnavailable("Pedagogy response exceeded the adapter size limit.")
            content_type = response.headers.get_content_type()
            return response.status, json.loads(body) if content_type == "application/json" else body.decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read(32 * 1024 + 1)
        if exc.code >= 500:
            raise PedagogyUnavailable(f"Pedagogy service returned HTTP {exc.code}.") from exc
        try:
            parsed = json.loads(body) if len(body) <= 32 * 1024 else None
        except (ValueError, UnicodeError):
            parsed = None
        if parsed is None:
            raise PedagogyUnavailable(f"Pedagogy API returned HTTP {exc.code}.") from exc
        return exc.code, parsed
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise PedagogyUnavailable(f"Pedagogy local service unavailable: {type(exc).__name__}.") from exc


def _identifier(value: Any, field: str = "session_id") -> str:
    try:
        valid = isinstance(value, str) and str(uuid.UUID(value)) == value and SESSION_ID.fullmatch(value)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{field} must be a canonical Pedagogy session UUID.")
    return value


def _domain_outcome(state: dict[str, Any]) -> str:
    if state.get("review") or state.get("transfer_feedback"):
        return "AUTHORED_FEEDBACK_AVAILABLE"
    if state.get("phase") == "complete":
        return "COMPLETE"
    return "IN_PROGRESS"


def execute(task_id: str, capability: str, context: dict[str, Any]) -> dict[str, Any]:
    """Execute one explicit API operation through a healthy local domain runtime."""
    action_request = context.get("pedagogy_request")
    if not isinstance(action_request, dict):
        raise ValueError("context.pedagogy_request must specify an explicit Pedagogy API operation.")
    action = action_request.get("action")
    if action not in CAPABILITY_ACTIONS.get(capability, set()):
        raise ValueError(f"Action {action!r} is not supported by {capability!r}.")

    try:
        runtime = (
            {"endpoint": service_url(), "managed": False, "reused": True, "health": "configured"}
            if os.environ.get("SYSTEM_AGENTIC_PEDAGOGY_URL")
            else ensure_runtime()
        )
    except PedagogyRuntimeUnavailable as exc:
        raise PedagogyUnavailable(str(exc)) from exc
    endpoint = runtime.get("endpoint")
    if not isinstance(endpoint, str):
        raise PedagogyUnavailable("Pedagogy runtime manager returned no local endpoint.")

    if action == "create":
        case_id = action_request.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("create requires a catalog case_id.")
        payload: dict[str, Any] = {"case_id": case_id, "request_id": action_request.get("request_id", str(uuid.uuid4()))}
        if action_request.get("title") is not None:
            payload["title"] = action_request["title"]
        status, body = request("POST", "/api/sessions", payload, base_url=endpoint)
        state = body.get("session") if isinstance(body, dict) else None
    elif action == "advance":
        session_id = _identifier(action_request.get("session_id"))
        revision = action_request.get("revision")
        event_type, choice = action_request.get("type"), action_request.get("choice")
        if type(revision) is not int or not isinstance(event_type, str) or not isinstance(choice, str):
            raise ValueError("advance requires integer revision, current phase type, and a selected choice.")
        payload = {"revision": revision, "type": event_type, "choice": choice,
                   "request_id": action_request.get("request_id", str(uuid.uuid4()))}
        if "text" in action_request:
            payload["text"] = action_request["text"]
        status, body = request("POST", f"/api/sessions/{session_id}/events", payload, base_url=endpoint)
        state = body.get("session") if isinstance(body, dict) else None
    else:
        session_id = _identifier(action_request.get("session_id"))
        suffix = {"get": "", "feedback": "", "events": "/events", "replay": "/replay", "export": "/export"}[action]
        if action == "export":
            fmt = action_request.get("format", "md")
            if fmt not in {"md", "json"}:
                raise ValueError("export format must be md or json.")
            suffix += "?format=" + fmt
        status, body = request("GET", f"/api/sessions/{session_id}{suffix}", base_url=endpoint)
        if action in {"get", "feedback"}:
            state = body.get("session") if isinstance(body, dict) else None
        elif action == "replay":
            state = None
        elif action == "events":
            state = None
        else:
            state = body if isinstance(body, dict) else None

    if not 200 <= status < 300:
        error = body.get("error", {}) if isinstance(body, dict) else {}
        if status < 500:
            return {
                "task_id": task_id,
                "module": "pedagogy",
                "execution_status": "COMPLETED",
                "capability": capability,
                "domain_outcome": "INPUT_REJECTED",
                "automatic_evaluation": "NOT_APPLICABLE",
                "session_id": action_request.get("session_id"),
                "domain_state": None,
                "feedback": None,
                "replay": None,
                "artifacts": [],
                "warnings": [],
                "errors": [{"code": str(error.get("code", status)),
                            "message": str(error.get("message", "invalid domain request"))}],
                "service": endpoint,
                "runtime": {
                    "managed": runtime.get("managed", False),
                    "reused": runtime.get("reused", False),
                    "pid": runtime.get("pid"),
                    "endpoint": endpoint,
                    "state_root": runtime.get("state_root"),
                    "db_path": runtime.get("db_path"),
                    "interpreter": runtime.get("interpreter"),
                    "startup_seconds": runtime.get("startup_seconds"),
                },
            }
        raise PedagogyUnavailable(f"Pedagogy service returned HTTP {status}.")

    feedback = None
    if action == "feedback":
        if not isinstance(state, dict):
            raise PedagogyUnavailable("Pedagogy feedback response omitted session state.")
        feedback = {key: state.get(key) for key in ("review", "transfer_feedback") if state.get(key) is not None}
        if not feedback:
            feedback = None
    replay = body if action == "replay" and isinstance(body, dict) else None
    session_id = state.get("id") if isinstance(state, dict) else action_request.get("session_id")
    artifact = None
    artifacts = []
    if isinstance(state, dict) and isinstance(session_id, str):
        artifacts.append({"type": "pedagogy-session-reference", "session_id": session_id,
                          "phase": state.get("phase"), "revision": state.get("revision")})
    if action == "export":
        artifact = {"type": "pedagogy-session-export", "session_id": session_id,
                    "format": action_request.get("format", "md"), "content": body}
    if action == "events":
        artifact = {"type": "pedagogy-session-events", "session_id": session_id, "items": body.get("items", [])}
    if artifact:
        artifacts.append(artifact)

    module_result = {
        "task_id": task_id,
        "module": "pedagogy",
        "execution_status": "COMPLETED",
        "capability": capability,
        "domain_outcome": (
            "ARTIFACT_EXPORTED" if action == "export" else
            "SESSION_EVENTS_AVAILABLE" if action == "events" else
            "REPLAY_MATCHED" if replay and replay.get("matches_saved_state") else
            "REPLAY_MISMATCH" if replay else
            _domain_outcome(state) if isinstance(state, dict) else "IN_PROGRESS"
        ),
        "automatic_evaluation": "NOT_APPLICABLE",
        "session_id": session_id,
        "domain_state": ({"phase": state.get("phase"), "revision": state.get("revision"),
                          "case_id": state.get("case_id"), "review_status": state.get("review_status"),
                          "next_prompt": state.get("prompt"), "transcript": state.get("transcript"),
                          "choices": state.get("choices"), "reflection": state.get("reflection"),
                          "review": state.get("review"), "transfer_feedback": state.get("transfer_feedback")}
                         if isinstance(state, dict) else None),
        "feedback": feedback,
        "replay": replay,
        "artifacts": artifacts,
        "warnings": ["Free text is not semantically scored; the authored rehearsal does not certify teaching competence."],
        "errors": [],
        "service": endpoint,
        "runtime": {
            "managed": runtime.get("managed", False),
            "reused": runtime.get("reused", False),
            "pid": runtime.get("pid"),
            "endpoint": endpoint,
            "state_root": runtime.get("state_root"),
            "db_path": runtime.get("db_path"),
            "interpreter": runtime.get("interpreter"),
            "startup_seconds": runtime.get("startup_seconds"),
        },
    }
    return module_result
