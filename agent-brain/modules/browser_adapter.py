"""Bounded live browser adapter backed by the installed Playwright runtime.

The adapter owns a disposable browser process/context. Callers provide semantic
actions in task context; arbitrary JavaScript and account writes are rejected.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import signal
import tempfile
from pathlib import Path
from typing import Any

from providers.runtime import ProviderTask

WORKBENCH_ROOT = Path(__file__).resolve().parents[2]
RUNNER = WORKBENCH_ROOT / "agent-brain" / "modules" / "browser_runner.mjs"
PLAYWRIGHT_ROOT = WORKBENCH_ROOT / "design-os-system-one" / "projects" / "voice-browser-agent" / "node_modules"
_BUNDLED_CHROME = Path.home() / "Library/Caches/ms-playwright/chromium-1248/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
CHROME = _BUNDLED_CHROME if _BUNDLED_CHROME.is_file() else Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


class BrowserAdapterError(RuntimeError):
    pass


def browser_health() -> dict[str, Any]:
    return {
        "status": "READY" if RUNNER.is_file() and PLAYWRIGHT_ROOT.is_dir() and CHROME.is_file() else "UNAVAILABLE",
        "runner": str(RUNNER), "playwright_root": str(PLAYWRIGHT_ROOT), "chrome": str(CHROME),
        "runtime": "playwright-node-disposable-context",
    }


def _safe_relative(root: Path, value: object, *, default: str) -> Path:
    raw = value if isinstance(value, str) and value.strip() else default
    candidate = (root / raw).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise BrowserAdapterError("browser artifact path escapes assigned workspace")
    return candidate


def execute_browser_module(task: ProviderTask) -> dict[str, Any]:
    health = browser_health()
    if health["status"] != "READY":
        raise BrowserAdapterError("browser runtime is unavailable")
    context = task.context.get("browser") if isinstance(task.context.get("browser"), dict) else task.context
    url = context.get("url") or context.get("app_url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise BrowserAdapterError("browser task requires an http(s) url")
    actions = context.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 80:
        raise BrowserAdapterError("browser actions must be a bounded list")
    if any(not isinstance(item, dict) or item.get("action") in {"evaluate", "submit", "login"} for item in actions):
        raise BrowserAdapterError("unsupported or unsafe browser action")
    external_service = bool(context.get("external_service", False))
    ownership = str(context.get("ownership") or ("external" if external_service else "adapter-owned"))
    if external_service and any(item.get("action") in {"click", "fill", "select", "press"} for item in actions) and context.get("allow_external_mutation") is not True:
        raise BrowserAdapterError("external service mutation requires explicit allow_external_mutation")
    if any(item.get("action") in {"click", "fill", "select", "press"} for item in actions) and task.execution_policy != "WORKSPACE_WRITE":
        raise BrowserAdapterError("browser mutation actions require explicit WORKSPACE_WRITE")
    artifact_dir = _safe_relative(task.workspace.resolve(strict=True), context.get("artifact_dir"), default=".system-agentic-goals/browser-evidence")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": url, "actions": actions, "headless": bool(context.get("headless", True)),
        "screenshot": str(_safe_relative(task.workspace.resolve(strict=True), context.get("screenshot"), default=".system-agentic-goals/browser-evidence/final.png")),
        "timeout_ms": min(max(int(context.get("timeout_ms", 20_000)), 1_000), 120_000),
        "viewport": context.get("viewport", {"width": 1280, "height": 800}),
        "executable": str(CHROME),
    }
    env = dict(os.environ)
    env["NODE_PATH"] = str(PLAYWRIGHT_ROOT)
    process = subprocess.Popen(["node", str(RUNNER), json.dumps(payload, ensure_ascii=False)], cwd=str(WORKBENCH_ROOT),
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=35)
    except subprocess.TimeoutExpired as exc:
        try: os.killpg(process.pid, signal.SIGTERM)
        except OSError: pass
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid, signal.SIGKILL)
            except OSError: pass
        raise BrowserAdapterError("browser execution timed out; owned context was terminated") from exc
    completed = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BrowserAdapterError("browser runner returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise BrowserAdapterError("browser runner returned an invalid result")
    if result.get("status") != "COMPLETED":
        return {"execution_status": "FAILED", "domain_outcome": "BROWSER_ASSERTION_FAILED",
                "summary": str(result.get("error", "browser assertion failed")),
                "errors": [{"code": "BROWSER_ASSERTION_FAILED", "message": str(result.get("error", "browser assertion failed"))}],
                "external_service": external_service, "ownership": ownership, **result}
    return {"execution_status": "COMPLETED", "domain_outcome": "BROWSER_VERIFIED",
            "summary": "live Playwright browser actions and assertions completed",
            "external_service": external_service, "ownership": ownership, **result}
