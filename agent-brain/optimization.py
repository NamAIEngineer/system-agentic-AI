"""Shared, fail-open output optimization through verified local Caveman tools."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable


DEFAULT_MIN_INPUT_BYTES = 8_192
MIN_SAVINGS_BYTES = 256
MAX_SHRINK_INPUT_BYTES = 32 * 1024 * 1024
IMPORTANT_LINE = re.compile(
    r"^(?:GSTACK_REVIEW_RESULT=|KNOWLEDGE_BUILDER_RESULT=|GATE_SUMMARY=)|"
    r"\b(error|failed?|critical|fatal|security|finding|vulnerabilit(?:y|ies)|"
    r"invalid|reject(?:ed)?|assertionerror|exit[_ -]?code|validation|warn(?:ing)?|"
    r"files|anchors|failures|exempt blocks)\b",
    re.IGNORECASE,
)
PATH_WITH_LINE = re.compile(r"(?<![\w-])(?:[A-Za-z]:)?(?:[\w.-]+/)+[\w.-]+:\d+(?::\d+)?|(?<![\w-])(?:[A-Za-z]:)?[\w./-]+\.[A-Za-z0-9_-]+:\d+(?::\d+)?")
TRACEBACK = re.compile(r"Traceback \(most recent call last\):|^diff --git |^@@ ", re.MULTILINE)
SENSITIVE_OUTPUT = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client_secret)\s*[:=]\s*\S+|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}|\bAKIA[0-9A-Z]{16}\b",
    re.IGNORECASE,
)


def _minimum_bytes(value: int | None = None) -> int:
    if value is not None:
        return max(0, value)
    raw = os.environ.get("SYSTEM_AGENTIC_CAVEMAN_MIN_BYTES", str(DEFAULT_MIN_INPUT_BYTES))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MIN_INPUT_BYTES


def _caveman_command() -> list[str] | None:
    executable = shutil.which("caveman")
    if executable is None:
        candidate = Path.home() / ".local" / "bin" / "caveman"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            executable = str(candidate)
    return [executable, "tools", "shrink"] if executable else None


def _protected_lines(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if IMPORTANT_LINE.search(line) or PATH_WITH_LINE.search(line):
            if line not in seen:
                result.append(line)
                seen.add(line)
    return result


def _is_structured_or_sensitive(text: str, content_type: str) -> bool:
    if content_type not in {"terminal", "text"} or TRACEBACK.search(text) or SENSITIVE_OUTPUT.search(text):
        return True
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            return True
    return False


def _trace(*, used: bool, capability: str, input_bytes: int, output_bytes: int, latency_ms: float,
           fallback: bool = False, reason: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    trace: dict[str, Any] = {
        "optimization_used": used,
        "capability": capability,
        "input_size_bytes": input_bytes,
        "output_size_bytes": output_bytes,
        "reduction_ratio": round(max(0.0, 1 - output_bytes / input_bytes), 4) if input_bytes else 0.0,
        "latency_ms": round(latency_ms, 2),
        "fallback_used": fallback,
    }
    if reason:
        trace["reason"] = reason
    if details:
        trace.update(details)
    return trace


def optimize_text(
    text: str,
    *,
    content_type: str = "terminal",
    min_bytes: int | None = None,
    shrinker: Callable[[str], tuple[str, dict[str, Any]]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Compact large terminal output only when CCR recovery and fidelity checks pass."""
    original_bytes = len(text.encode("utf-8"))
    capability = "agent.output-shrink"
    threshold = _minimum_bytes(min_bytes)
    if original_bytes < threshold:
        return text, _trace(used=False, capability=capability, input_bytes=original_bytes,
                            output_bytes=original_bytes, latency_ms=0, reason="below_size_threshold")
    if original_bytes > MAX_SHRINK_INPUT_BYTES:
        return text, _trace(used=False, capability=capability, input_bytes=original_bytes,
                            output_bytes=original_bytes, latency_ms=0, reason="input_over_limit")
    if _is_structured_or_sensitive(text, content_type):
        return text, _trace(used=False, capability=capability, input_bytes=original_bytes,
                            output_bytes=original_bytes, latency_ms=0, reason="fidelity_policy_bypass")

    started = time.monotonic()
    recovery_verified = False
    verified_handle: str | None = None
    try:
        if shrinker is None:
            command = _caveman_command()
            if command is None:
                raise FileNotFoundError("local caveman CLI is unavailable")
            completed = subprocess.run(command, input=text, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=20, check=False)
            if completed.returncode != 0:
                raise RuntimeError(f"caveman shrink exited {completed.returncode}")
            compact = completed.stdout
            metadata = _metadata_from_stderr(completed.stderr)
            if metadata is None:
                raise RuntimeError("caveman shrink omitted machine-readable recovery metadata")
            shrinker_metadata = metadata
            if not isinstance(metadata.get("recovery_handle"), str):
                raise RuntimeError("caveman shrink output has no recoverable handle")
            try:
                recovered_text = retrieve_recovered_text(metadata["recovery_handle"])
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
                recovered_text = None
            if recovered_text != text:
                raise RuntimeError("caveman recovery did not match the exact original output")
            recovery_verified = True
            verified_handle = metadata["recovery_handle"]
        else:
            compact, shrinker_metadata = shrinker(text)
        if not compact.strip():
            raise RuntimeError("caveman returned empty output for non-empty input")
        protected = _protected_lines(text)
        missing = [line for line in protected if line not in compact]
        if missing:
            compact = compact.rstrip() + "\n\n[Preserved verification evidence]\n" + "\n".join(missing) + "\n"
        compact_bytes = len(compact.encode("utf-8"))
        if compact_bytes + MIN_SAVINGS_BYTES > original_bytes:
            raise RuntimeError("no material reduction after preserving verification evidence")
        details: dict[str, Any] = {"recovery_verified": recovery_verified}
        if verified_handle is not None:
            details["recovery_handle"] = verified_handle
        if shrinker_metadata.get("basis"):
            details["caveman_measurement_basis"] = shrinker_metadata["basis"]
        for field in ("tokens_before", "tokens_after", "token_count_basis", "method"):
            if field in shrinker_metadata:
                details[field] = shrinker_metadata[field]
        return compact, _trace(used=True, capability=capability, input_bytes=original_bytes,
                               output_bytes=compact_bytes, latency_ms=(time.monotonic() - started) * 1000,
                               details=details)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        return text, _trace(used=False, capability=capability, input_bytes=original_bytes,
                            output_bytes=original_bytes, latency_ms=(time.monotonic() - started) * 1000,
                            fallback=True, reason=f"optimizer_unavailable_or_unsafe:{type(exc).__name__}")


def _metadata_from_stderr(stderr: str) -> dict[str, Any] | None:
    for line in reversed(stderr.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "tokens_before" in payload and "tokens_after" in payload:
            return payload
    return None


def retrieve_recovered_text(handle: str) -> str:
    """Retrieve a Caveman CCR artifact; callers must verify it against their digest."""
    if not isinstance(handle, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", handle):
        raise ValueError("invalid Caveman recovery handle")
    command = _caveman_command()
    if command is None:
        raise FileNotFoundError("local caveman CLI is unavailable")
    recovered = subprocess.run(
        [*command[:1], "tools", "retrieve", handle], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=20, check=False,
    )
    if recovered.returncode != 0:
        raise RuntimeError(f"caveman recovery exited {recovered.returncode}")
    return recovered.stdout


def optimize_provider_summary(result: Any, *, content_type: str) -> Any:
    """Optimize a provider result summary after domain-specific validation is complete."""
    summary = getattr(result, "summary", None)
    if not isinstance(summary, str) or not summary:
        return result
    compact, trace = optimize_text(summary, content_type=content_type)
    context = dict(getattr(result, "execution_context", {}) or {})
    context["optimization"] = trace
    return replace(result, summary=compact, execution_context=context)


def optimization_inventory() -> list[dict[str, Any]]:
    command = _caveman_command()
    companion = Path.home() / ".caveman" / "bin" / "caveman-engine"
    available = command is not None and companion.is_file() and os.access(companion, os.X_OK)
    registry_path = Path(__file__).with_name("modules") / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    layers = registry.get("optimization_layers", [])
    if not isinstance(layers, list):
        raise ValueError("optimization_layers must be a list")
    return [{
        **layer,
        "available": bool(layer.get("enabled")) and available,
        "runtime_state": "active" if available else "inactive",
        "state_evidence": [f"cli={command[0] if command else 'missing'}", f"companion={companion}"],
    } for layer in layers]
