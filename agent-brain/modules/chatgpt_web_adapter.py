"""Approval-gated live ChatGPT adapter with explicit dynamic routing.

hstry remains retrieval/storage. This module only exchanges bounded requests
through loopback browser bridges. The regular ChatGPT route is always tried
first. The optional Work route is used only after an allowlisted availability
or quota failure, when its own bridge and approval are configured.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit
from typing import Any

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
DEFAULT_STATE_ROOT = Path.home() / "Library" / "Application Support" / "SystemAgenticAI" / "chatgpt-adapter"
DEFAULT_ROUTING_CONFIG = Path(__file__).resolve().parents[1] / "chatgpt-routing.json"
MAX_PROMPT_CHARS = 12_000
MAX_SNIPPET_CHARS = 1_000
DEFAULT_TIMEOUT = 180.0
ROUTE_NAMES = {"regular", "work"}
FALLBACK_ERRORS = frozenset({
    "CHATGPT_WORK_QUOTA_EXHAUSTED", "UNAVAILABLE", "bridge_unavailable",
    "ConnectionError", "TimeoutError", "URLError",
})


def _transport_error(exc: BaseException) -> str:
    """Map transport failures to bounded routing classes.

    HTTP auth, identity, and not-found responses are terminal for the current
    route. They must not be mistaken for Work quota or availability failures.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return "CHATGPT_WORK_QUOTA_EXHAUSTED"
        if exc.code in {408, 502, 503, 504}:
            return "UNAVAILABLE"
        return f"HTTP_{exc.code}"
    if isinstance(exc, TimeoutError):
        return "TimeoutError"
    if isinstance(exc, ConnectionError):
        return "ConnectionError"
    if isinstance(exc, urllib.error.URLError):
        return "URLError"
    if isinstance(exc, ValueError):
        return "bridge_unavailable"
    return type(exc).__name__


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError(f"{field} must be a path-safe identifier")
    return value


def _loopback_url(value: str | None) -> str:
    raw = value or os.environ.get("SYSTEM_AGENTIC_CHATGPT_BRIDGE_URL", "")
    parsed = urlsplit(raw.rstrip("/"))
    try:
        loopback = parsed.hostname is not None and ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if (parsed.scheme != "http" or not loopback or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("ChatGPT bridge URL must be a plain loopback HTTP origin")
    return raw.rstrip("/")


@dataclass(frozen=True)
class ChatGPTExchange:
    run_id: str
    account_id: str
    conversation_id: str | None
    prompt: str
    idempotency_key: str
    approval_id: str | None = None
    channel: str = "regular"


@dataclass(frozen=True)
class ChatGPTRoute:
    name: str
    bridge_url: str | None
    auth_file: str | None
    account_id: str
    conversation_id: str | None
    enabled: bool
    configured: bool
    fallback_errors: frozenset[str]


class ChatGPTWebAdapter:
    """Stateful, approval-gated adapter with regular-first route selection."""

    def __init__(self, state_root: str | Path | None = None, *, bridge_url: str | None = None,
                 timeout_seconds: float = DEFAULT_TIMEOUT,
                 routing_config: str | Path | None = None):
        self.state_root = Path(state_root or os.environ.get(
            "SYSTEM_AGENTIC_CHATGPT_STATE_ROOT", DEFAULT_STATE_ROOT
        )).expanduser()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.bridge_url = bridge_url
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 180.0))
        self.routing_config = Path(routing_config or os.environ.get(
            "SYSTEM_AGENTIC_CHATGPT_ROUTING_CONFIG", DEFAULT_ROUTING_CONFIG
        )).expanduser()
        self._routing = self._load_routing()

    def _load_routing(self) -> dict[str, Any]:
        try:
            value = json.loads(self.routing_config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        routes = value.get("routes") if isinstance(value.get("routes"), dict) else {}
        fallback_routes = value.get("fallback_routes")
        if not isinstance(fallback_routes, list):
            fallback_routes = ["work"]
        fallback_errors = value.get("fallback_errors")
        if not isinstance(fallback_errors, list):
            fallback_errors = list(FALLBACK_ERRORS)
        return {
            "default_route": str(value.get("default_route") or "regular"),
            "fallback_routes": [str(item) for item in fallback_routes if isinstance(item, str)],
            "fallback_errors": frozenset(str(item) for item in fallback_errors if isinstance(item, str)),
            "routes": routes,
        }

    @staticmethod
    def _route_name(value: Any) -> str:
        name = str(value or "regular").strip().lower()
        if name not in ROUTE_NAMES:
            raise ValueError("channel must be regular or work")
        return name

    def _route(self, channel: str, request: dict[str, Any] | None = None) -> ChatGPTRoute:
        request = request or {}
        name = self._route_name(channel)
        raw = self._routing.get("routes", {}).get(name, {})
        if not isinstance(raw, dict):
            raw = {}
        bridge_env = raw.get("bridge_url_env")
        auth_env = raw.get("auth_file_env")
        bridge_url = self.bridge_url if name == "regular" and self.bridge_url else None
        if bridge_url is None and isinstance(bridge_env, str):
            bridge_url = os.environ.get(bridge_env) or None
        auth_file = os.environ.get(auth_env) if isinstance(auth_env, str) else None
        account_env = raw.get("account_id_env")
        configured_account = os.environ.get(account_env) if isinstance(account_env, str) else None
        supplied_account = request.get("account_id") if name == "regular" else (
            request.get("work_account_id") or request.get("account_id")
        )
        account_id = str(supplied_account or configured_account or raw.get("default_account_id") or "")
        conversation_env = raw.get("conversation_id_env")
        configured_conversation = os.environ.get(conversation_env) if isinstance(conversation_env, str) else None
        supplied_conversation = request.get("conversation_id") if name == "regular" else request.get("work_conversation_id")
        conversation_id = supplied_conversation or configured_conversation
        enabled = raw.get("enabled", True) is True
        # Every route needs its own loopback bridge.  A regular route with no
        # URL must be reported unavailable instead of reaching _request with
        # an implicit environment fallback and producing an ambiguous error.
        configured = bool(enabled and account_id and bridge_url)
        return ChatGPTRoute(name, bridge_url, auth_file, account_id, conversation_id,
                            enabled, configured, self._routing["fallback_errors"] or FALLBACK_ERRORS)

    def routing_status(self) -> dict[str, Any]:
        routes: dict[str, Any] = {}
        for name in sorted(ROUTE_NAMES):
            route = self._route(name, {})
            routes[name] = {
                "enabled": route.enabled,
                "configured": route.configured and bool(route.bridge_url),
                "account_id": route.account_id if route.configured and bool(route.bridge_url) else None,
                "conversation_configured": bool(route.conversation_id),
            }
        return {"default_route": self._routing["default_route"],
                "fallback_routes": list(self._routing["fallback_routes"]), "routes": routes}

    def _path(self, name: str) -> Path:
        return self.state_root / f"{_safe_id(name, 'state')}.json"

    def _save(self, name: str, value: dict[str, Any]) -> str:
        target = self._path(name)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)
        return str(target)

    def _existing_receipt(self, idempotency_key: str, route: ChatGPTRoute | None = None) -> dict[str, Any] | None:
        for path in sorted(self.state_root.glob("exchange-*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict) or value.get("idempotency_key") != idempotency_key:
                continue
            if value.get("status") not in {"COMPLETED", "FAILED", "UNKNOWN", "BUSY", "RUNNING", "SUBMITTED", "PENDING"}:
                continue
            if (value.get("status") == "FAILED" and not value.get("error")
                    and isinstance(value.get("request_id"), str)):
                try:
                    bridge_value = self._request("GET", f"/v1/chatgpt/exchanges/{value['request_id']}",
                                                 bridge_url=route.bridge_url if route else None,
                                                 auth_file=route.auth_file if route else None)
                    if isinstance(bridge_value, dict):
                        for key in ("error", "conversation_id", "url", "message_id", "created_at",
                                    "source", "source_id", "snippet", "retry_count"):
                            if bridge_value.get(key) is not None:
                                value[key] = bridge_value[key]
                except (OSError, ValueError, RuntimeError, urllib.error.URLError, urllib.error.HTTPError):
                    pass
            return {**value, "receipt_ref": str(path), "reused_receipt": True}
        return None

    def _poll(self, request_id: str, route: ChatGPTRoute | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        result: dict[str, Any] = {"request_id": request_id, "status": "RUNNING"}
        while result.get("status") in {"SUBMITTED", "RUNNING", "PENDING"} and time.monotonic() < deadline:
            time.sleep(0.25)
            result = self._request("GET", f"/v1/chatgpt/exchanges/{request_id}",
                                   bridge_url=route.bridge_url if route else None,
                                   auth_file=route.auth_file if route else None)
        return result

    def _receipt(self, exchange: ChatGPTExchange, result: dict[str, Any], *,
                 reused: bool = False, route_attempts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        request_id = _safe_id(result.get("request_id"), "request_id")
        receipt = {"status": result.get("status", "UNKNOWN"), "adapter": "chatgpt-web",
                   "request_id": request_id, "run_id": exchange.run_id,
                   "account_id": exchange.account_id, "channel": exchange.channel,
                   "conversation_id": result.get("conversation_id", exchange.conversation_id),
                   "message_id": result.get("message_id"), "created_at": result.get("created_at"),
                   "url": result.get("url"), "source": result.get("source", "chatgpt-web"),
                   "source_id": result.get("source_id"), "idempotency_key": exchange.idempotency_key,
                   "snippet": str(result.get("snippet", ""))[:MAX_SNIPPET_CHARS], "content_sent": True}
        if result.get("retry_count") is not None:
            receipt["retry_count"] = result.get("retry_count")
        if result.get("error") is not None:
            receipt["error"] = str(result.get("error"))[:200]
        if reused:
            receipt["reused_receipt"] = True
        if route_attempts:
            receipt["route_attempts"] = route_attempts[-8:]
        suffix = "" if exchange.channel == "regular" else f"-{exchange.channel}"
        return {**receipt, "receipt_ref": self._save(f"exchange-{exchange.run_id}{suffix}", receipt)}

    @staticmethod
    def _idempotency(account_id: str, conversation_id: str | None, prompt: str,
                     run_id: str, channel: str = "regular") -> str:
        material = "\x1f".join((channel, account_id, conversation_id or "", prompt, run_id))
        return "chatgpt-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def validate(self, request: dict[str, Any]) -> ChatGPTExchange:
        run_id = _safe_id(request.get("run_id") or str(uuid.uuid4()), "run_id")
        account_id = _safe_id(request.get("account_id"), "account_id")
        conversation_id = request.get("conversation_id")
        if conversation_id is not None:
            conversation_id = _safe_id(conversation_id, "conversation_id")
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt exceeds {MAX_PROMPT_CHARS} characters")
        channel = self._route_name(request.get("channel") or request.get("provider_route") or "regular")
        idempotency_key = request.get("idempotency_key")
        if idempotency_key is None:
            idempotency_key = self._idempotency(account_id, conversation_id, prompt, run_id, channel)
        idempotency_key = _safe_id(idempotency_key, "idempotency_key")
        approval_id = request.get("approval_id")
        if approval_id is not None:
            approval_id = _safe_id(approval_id, "approval_id")
        return ChatGPTExchange(run_id, account_id, conversation_id, prompt, idempotency_key,
                               approval_id, channel)

    def dry_run(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            exchange = self.validate(request)
        except (TypeError, ValueError) as exc:
            return {"status": "INPUT_INVALID", "error": str(exc), "adapter": "chatgpt-web"}
        routing = self.routing_status()
        receipt = {"status": "DRY_RUN", "adapter": "chatgpt-web", "run_id": exchange.run_id,
                   "account_id": exchange.account_id, "conversation_id": exchange.conversation_id,
                   "channel": exchange.channel, "idempotency_key": exchange.idempotency_key,
                   "approval_required": True,
                   "bridge_configured": routing["routes"][exchange.channel]["configured"],
                   "routing": routing, "created_at": _now(), "content_sent": False}
        return {**receipt, "receipt_ref": self._save(f"dry-run-{exchange.run_id}", receipt)}

    def record_approval(self, request: dict[str, Any], *, approval_id: str, approved_by: str) -> dict[str, Any]:
        exchange = self.validate(request)
        approval_id = _safe_id(approval_id, "approval_id")
        if not isinstance(approved_by, str) or not approved_by.strip() or len(approved_by) > 200:
            raise ValueError("approved_by must be a bounded non-empty string")
        receipt = {"status": "APPROVED", "approval_id": approval_id, "approved_by": approved_by[:200],
                   "approved_at": _now(), "run_id": exchange.run_id, "account_id": exchange.account_id,
                   "conversation_id": exchange.conversation_id, "idempotency_key": exchange.idempotency_key,
                   "channel": exchange.channel}
        return {**receipt, "receipt_ref": self._save(f"approval-{approval_id}", receipt)}

    def _approval(self, exchange: ChatGPTExchange) -> dict[str, Any] | None:
        if not exchange.approval_id:
            return None
        try:
            value = json.loads(self._path(f"approval-{exchange.approval_id}").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if (value.get("status") != "APPROVED" or value.get("run_id") != exchange.run_id
                or value.get("account_id") != exchange.account_id
                or value.get("idempotency_key") != exchange.idempotency_key
                or value.get("channel", "regular") != exchange.channel):
            return None
        return value

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None,
                 *, bridge_url: str | None = None, auth_file: str | None = None) -> dict[str, Any]:
        base = _loopback_url(bridge_url or self.bridge_url)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        auth_path = auth_file or os.environ.get("SYSTEM_AGENTIC_CHATGPT_BRIDGE_AUTH_FILE", "")
        if auth_path:
            try:
                token = Path(auth_path).expanduser().read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                token = ""
            if token:
                headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=self.timeout_seconds) as response:
            raw = response.read(32 * 1024 + 1)
            if len(raw) > 32 * 1024:
                raise RuntimeError("ChatGPT bridge response exceeded bounded size")
            value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("ChatGPT bridge returned a non-object response")
        return value

    def _send_once(self, exchange: ChatGPTExchange, route: ChatGPTRoute,
                   *, idempotency_key: str | None = None, approval_id: str | None = None,
                   route_attempts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        key = idempotency_key or exchange.idempotency_key
        effective = ChatGPTExchange(exchange.run_id, route.account_id,
                                    route.conversation_id or exchange.conversation_id,
                                    exchange.prompt, key, approval_id or exchange.approval_id, route.name)
        attempt = {"channel": route.name, "status": "NOT_STARTED", "configured": bool(route.bridge_url),
                   "account_id": route.account_id if route.bridge_url else None}
        # Keep regular exchange tests and explicit constructor injection
        # usable without an environment URL. The actual bridge call remains
        # fail-closed in _request; only Work is unavailable until its own URL
        # is explicitly configured.
        if not route.enabled or (not route.bridge_url and route.name == "work"):
            attempt.update({"status": "UNAVAILABLE", "error": "route_not_configured"})
            return {"status": "UNAVAILABLE", "adapter": "chatgpt-web", "run_id": exchange.run_id,
                    "account_id": route.account_id, "channel": route.name,
                    "conversation_id": effective.conversation_id, "idempotency_key": key,
                    "content_sent": False, "error": "route_not_configured",
                    "route_attempts": (route_attempts or []) + [attempt]}
        if self._approval(effective) is None:
            attempt.update({"status": "APPROVAL_REQUIRED", "error": "approval_required"})
            return {"status": "APPROVAL_REQUIRED", "adapter": "chatgpt-web", "run_id": exchange.run_id,
                    "account_id": route.account_id, "channel": route.name,
                    "conversation_id": effective.conversation_id, "idempotency_key": key,
                    "content_sent": False, "error": "approval_required",
                    "route_attempts": (route_attempts or []) + [attempt]}
        existing = self._existing_receipt(key, route)
        if existing is not None:
            request_id = existing.get("request_id")
            if existing.get("status") in {"RUNNING", "SUBMITTED", "PENDING"} and isinstance(request_id, str):
                try:
                    return self._receipt(effective, self._poll(request_id, route), reused=True,
                                         route_attempts=(route_attempts or []) + [attempt])
                except (OSError, ValueError, RuntimeError, urllib.error.URLError, urllib.error.HTTPError) as exc:
                    return {**existing, "status": "RUNNING", "error": type(exc).__name__, "reused_receipt": True}
            return existing
        try:
            submitted = self._request("POST", "/v1/chatgpt/exchanges", {
                "account_id": route.account_id, "conversation_id": effective.conversation_id,
                "prompt": exchange.prompt, "idempotency_key": key,
            }, bridge_url=route.bridge_url, auth_file=route.auth_file)
            request_id = _safe_id(submitted.get("request_id"), "request_id")
            result = self._poll(request_id, route)
            attempt.update({"status": result.get("status", "UNKNOWN"), "request_id": request_id,
                            "error": result.get("error")})
            return self._receipt(effective, result, route_attempts=(route_attempts or []) + [attempt])
        except (OSError, ValueError, RuntimeError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            # URL/configuration parsing errors are transport unavailability;
            # conversation mismatch and unknown outcomes come back as bounded
            # bridge receipts and are never rewritten here.
            error = _transport_error(exc)
            attempt.update({"status": "UNAVAILABLE", "error": error})
            return {"status": "UNAVAILABLE", "adapter": "chatgpt-web", "run_id": exchange.run_id,
                    "account_id": route.account_id, "channel": route.name,
                    "conversation_id": effective.conversation_id, "idempotency_key": key,
                    "content_sent": False, "error": error,
                    "route_attempts": (route_attempts or []) + [attempt]}

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            exchange = self.validate(request)
        except (TypeError, ValueError) as exc:
            return {"status": "INPUT_INVALID", "error": str(exc), "adapter": "chatgpt-web"}
        route = self._route(exchange.channel, request)
        result = self._send_once(exchange, route)
        attempts = result.get("route_attempts") if isinstance(result.get("route_attempts"), list) else []
        error = result.get("error")
        if (request.get("fallback_enabled") is True and exchange.channel == "regular"
                and result.get("status") in {"UNAVAILABLE", "FAILED"}
                and isinstance(error, str) and error in route.fallback_errors):
            for fallback_name in self._routing["fallback_routes"]:
                fallback_route = self._route(fallback_name, request)
                fallback_key = request.get("fallback_idempotency_key")
                fallback_approval = request.get("fallback_approval_id")
                if not isinstance(fallback_key, str) or not isinstance(fallback_approval, str):
                    return {**result, "fallback_required": fallback_name, "routing_status": self.routing_status()}
                work_exchange = ChatGPTExchange(exchange.run_id, fallback_route.account_id,
                                                fallback_route.conversation_id or exchange.conversation_id,
                                                exchange.prompt, fallback_key, fallback_approval, fallback_name)
                fallback_result = self._send_once(work_exchange, fallback_route,
                                                  idempotency_key=fallback_key,
                                                  approval_id=fallback_approval,
                                                  route_attempts=attempts)
                return {**fallback_result, "fallback_from": "regular",
                        "routing_status": self.routing_status()}
        result["route_attempts"] = attempts
        result["routing_status"] = self.routing_status()
        return result


def dry_run(request: dict[str, Any], *, state_root: str | Path | None = None) -> dict[str, Any]:
    return ChatGPTWebAdapter(state_root=state_root).dry_run(request)
