#!/usr/bin/env python3
"""Read-only health report for a System Agentic AI checkout.

The top-level status covers only the portable framework core. Optional
references, Quyet, providers, and host execution are reported separately so
an absent optional integration cannot make the core look broken.
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def add(checks: list[dict[str, Any]], check_id: str, status: str, **extra: Any) -> None:
    checks.append({"id": check_id, "status": status, **extra})


def load_manifest() -> tuple[list[dict[str, Any]], str | None]:
    try:
        data = json.loads((ROOT / "sources.manifest.json").read_text(encoding="utf-8"))
        repos = data.get("repositories")
        required = {"id", "url", "branch", "commit", "required", "default_install", "license", "purpose"}
        if not isinstance(repos, list) or len(repos) != 7:
            return [], "manifest must contain exactly seven references"
        for item in repos:
            if not isinstance(item, dict) or not required.issubset(item):
                return [], "manifest entry is incomplete"
            if not isinstance(item["commit"], str) or len(item["commit"]) != 40:
                return [], f"{item.get('id', '<unknown>')} has no pinned commit"
        return repos, None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return [], type(exc).__name__


def read_reference_state() -> tuple[str, dict[str, Any]]:
    state_path = ROOT / ".system-agentic-local" / "references-installation.json"
    if not state_path.is_file():
        return "NOT_INSTALLED", {"state_file": str(state_path)}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        items = state.get("items", {})
        if not isinstance(items, dict) or len(items) == 0:
            return "NOT_INSTALLED", {"state_file": str(state_path)}
        statuses = [str(item.get("status", "UNKNOWN")) for item in items.values() if isinstance(item, dict)]
        verified = [bool(item.get("pin_verified")) for item in items.values() if isinstance(item, dict)]
        if len(items) >= 7 and all(verified) and all(status.startswith(("READY", "PINNED")) for status in statuses):
            value = "INSTALLED"
        elif any(status.startswith(("UNKNOWN", "BLOCKED")) for status in statuses):
            value = "BLOCKED"
        else:
            value = "PARTIAL"
        return value, {"state_file": str(state_path), "items": len(items), "pin_verified": sum(verified)}
    except (OSError, ValueError, TypeError):
        return "BLOCKED", {"state_file": str(state_path), "reason": "invalid receipt"}


def run_quyet_check() -> dict[str, Any]:
    script = ROOT / "scripts" / "setup-quyet.py"
    if not script.is_file():
        return {"status": "UNAVAILABLE", "reason": "setup-quyet.py missing"}
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "--check-only", "--json"],
            cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
        )
        value = json.loads(completed.stdout) if completed.stdout.strip() else {}
        return value if isinstance(value, dict) else {"status": "ERROR", "reason": "invalid Quyet report"}
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
        return {"status": "ERROR", "reason": type(exc).__name__}


def optional_providers() -> dict[str, dict[str, Any]]:
    providers: dict[str, dict[str, Any]] = {}
    for provider, reason in (
        ("qwen", "local model/runtime is not bundled or started"),
        ("rea", "external checkout is optional and not installed by default"),
        ("nine-router", "optional external integration; never required by core"),
        ("chatgpt-bridge", "approval-gated external bridge; credentials are not bundled"),
    ):
        providers[provider] = {"status": "UNVERIFIED", "reason": reason}
    providers["agent-desktop"] = {
        "status": "UNVERIFIED" if shutil.which("agent-desktop") else "NOT_CONFIGURED",
        "executable": bool(shutil.which("agent-desktop")),
        "reason": "binary is user-installed and execution requires a desktop session",
    }
    return providers


def run() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    add(checks, "os", "PASS", value=platform.system(), architecture=platform.machine())
    add(checks, "python", "PASS" if sys.version_info >= (3, 10) else "BLOCKED", version=platform.python_version())
    add(checks, "git", "PASS" if shutil.which("git") else "BLOCKED", executable=bool(shutil.which("git")))
    required = [
        "AGENTS.md", "CLAUDE.md", "README.md", "WORKFLOW.md", "LICENSE", "VERSION",
        "THIRD_PARTY_NOTICES.md", "agent-brain/operating-contract.md",
        "agent-brain/providers/registry.json", "agent-brain/modules/registry.json",
        "products/agent-runtime/agentic_runtime.py", "packages/agent-core/package.json",
        "scripts/setup.py", "scripts/install-references.py", "scripts/setup-quyet.py",
    ]
    for rel in required:
        add(checks, f"file:{rel}", "PASS" if (ROOT / rel).is_file() else "BLOCKED")

    repos, manifest_error = load_manifest()
    add(checks, "manifest", "PASS" if manifest_error is None else "BLOCKED", count=len(repos), error=manifest_error)

    external: list[str] = []
    for path in ROOT.rglob("*"):
        if path.is_symlink():
            try:
                path.resolve().relative_to(ROOT)
            except ValueError:
                external.append(str(path.relative_to(ROOT)))
    add(checks, "symlinks", "PASS" if not external else "BLOCKED", external=external)

    core_status = "BLOCKED" if any(item["status"] == "BLOCKED" for item in checks) else "READY"
    references_status, references_detail = read_reference_state()
    quyet = run_quyet_check()
    quyet_status = str(quyet.get("status", "UNAVAILABLE"))
    host_commands = [name for name in ("orca", "codex") if shutil.which(name)]
    host_execution = {
        "status": "UNVERIFIED" if host_commands else "UNSUPPORTED",
        "commands": host_commands,
        "reason": "command presence does not prove a real host task executed" if host_commands else "no ORCA/Codex executable detected",
    }
    return {
        "status": core_status, "core_status": core_status,
        "references_status": references_status, "references": references_detail,
        "quyet_status": quyet_status, "quyet": quyet,
        "optional_providers": optional_providers(), "host_execution_status": host_execution,
        "os": platform.system(), "architecture": platform.machine(), "checks": checks,
        "notes": [
            "Read-only doctor: no install, login, service, PATH, credential, or host-config action performed.",
            "Optional status does not downgrade core_status.",
            "Classifier output is advisory and never grants execution authority.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="return non-zero when core_status is not READY")
    args = parser.parse_args()
    report = run()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"DOCTOR_{report['status']}")
        print(f"CORE_STATUS={report['core_status']}")
        print(f"REFERENCES_STATUS={report['references_status']}")
        print(f"QUYET_STATUS={report['quyet_status']}")
        print(f"HOST_EXECUTION_STATUS={report['host_execution_status']['status']}")
        for check in report["checks"]:
            print(f"{check['status']} {check['id']}")
        for name, value in report["optional_providers"].items():
            print(f"OPTIONAL_PROVIDER {name} {value['status']}")
        for note in report["notes"]:
            print(f"NOTE {note}")
    return 1 if report["core_status"] == "BLOCKED" or (args.strict and report["core_status"] != "READY") else 0


if __name__ == "__main__":
    raise SystemExit(main())
