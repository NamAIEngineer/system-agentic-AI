#!/usr/bin/env python3
"""Safe, cross-platform local bootstrap for System Agentic AI.

Default check-only mode is read-only. Install mode writes only ignored local
state under this checkout and never changes PATH, shells, credentials, global
packages, services, ORCA, 9router, or browser profiles.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true", help="read-only prerequisite and layout check")
    mode.add_argument("--install", action="store_true", help="create local ignored config/state")
    p.add_argument("--skip-references", action="store_true", help="do not install optional references")
    p.add_argument("--references", action="store_true", help="opt in to reference installer after local setup")
    p.add_argument("--json", action="store_true", help="emit one JSON result")
    return p


def required_files() -> list[str]:
    return [
        "AGENTS.md", "CLAUDE.md", "README.md", "WORKFLOW.md", "LICENSE", "VERSION",
        "agent-brain/operating-contract.md", "agent-brain/providers/registry.json",
        "agent-brain/skills/system-agentic-ai/SKILL.md", "products/agent-runtime/agentic_runtime.py",
        "packages/agent-core/package.json", "sources.manifest.json",
        "scripts/doctor.py", "scripts/system-agentic", "scripts/agentic-runtime",
    ]


def result(status: str, checks: list[dict[str, object]], messages: list[str]) -> dict[str, object]:
    return {"status": status, "os": platform.system(), "architecture": platform.machine(), "checks": checks, "messages": messages}


def check() -> dict[str, object]:
    checks: list[dict[str, object]] = []
    messages: list[str] = []
    py_ok = sys.version_info >= (3, 10)
    checks.append({"id": "python", "status": "PASS" if py_ok else "BLOCKED", "version": platform.python_version()})
    git = shutil.which("git")
    checks.append({"id": "git", "status": "PASS" if git else "BLOCKED", "executable": bool(git)})
    for rel in required_files():
        ok = (ROOT / rel).is_file()
        checks.append({"id": f"file:{rel}", "status": "PASS" if ok else "BLOCKED"})
    try:
        data = json.loads((ROOT / "sources.manifest.json").read_text(encoding="utf-8"))
        repos = data.get("repositories")
        manifest_ok = isinstance(repos, list) and all(isinstance(x, dict) and x.get("id") and x.get("url") for x in repos)
    except (OSError, ValueError, TypeError):
        manifest_ok = False
    checks.append({"id": "manifest", "status": "PASS" if manifest_ok else "BLOCKED"})
    node = shutil.which("node")
    checks.append({"id": "node", "status": "PASS" if node else "PARTIAL", "optional": True, "available": bool(node)})
    checks.append({"id": "orca", "status": "PASS" if shutil.which("orca") else "PARTIAL", "optional": True})
    checks.append({"id": "agent-desktop", "status": "PASS" if shutil.which("agent-desktop") else "PARTIAL", "optional": True})
    blocked = any(c["status"] == "BLOCKED" for c in checks)
    if not py_ok:
        messages.append("Python 3.10+ is required")
    if not git:
        messages.append("Git is required for optional references")
    messages.append("No global package, provider, service, ORCA, 9router, browser, or credential change is performed by check-only")
    return result("BLOCKED" if blocked else ("PARTIAL" if any(c["status"] == "PARTIAL" for c in checks) else "READY"), checks, messages)


def install_local() -> list[str]:
    local = ROOT / ".system-agentic-local"
    local.mkdir(mode=0o700, exist_ok=True)
    env = ROOT / ".env"
    if not env.exists():
        shutil.copy2(ROOT / ".env.example", env)
    state = {"schema_version": 1, "root": str(ROOT), "references_installed": False}
    (local / "setup-state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return ["Created ignored local .env and .system-agentic-local/setup-state.json"]


def main() -> int:
    args = parser().parse_args()
    if not args.check_only and not args.install:
        args.check_only = True
    report = check()
    if args.install:
        if report["status"] == "BLOCKED":
            report["messages"].append("Install refused because core prerequisites/layout are blocked")
        else:
            report["messages"].extend(install_local())
            if args.references and not args.skip_references:
                report["messages"].append("Reference installation is explicit; run scripts/install-references.py --install --allow-network")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"SETUP_{report['status']}")
        for item in report["messages"]:
            print(f"- {item}")
    return 1 if report["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
