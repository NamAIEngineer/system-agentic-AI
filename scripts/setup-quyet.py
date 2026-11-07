#!/usr/bin/env python3
"""Check or explicitly install the optional Quyet Tiny decision classifier.

Quyet is advisory only. This script never starts/stops a service, changes a
global interpreter, edits shell configuration, or grants execution authority.
Model download requires both ``--allow-network`` and
``--allow-model-download``. The model stays in the user cache, outside Git.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import platform
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MODEL_ID = "chinhnc/Quyet-1.0-Tiny"
MODEL_REVISION = "fba8ca9292a705e21b14a4c5150e79a4ddcc8db3"
MODEL_LICENSE = "Apache-2.0"
MODEL_CACHE = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))) / "hub" / "models--chinhnc--Quyet-1.0-Tiny" / "snapshots" / MODEL_REVISION
QUYET_URL = os.environ.get("SYSTEM_AGENTIC_QUYET_URL", "http://127.0.0.1:8766")
STATE_ROOT = Path(os.environ.get("SYSTEM_AGENTIC_QUYET_ROOT", str(Path.home() / ".system-agentic-ai" / "quyet"))).expanduser()


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def health() -> tuple[str, dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"{QUYET_URL.rstrip('/')}/health", timeout=2) as response:
            value = json.loads(response.read().decode("utf-8"))
        return ("READY" if value.get("status") in {"ready", "degraded"} and value.get("inference_valid") is True else "ERROR", value)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return "UNAVAILABLE", {"error": type(exc).__name__}


def inference() -> tuple[str, dict[str, Any]]:
    payload = json.dumps({"task": "Classify this synthetic bootstrap check.", "module_options": ["none", "orchestration"], "execution_class_options": ["READ_ONLY", "WORKSPACE_WRITE"]}).encode("utf-8")
    request = urllib.request.Request(f"{QUYET_URL.rstrip('/')}/decision", data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
        valid = value.get("advisory") is True and isinstance(value.get("answers"), dict) and isinstance(value.get("inference"), dict) and value["inference"].get("valid") is True
        return ("PASS" if valid else "ERROR", {"advisory": value.get("advisory"), "answers_present": isinstance(value.get("answers"), dict), "inference": value.get("inference"), "model": value.get("model")})
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return "UNAVAILABLE", {"error": type(exc).__name__}


def report() -> dict[str, Any]:
    torch = importlib.util.find_spec("torch")
    quyet = importlib.util.find_spec("quyet")
    model_present = MODEL_CACHE.is_dir() and (MODEL_CACHE / "LICENSE").is_file() and (MODEL_CACHE / "NOTICE").is_file() and (MODEL_CACHE / "quyet_config.json").is_file()
    health_status, health_data = health()
    return {
        "model": MODEL_ID, "revision": MODEL_REVISION, "license": MODEL_LICENSE,
        "os": platform.system(), "architecture": platform.machine(),
        "python": platform.python_version(), "mps_available": bool(torch and platform.system() == "Darwin" and platform.machine() == "arm64"),
        "dependencies": {"torch": bool(torch), "quyet": bool(quyet), "torch_version": package_version("torch"), "quyet_version": package_version("quyet")},
        "model_present": model_present, "health": health_status, "health_data": health_data,
        "status": "READY" if model_present and health_status == "READY" else ("NOT_INSTALLED" if not model_present else "UNAVAILABLE"),
        "download_approval": False,
        "execution_authority": "ADVISORY_ONLY",
    }


def install(args: argparse.Namespace) -> int:
    if not args.allow_network or not args.allow_model_download:
        print("APPROVAL_REQUIRED: use --allow-network and --allow-model-download for model/dependency download", file=sys.stderr)
        return 2
    STATE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    venv = STATE_ROOT / "venv"
    if not venv.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run([str(python), "-m", "pip", "install", "quyet==1.0.2"], check=True)
    code = "import quyet; quyet.load('chinhnc/Quyet-1.0-Tiny', revision='fba8ca9292a705e21b14a4c5150e79a4ddcc8db3', device='cpu')"
    subprocess.run([str(python), "-c", code], check=True)
    print("QUYET_INSTALL_COMPLETE")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--verify", action="store_true", help="check loopback health and run one synthetic advisory inference")
    p.add_argument("--install", action="store_true")
    p.add_argument("--allow-network", action="store_true")
    p.add_argument("--allow-model-download", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    if args.install:
        return install(args)
    value = report()
    if args.verify:
        value["inference"], value["inference_data"] = inference()
    if args.json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(f"QUYET_{value['status']}")
        print(f"QUYET_MODEL_PRESENT={'PASS' if value['model_present'] else 'FAIL'}")
        print(f"QUYET_DEPENDENCIES={'PASS' if all(value['dependencies'][x] for x in ('torch', 'quyet')) else 'NOT_INSTALLED'}")
        print(f"QUYET_HEALTH={value['health']}")
        if args.verify: print(f"QUYET_INFERENCE={value.get('inference', 'UNVERIFIED')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
