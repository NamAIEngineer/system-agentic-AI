#!/usr/bin/env python3
"""Opt-in installer for pinned public reference checkouts.

No network is used without --allow-network. Existing dirty checkouts are never
reset, pulled, overwritten, or deleted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    data = json.loads((ROOT / "sources.manifest.json").read_text(encoding="utf-8"))
    return data["repositories"]


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False, timeout=180)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--install", action="store_true")
    p.add_argument("--allow-network", action="store_true")
    p.add_argument("--id", action="append", dest="ids")
    p.add_argument("--root", type=Path, help="reference directory; default is user-local")
    args = p.parse_args()
    repos = load()
    selected = [r for r in repos if not args.ids or r["id"] in set(args.ids)]
    if not selected:
        print("NO_REFERENCES_SELECTED")
        return 2
    if not args.install:
        print(json.dumps({"status": "CHECK_ONLY", "references": selected}, ensure_ascii=False, indent=2))
        return 0
    if not args.allow_network:
        print("BLOCKED_NETWORK_OPT_IN_REQUIRED", file=sys.stderr)
        return 2
    if not shutil.which("git"):
        print("BLOCKED_GIT_REQUIRED", file=sys.stderr)
        return 2
    env_root = os.environ.get("SYSTEM_AGENTIC_REFERENCES_ROOT", "").strip()
    dest = (args.root or (Path(env_root).expanduser() if env_root else (Path.home() / ".system-agentic-ai" / "references"))).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    failures = 0
    for item in selected:
        target = dest / item["id"]
        if target.exists():
            if not (target / ".git").is_dir():
                print(f"SKIP {item['id']} existing non-git path")
                failures += 1
                continue
            dirty = run(["git", "status", "--porcelain", "--untracked-files=all"], target).stdout.strip()
            if dirty:
                print(f"SKIP {item['id']} dirty checkout preserved")
                failures += 1
                continue
            print(f"KEEP {item['id']} existing checkout preserved")
            continue
        clone = run(["git", "clone", "--branch", item["branch"], "--depth", "1", item["url"], str(target)])
        if clone.returncode != 0:
            print(f"FAIL {item['id']} clone failed")
            failures += 1
            continue
        if item.get("commit"):
            fetched = run(["git", "fetch", "--depth", "1", "origin", item["commit"]], target)
            checked = run(["git", "checkout", "--detach", item["commit"]], target) if fetched.returncode == 0 else fetched
            if checked.returncode != 0:
                print(f"FAIL {item['id']} pinned checkout failed")
                failures += 1
                continue
        head = run(["git", "rev-parse", "HEAD"], target).stdout.strip()
        print(f"READY {item['id']} {head}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
