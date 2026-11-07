#!/usr/bin/env python3
"""Offline cross-platform smoke checks; no network or service mutation."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    failures: list[str] = []
    if sys.version_info < (3, 10):
        failures.append("Python 3.10+ required")
    required = ["AGENTS.md", "CLAUDE.md", "README.md", "WORKFLOW.md", "LICENSE", "VERSION", "THIRD_PARTY_NOTICES.md", "sources.manifest.json", "scripts/setup.py", "scripts/doctor.py", "scripts/install-references.py", "packages/agent-core/package.json"]
    failures.extend(f"missing:{p}" for p in required if not (ROOT / p).is_file())
    for rel in ["sources.manifest.json", "agent-brain/providers/registry.json", "agent-brain/modules/registry.json"]:
        try:
            json.loads((ROOT / rel).read_text(encoding="utf-8"))
        except Exception as exc:
            failures.append(f"invalid-json:{rel}:{type(exc).__name__}")
    for p in ROOT.rglob("*.py"):
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except Exception as exc:
            failures.append(f"python:{p.relative_to(ROOT)}:{type(exc).__name__}")
    # Setup/doctor may create ignored bytecode and local state. Treat those as
    # disposable runtime output; enforce the public boundary against tracked
    # files and any other forbidden artifact that appears in the checkout.
    tracked: set[Path] = set()
    try:
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True)
        tracked = {Path(line) for line in listed.stdout.splitlines() if line}
    except (OSError, subprocess.CalledProcessError):
        tracked = set()
    forbidden_names = {"node_modules", ".venv", "__pycache__"}
    forbidden_suffixes = {".pyc", ".pyo", ".sqlite", ".sqlite3", ".db", ".log"}
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if p.is_symlink():
            try:
                p.resolve().relative_to(ROOT)
            except ValueError:
                failures.append(f"external-symlink:{rel}")
        generated_cache = "__pycache__" in rel.parts or p.name == ".system-agentic-local"
        if generated_cache:
            continue
        is_forbidden = p.name in forbidden_names or p.suffix in forbidden_suffixes
        if is_forbidden:
            label = "tracked-artifact" if rel in tracked or any(parent in tracked for parent in rel.parents) else "forbidden-artifact"
            failures.append(f"{label}:{rel}")
    # Windows CI has no stable POSIX path contract for invoking Git Bash from
    # Python. Unix runners validate shell syntax; Windows validates the native
    # PowerShell/setup paths separately.
    if os.name != "nt":
        for script in ["scripts/setup.sh", "scripts/doctor.sh", "scripts/smoke-test.sh", "scripts/setup-agent-desktop.sh"]:
            if shutil.which("bash") and (ROOT / script).is_file():
                check = subprocess.run(["bash", "-n", script], cwd=ROOT, capture_output=True, text=True)
                if check.returncode:
                    failures.append(f"shell:{script}")
    if failures:
        print("SMOKE_FAIL")
        for item in failures:
            print(item)
        return 1
    print("SMOKE_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
