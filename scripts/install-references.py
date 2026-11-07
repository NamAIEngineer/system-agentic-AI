#!/usr/bin/env python3
"""Install optional public references into an isolated user root.

Network requires ``--allow-network``. Existing dirty/non-Git checkouts are
preserved. No reference lifecycle hook or installer is executed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_VERSION = 2
TIMEOUT = 180


def load_manifest() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = json.loads((ROOT / "sources.manifest.json").read_text(encoding="utf-8"))
    repositories = data.get("repositories")
    required = {"id", "url", "branch", "commit", "required", "default_install", "license", "purpose"}
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("sources.manifest.json has no repositories")
    for item in repositories:
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError("reference manifest entry is incomplete")
        if not isinstance(item["commit"], str) or len(item["commit"]) != 40:
            raise ValueError(f"{item.get('id')} must have a pinned 40-character commit")
    return data, repositories


def run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"TIMEOUT after {TIMEOUT}s")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def default_root() -> Path:
    configured = os.environ.get("SYSTEM_AGENTIC_REFERENCES_ROOT", "").strip()
    return (Path(configured).expanduser() if configured else Path.home() / ".system-agentic-ai" / "references").resolve()


def choose(repositories: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.all and args.ids:
        raise ValueError("use --all or --id, not both")
    if args.all or not args.install:
        return list(repositories)
    requested = set(args.ids or [])
    unknown = requested.difference({r["id"] for r in repositories})
    if unknown:
        raise ValueError(f"unknown reference id(s): {', '.join(sorted(unknown))}")
    return [r for r in repositories if r["id"] in requested]


def make_receipt(item: dict[str, Any], target: Path, status: str, *, head: str | None = None, message: str = "") -> dict[str, Any]:
    capabilities = item.get("capabilities") if isinstance(item.get("capabilities"), list) else []
    return {
        "id": item["id"], "url": item["url"], "branch": item["branch"],
        "expected_commit": item["commit"], "license": item["license"],
        "target": str(target), "status": status, "head": head,
        "source_cloned": (target / ".git").exists(),
        "pin_verified": bool(head and head == item["commit"]),
        "dependencies": "NOT_CHECKED", "capabilities": capabilities,
        "capability_registered": bool(capabilities),
        "capability_registration": "DECLARATION_RECORDED" if capabilities else "NOT_DECLARED",
        "execution_verified": False, "message": message,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check-only", action="store_true", help="print the plan; no writes/network")
    p.add_argument("--install", action="store_true", help="clone selected references")
    p.add_argument("--all", action="store_true", help="select all seven references")
    p.add_argument("--allow-network", action="store_true", help="explicitly allow public Git downloads")
    p.add_argument("--id", action="append", dest="ids", help="select one reference; repeatable")
    p.add_argument("--root", type=Path, help="references root; default user-local")
    p.add_argument("--state-root", type=Path, help="ignored local state root")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    try:
        manifest, repositories = load_manifest()
        selected = choose(repositories, args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"MANIFEST_BLOCKED: {exc}", file=sys.stderr)
        return 2
    root = (args.root or default_root()).resolve()
    plan = {"schema_version": STATE_VERSION, "references_root": str(root), "network": "NOT_USED" if not args.install else ("ALLOWED" if args.allow_network else "APPROVAL_REQUIRED"), "references": selected}
    if not args.install:
        print(json.dumps({"status": "CHECK_ONLY", **plan}, ensure_ascii=False, indent=2))
        return 0
    if not args.allow_network:
        print("APPROVAL_REQUIRED_NETWORK: review the seven-reference plan, then rerun with --allow-network", file=sys.stderr)
        return 2
    if not args.all and not args.ids:
        print("APPROVAL_REQUIRED_SELECTION: use --all or --id", file=sys.stderr)
        return 2
    if not shutil.which("git"):
        print("BLOCKED_GIT_REQUIRED", file=sys.stderr)
        return 2
    state_root = (args.state_root or (ROOT / ".system-agentic-local")).resolve()
    state_path = state_root / "references-installation.json"
    state: dict[str, Any] = {"schema_version": STATE_VERSION, "references_root": str(root), "items": {}}
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("schema_version") == STATE_VERSION and isinstance(loaded.get("items"), dict): state = loaded
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    failures = 0
    for item in selected:
        target = root / item["id"]
        if target.exists():
            if not (target / ".git").is_dir():
                result = make_receipt(item, target, "BLOCKED_NON_GIT_PATH", message="existing path preserved")
            else:
                dirty = run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=target).stdout.strip()
                if dirty:
                    result = make_receipt(item, target, "DIRTY_PRESERVED", message="existing dirty checkout preserved")
                else:
                    head = run(["git", "rev-parse", "HEAD"], cwd=target).stdout.strip()
                    result = make_receipt(item, target, "READY_EXISTING" if head == item["commit"] else "PIN_MISMATCH_PRESERVED", head=head, message="clean pinned checkout preserved" if head == item["commit"] else "no reset or overwrite")
            state["items"][item["id"]] = result; results.append(result)
            if result["status"] != "READY_EXISTING": failures += 1
            atomic_json(state_path, state)
            continue
        clone = run(["git", "clone", "--branch", item["branch"], "--no-single-branch", item["url"], str(target)])
        if clone.returncode != 0:
            result = make_receipt(item, target, "UNKNOWN_CLONE", message="clone failed or timed out; reconcile target before retry")
            failures += 1
        else:
            fetched = run(["git", "fetch", "--depth", "1", "origin", item["commit"]], cwd=target)
            checked = run(["git", "checkout", "--detach", item["commit"]], cwd=target) if fetched.returncode == 0 else fetched
            head = run(["git", "rev-parse", "HEAD"], cwd=target).stdout.strip() if checked.returncode == 0 else None
            if checked.returncode != 0 or head != item["commit"]:
                result = make_receipt(item, target, "UNKNOWN_PIN", head=head, message="pin verification failed; target preserved")
                failures += 1
            else:
                status = "READY_PINNED" if item["license"] not in {"UNKNOWN", "NOASSERTION_REVIEW"} else "PINNED_LICENSE_REVIEW"
                result = make_receipt(item, target, status, head=head, message="source cloned and pin verified; dependencies not executed")
        state["items"][item["id"]] = result; results.append(result); atomic_json(state_path, state)
    summary = {"status": "PASS" if failures == 0 else "PARTIAL", "selected": len(selected), "results": results, "state_file": str(state_path), "references_root": str(root), "source_cloned": sum(bool(r["source_cloned"]) for r in results), "pin_verified": sum(bool(r["pin_verified"]) for r in results), "capability_registered": sum(bool(r["capability_registered"]) for r in results), "execution_verified": sum(bool(r["execution_verified"]) for r in results)}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for r in results: print(f"{r['status']} {r['id']} {r.get('head') or ''}".rstrip())
        for key in ("source_cloned", "pin_verified", "capability_registered", "execution_verified"): print(f"REFERENCES_{key.upper()}={summary[key]}/{len(selected)}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
