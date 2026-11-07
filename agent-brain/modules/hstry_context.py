"""Bounded hstry provenance retrieval for orchestration planning."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

DEFAULT_BIN = os.environ.get("HSTRY_BIN") or shutil.which("hstry") or "hstry"
DEFAULT_CONFIG = "~/.config/hstry/config.toml"
MAX_CHARS = 6000


def _binary() -> str | None:
    value = os.environ.get("HSTRY_BIN")
    if value:
        return value
    if Path(DEFAULT_BIN).is_file():
        return DEFAULT_BIN
    return shutil.which("hstry")


def _prov(hit: dict[str, Any]) -> dict[str, Any]:
    value = hit.get("provenance")
    return value if isinstance(value, dict) else {}


def _metadata(hits: list[dict[str, Any]], result: dict[str, Any], key: str) -> Any:
    if result.get(key) is not None:
        return result[key]
    return next((_prov(hit).get(key) for hit in hits if _prov(hit).get(key) is not None), None)


def capture_hstry_context(query: str, max_chars: int = 2400, mode: str = "auto", workspace: str | None = None) -> dict[str, Any]:
    """Run bounded local hstry search; return only allowlisted provenance."""
    try:
        limit = max(300, min(int(max_chars), MAX_CHARS))
    except (TypeError, ValueError):
        limit = 2400
    base = {"query": query, "mode": mode, "scope": "local"}
    binary = _binary()
    if not binary:
        return {"status": "UNAVAILABLE", "error_code": "HSTRY_BINARY_NOT_FOUND", **base, "hits": []}
    # hstry 0.5.x accepts --workspace only as a stored-source filter.  The
    # orchestration workspace is provenance metadata, not a reason to hide
    # global ChatGPT history; omitting the filter keeps live retrieval intact.
    argv = [binary, "--config", os.path.expanduser(os.environ.get("HSTRY_CONFIG", DEFAULT_CONFIG)), "search", query,
            "--json", "--mode", mode, "--max-chars", str(limit), "--snippet-chars", "300", "--scope", "local", "--no-tools"]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=12, check=False)
    except FileNotFoundError:
        return {"status": "UNAVAILABLE", "error_code": "HSTRY_BINARY_NOT_FOUND", **base, "hits": []}
    except subprocess.TimeoutExpired:
        return {"status": "UNAVAILABLE", "error_code": "HSTRY_TIMEOUT", **base, "hits": []}
    except OSError:
        return {"status": "UNAVAILABLE", "error_code": "HSTRY_OS_ERROR", **base, "hits": []}
    if completed.returncode != 0:
        return {"status": "ERROR", "error_code": f"HSTRY_CLI_EXIT_{completed.returncode}", **base, "hits": []}
    try:
        envelope = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return {"status": "ERROR", "error_code": "HSTRY_JSON_PARSE_FAILURE", **base, "hits": []}
    result = envelope.get("result") if isinstance(envelope, dict) else None
    if not isinstance(result, dict):
        return {"status": "ERROR", "error_code": "HSTRY_INVALID_ENVELOPE", **base, "hits": []}
    raw_hits = result.get("hits", [])
    hits = [hit for hit in raw_hits if isinstance(hit, dict)] if isinstance(raw_hits, list) else []
    bounded = []
    for hit in hits[:8]:
        snippet = hit.get("snippet") if isinstance(hit.get("snippet"), str) else ""
        provenance = _prov(hit)
        bounded.append({
            "source": provenance.get("source") or hit.get("source_adapter") or hit.get("source"),
            "source_id": hit.get("source_id") or provenance.get("source_id")
            or hit.get("source_adapter") or provenance.get("source"),
            "conversation_id": hit.get("conversation_id"), "external_id": hit.get("external_id"),
            "message_idx": hit.get("message_idx"), "message_id": hit.get("message_id"),
            "timestamp": hit.get("timestamp", hit.get("created_at")), "title": hit.get("title"),
            "snippet": snippet[:320],
        })
    return {"status": "SUCCESS", "query": query, "mode": mode, "scope": result.get("scope", "local"),
            "last_sync_at": _metadata(hits, result, "last_sync_at"),
            "completeness": _metadata(hits, result, "completeness"), "hits": bounded}
