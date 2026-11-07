#!/usr/bin/env python3
"""Offline cross-platform smoke checks; no network or service mutation."""
from __future__ import annotations

import json
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
    for p in ROOT.rglob("*"):
        if p.is_symlink():
            try:
                p.resolve().relative_to(ROOT)
            except ValueError:
                failures.append(f"external-symlink:{p.relative_to(ROOT)}")
        if p.name in {"node_modules", ".venv", "__pycache__"} or p.suffix in {".pyc", ".pyo", ".sqlite", ".sqlite3", ".db", ".log"}:
            failures.append(f"forbidden-artifact:{p.relative_to(ROOT)}")
    for script in ["scripts/setup.sh", "scripts/doctor.sh", "scripts/smoke-test.sh", "scripts/setup-agent-desktop.sh"]:
        if shutil.which("bash") and (ROOT / script).is_file():
            check = subprocess.run(["bash", "-n", str(ROOT / script)], capture_output=True, text=True)
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
