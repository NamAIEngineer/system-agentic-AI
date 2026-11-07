#!/usr/bin/env python3
"""Read-only health and compatibility report for a cloned framework."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def add(checks, check_id, status, **extra):
    checks.append({"id": check_id, "status": status, **extra})


def run() -> dict:
    checks = []
    add(checks, "os", "PASS", value=platform.system(), architecture=platform.machine())
    add(checks, "python", "PASS" if sys.version_info >= (3, 10) else "BLOCKED", version=platform.python_version())
    add(checks, "git", "PASS" if shutil.which("git") else "BLOCKED", executable=bool(shutil.which("git")))
    add(checks, "node", "PASS" if shutil.which("node") else "PARTIAL", optional=True, executable=bool(shutil.which("node")))
    required = ["AGENTS.md", "CLAUDE.md", "README.md", "WORKFLOW.md", "LICENSE", "VERSION", "THIRD_PARTY_NOTICES.md", "agent-brain/operating-contract.md", "agent-brain/providers/registry.json", "products/agent-runtime/agentic_runtime.py", "packages/agent-core/package.json", "scripts/setup.py", "scripts/install-references.py"]
    for rel in required:
        add(checks, f"file:{rel}", "PASS" if (ROOT / rel).is_file() else "BLOCKED")
    try:
        manifest = json.loads((ROOT / "sources.manifest.json").read_text(encoding="utf-8"))
        repos = manifest["repositories"]
        valid = isinstance(repos, list) and all({"id", "url", "branch", "commit", "required", "default_install", "license", "purpose"} <= set(x) for x in repos)
        add(checks, "manifest", "PASS" if valid else "BLOCKED", count=len(repos) if isinstance(repos, list) else 0)
    except (OSError, ValueError, TypeError, KeyError):
        add(checks, "manifest", "BLOCKED")
    links = list(ROOT.rglob("*"))
    external = []
    for path in links:
        if path.is_symlink():
            try:
                path.resolve().relative_to(ROOT)
            except ValueError:
                external.append(str(path.relative_to(ROOT)))
    add(checks, "symlinks", "PASS" if not external else "BLOCKED", external=external)
    add(checks, "codex-orca", "PASS" if shutil.which("codex") or shutil.which("orca") else "UNVERIFIED", optional=True)
    add(checks, "agent-desktop", "PASS" if shutil.which("agent-desktop") else "PARTIAL", optional=True)
    add(checks, "qwen", "UNVERIFIED", optional=True, reason="local model is not bundled or started")
    add(checks, "rea", "UNVERIFIED", optional=True, reason="external checkout is not installed by default")
    add(checks, "nine-router", "UNVERIFIED", optional=True, reason="optional external integration; not required")
    blocked = any(c["status"] == "BLOCKED" for c in checks)
    partial = any(c["status"] in {"PARTIAL", "UNVERIFIED"} for c in checks)
    return {"status": "BLOCKED" if blocked else ("PARTIAL" if partial else "READY"), "os": platform.system(), "architecture": platform.machine(), "checks": checks, "notes": ["Read-only doctor: no install, login, service, PATH, credential, or host-config action performed."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="return non-zero for PARTIAL/UNVERIFIED too")
    args = parser.parse_args()
    report = run()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"DOCTOR_{report['status']}")
        for check in report["checks"]:
            print(f"{check['status']} {check['id']}")
        for note in report["notes"]:
            print(f"NOTE {note}")
    return 1 if report["status"] == "BLOCKED" or (args.strict and report["status"] != "READY") else 0


if __name__ == "__main__":
    raise SystemExit(main())
