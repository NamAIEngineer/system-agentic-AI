"""Opt-in, local authenticated extension transport for browser.automation.

No model/provider call, connection discovery, account writes, or replay fallback.
The interactive browser user must pair the tab before a task can observe it.
"""
from __future__ import annotations

import hashlib
import json
import stat
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from providers.runtime import ProviderTask

SESSION_ROOT = Path(__file__).resolve().parents[2] / ".system-agentic-local" / "browser-extension"
CONNECTION = SESSION_ROOT / "session" / "connection.json"


class ExtensionTransportError(RuntimeError):
    def __init__(self, message: str, *, status: str = "FAILED", request_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.request_id = request_id
        self.evidence = []


def _connection() -> dict:
    if CONNECTION.is_symlink() or not CONNECTION.is_file():
        raise ExtensionTransportError("extension NOT_PAIRED; start local bridge and pair a tab")
    mode = CONNECTION.stat().st_mode
    if stat.S_IMODE(mode) & 0o077:
        raise ExtensionTransportError("extension connection file must be private (0600)")
    config = json.loads(CONNECTION.read_text())
    if not isinstance(config, dict):
        raise ExtensionTransportError("invalid extension connection configuration")
    port, token = config.get("port"), config.get("controllerToken")
    if type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str) or len(token) != 64:
        raise ExtensionTransportError("invalid extension connection configuration")
    return config


def _rpc(config: dict, op: str, *, request_id: str | None = None, **fields) -> dict:
    payload = {"version": 1, "id": request_id or str(uuid.uuid4()), "op": op, **fields}
    request = urllib.request.Request(
        f"http://127.0.0.1:{config['port']}/rpc", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {config['controllerToken']}", "Content-Type": "application/json"}, method="POST")
    try:
        # A machine proxy must never receive local pairing credentials.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=18) as response:
            body = response.read(256001)
            if len(body) > 256000:
                raise ValueError("extension response exceeds limit")
            result = json.loads(body)
    except (OSError, ValueError, urllib.error.HTTPError) as error:
        raise ExtensionTransportError("extension request unavailable/uncertain; do not retry a mutation", status="UNCERTAIN" if op == "act" else "FAILED", request_id=payload["id"]) from error
    if not isinstance(result, dict):
        raise ExtensionTransportError("invalid extension receipt", status="UNCERTAIN" if op == "act" else "FAILED", request_id=payload["id"])
    if result.get("status") != "COMPLETED":
        raise ExtensionTransportError(f"extension {result.get('status', 'FAILED')}: {result.get('error', 'invalid receipt')}",
                                      status=result.get("status", "FAILED"), request_id=payload["id"])
    return result


def extension_health() -> dict:
    try:
        config = _connection()
        result = _rpc(config, "capabilities")
        return {"status": "READY" if result.get("paired") else "NOT_PAIRED", "capabilities": result["capabilities"]}
    except (ExtensionTransportError, OSError, ValueError) as error:
        return {"status": "NOT_PAIRED", "reason": str(error)}


def execute_extension_browser(task: ProviderTask, context: dict) -> dict:
    actions = context.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 40:
        raise ExtensionTransportError("extension actions must be a list of at most 40")
    if actions and task.execution_policy != "WORKSPACE_WRITE":
        raise ExtensionTransportError("extension mutation requires WORKSPACE_WRITE")
    if actions and context.get("allow_external_mutation") is not True:
        raise ExtensionTransportError("paired-tab mutation requires explicit allow_external_mutation")
    origins = context.get("authorized_origins", [])
    if not isinstance(origins, list) or not origins or any(not isinstance(o, str) for o in origins):
        raise ExtensionTransportError("extension requires explicit authorized_origins even for reads")
    expected = context.get("expect_text")
    if actions and (not isinstance(expected, str) or not expected.strip()):
        raise ExtensionTransportError("mutation requires a final expect_text postcondition")
    for action in actions:
        if not isinstance(action, dict) or action.get("action") not in {"click", "fill"}:
            raise ExtensionTransportError("extension supports semantic click/fill only")
        if not isinstance(action.get("name"), str) or not action["name"] or not isinstance(action.get("role"), str):
            raise ExtensionTransportError("each action requires exact role and name")
        if action["action"] == "fill" and (not isinstance(action.get("text"), str) or len(action["text"]) > 10000):
            raise ExtensionTransportError("fill text must be bounded")
    config = _connection()
    evidence = []

    def observe():
        observed = _rpc(config, "observe")
        if not isinstance(observed.get("url"), str) or not isinstance(observed.get("elements"), list):
            raise ExtensionTransportError("invalid extension observation")
        url = urlsplit(observed["url"])
        origin = f"{url.scheme}://{url.netloc}"
        if url.scheme not in {"http", "https"} or origin not in origins:
            raise ExtensionTransportError("paired origin is outside authorized_origins")
        return observed

    observed = observe()
    try:
        for index, action in enumerate(actions):
            matches = [e for e in observed["elements"] if e["role"] == action["role"] and e["name"] == action["name"]]
            if len(matches) != 1 or matches[0].get("disabled"):
                raise ExtensionTransportError("semantic target must be unique and enabled")
            # Stable mutation IDs prevent accidental replay when callers reconcile a task.
            request_id = hashlib.sha256(f"{task.task_id}:{index}".encode()).hexdigest()
            fields = {"action": action["action"], "ref": matches[0]["ref"], **{k: observed[k] for k in ("lease", "epoch", "pageId")}}
            if action["action"] == "fill":
                fields["text"] = action["text"]
            receipt = _rpc(config, "act", request_id=request_id, **fields)
            evidence.append({"index": index, "action": action["action"], "receipt_id": receipt["id"], "status": receipt["status"]})
            observed = observe()
        if expected and expected not in observed.get("text", ""):
            raise ExtensionTransportError("browser postcondition failed; reconcile state before any further write")
    except ExtensionTransportError as error:
        error.evidence = evidence
        raise
    return {"execution_status": "COMPLETED", "domain_outcome": "BROWSER_VERIFIED" if expected else "BROWSER_OBSERVED",
            "summary": "paired browser state and explicit postcondition verified" if expected else "paired browser observed",
            "external_service": True, "ownership": "user-paired-tab", "transport": "extension",
            "evidence": evidence, "observation": observed}
