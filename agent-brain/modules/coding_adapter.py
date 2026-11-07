"""Workspace-bounded coding execution adapter.

It performs explicit source replacements and declared verification commands. The
adapter never invents a patch or runs commands outside the assigned workspace.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from providers.runtime import ProviderTask


class CodingAdapterError(RuntimeError):
    pass


def coding_health() -> dict[str, Any]:
    return {"status": "READY", "runtime": "workspace-bounded-native-shell", "capability": "software.coding"}


def _path(root: Path, raw: object) -> Path:
    if not isinstance(raw, str) or not raw.strip() or Path(raw).is_absolute():
        raise CodingAdapterError("coding path must be a non-empty relative path")
    result = (root / raw).resolve(strict=False)
    if not result.is_relative_to(root):
        raise CodingAdapterError("coding path escapes assigned workspace")
    return result


def execute_coding_module(task: ProviderTask) -> dict[str, Any]:
    root = task.workspace.resolve(strict=True)
    context = task.context.get("coding") if isinstance(task.context.get("coding"), dict) else task.context
    patch = context.get("patch")
    changed = []
    if patch is not None:
        if not isinstance(patch, dict):
            raise CodingAdapterError("coding patch must be an object")
        path = _path(root, patch.get("path"))
        if not path.is_file() or path.is_symlink():
            raise CodingAdapterError("coding patch target is missing or unsafe")
        old, new = patch.get("old"), patch.get("new")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise CodingAdapterError("coding patch requires non-empty old/new strings")
        content = path.read_text(encoding="utf-8")
        if content.count(old) != 1:
            raise CodingAdapterError("coding patch requires exactly one matching source occurrence")
        path.write_text(content.replace(old, new, 1), encoding="utf-8")
        changed.append({"path": str(path.relative_to(root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    command = context.get("test_command") or context.get("verify_command")
    if not isinstance(command, str) or not command.strip():
        raise CodingAdapterError("coding task requires a declared test_command")
    argv = shlex.split(command)
    if not argv or any(part.startswith("http://") or part.startswith("https://") for part in argv):
        raise CodingAdapterError("coding command is invalid or network-shaped")
    try:
        completed = subprocess.run(argv, cwd=str(root), capture_output=True, text=True, timeout=60, check=False)
        evidence = {"command": command, "exit_code": completed.returncode,
                    "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]}
    except subprocess.TimeoutExpired as exc:
        evidence = {"command": command, "exit_code": None, "stdout": str(exc.stdout or "")[-8000:],
                    "stderr": str(exc.stderr or "")[-8000:], "timeout_seconds": 60}
        return {"execution_status": "FAILED", "domain_outcome": "CODING_TEST_FAILED",
                "changed_files": changed, "verification": evidence,
                "errors": [{"code": "TEST_TIMEOUT", "message": "declared coding test exceeded 60 seconds"}]}
    except OSError as exc:
        evidence = {"command": command, "exit_code": None, "stdout": "", "stderr": str(exc),
                    "error_type": type(exc).__name__}
        return {"execution_status": "FAILED", "domain_outcome": "CODING_TEST_FAILED",
                "changed_files": changed, "verification": evidence,
                "errors": [{"code": "COMMAND_UNAVAILABLE", "message": str(exc)}]}
    if completed.returncode != 0:
        return {"execution_status": "FAILED", "domain_outcome": "CODING_TEST_FAILED", "changed_files": changed, "verification": evidence, "errors": [{"code": "TEST_FAILED", "message": completed.stderr[-1000:]}]}
    return {"execution_status": "COMPLETED", "domain_outcome": "CODE_CHANGED_AND_VERIFIED", "changed_files": changed, "verification": evidence, "summary": "source change and declared test completed"}
