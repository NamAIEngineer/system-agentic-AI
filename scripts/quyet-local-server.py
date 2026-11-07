#!/usr/bin/env python3
"""Offline loopback inference service for the locally cached Quyet-Tiny model."""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from classifier_contract import DECISION_QUESTIONS, QUESTIONS, decision_questions


MODEL_REVISION = "fba8ca9292a705e21b14a4c5150e79a4ddcc8db3"
_default_hf_home = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))).expanduser()


def _model_path() -> Path:
    configured = os.environ.get("SYSTEM_AGENTIC_QUYET_MODEL_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    cache_root = os.environ.get("SYSTEM_AGENTIC_QUYET_MODEL_CACHE", "").strip()
    if not cache_root:
        state_root = os.environ.get("SYSTEM_AGENTIC_QUYET_ROOT", "").strip()
        cache_root = str(Path(state_root).expanduser() / "model-cache") if state_root else str(_default_hf_home)
    root = Path(cache_root).expanduser()
    if (root / "quyet_config.json").is_file():
        return root.resolve()
    return (root / "hub" / "models--chinhnc--Quyet-1.0-Tiny" / "snapshots" / MODEL_REVISION).resolve()


MODEL_PATH = _model_path()
_configured_url = os.environ.get("SYSTEM_AGENTIC_QUYET_URL", "http://127.0.0.1:8766")
_parsed_url = urlparse(_configured_url)
HOST = os.environ.get("SYSTEM_AGENTIC_QUYET_HOST", _parsed_url.hostname or "127.0.0.1")
PORT = int(os.environ.get("SYSTEM_AGENTIC_QUYET_PORT", str(_parsed_url.port or 8766)))
if HOST not in {"127.0.0.1", "localhost", "::1"}:
    raise RuntimeError("Quyet service must bind to loopback")
MAX_REQUEST_BYTES = 64 * 1024


def load_model():
    import torch
    import quyet

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    if not MODEL_PATH.is_dir():
        raise RuntimeError(f"Quyet-Tiny snapshot is missing: {MODEL_PATH}")
    try:
        model = quyet.load(str(MODEL_PATH), device=device)
    except RuntimeError:
        if device != "mps":
            raise
        device = "cpu"
        model = quyet.load(str(MODEL_PATH), device=device)
    return model, device


MODEL, DEVICE = load_model()
PREFERRED_DEVICE = DEVICE
FALLBACK_REASON = None
INFERENCE_VALID = True


def _uniform_answers(result):
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict) or not answers:
        return False
    seen = 0
    for answer in answers.values():
        probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
        values = list(probabilities.values()) if isinstance(probabilities, dict) else probabilities
        if not isinstance(values, (list, tuple)) or len(values) < 2:
            return False
        try:
            numbers = [float(value) for value in values]
        except (TypeError, ValueError):
            return False
        if any(not 0 <= value <= 1 for value in numbers) or max(numbers) - min(numbers) > 0.00011:
            return False
        seen += 1
    return seen > 0


def _predict(task, questions):
    global MODEL, DEVICE, FALLBACK_REASON, INFERENCE_VALID
    try:
        result = MODEL.predict(task, questions)
    except Exception as exc:
        if DEVICE != "mps" or not isinstance(exc, RuntimeError):
            raise
        reason = f"mps_inference_error_{type(exc).__name__}"
    else:
        if DEVICE != "mps" or not _uniform_answers(result):
            INFERENCE_VALID = True
            return result
        reason = "mps_degenerate_uniform_probabilities"

    # Do not return uniform scores as if they were model confidence.
    previous_model = MODEL
    try:
        import torch
        try:
            torch.mps.synchronize()
        except Exception:
            pass
        try:
            torch.mps.empty_cache()
        except Exception:
            pass
        cpu_model = __import__("quyet").load(str(MODEL_PATH), device="cpu")
        cpu_result = cpu_model.predict(task, questions)
    except Exception:
        MODEL = previous_model
        raise
    MODEL = cpu_model
    DEVICE = "cpu"
    FALLBACK_REASON = reason
    INFERENCE_VALID = not _uniform_answers(cpu_result)
    return cpu_result


class Handler(BaseHTTPRequestHandler):
    server_version = "SystemAgenticQuyet/1.0"

    def log_message(self, fmt, *args):
        print("quyet-local", self.address_string(), fmt % args, flush=True)

    def _json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != "/health":
            self._json(404, {"error": "not found"})
            return
        self._json(200, {
            "status": "degraded" if FALLBACK_REASON else "ready",
            "model": MODEL.name,
            "device": DEVICE,
            "preferred_device": PREFERRED_DEVICE,
            "fallback_reason": FALLBACK_REASON,
            "inference_valid": INFERENCE_VALID,
            "local": True,
        })

    def do_POST(self):
        if self.path not in {"/predict", "/decision"}:
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            self._json(400, {"error": "invalid content length"})
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._json(413, {"error": "request must be at most 65536 bytes"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "content type must be application/json"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "invalid JSON"})
            return
        task = payload.get("task") if isinstance(payload, dict) else None
        if not isinstance(task, str) or not task.strip():
            self._json(400, {"error": "task must be a non-empty string"})
            return
        try:
            if self.path == "/decision":
                options = payload.get("module_options")
                class_options = payload.get("execution_class_options")
                questions = DECISION_QUESTIONS if options is None and class_options is None else decision_questions(options, class_options)
            else:
                questions = QUESTIONS
            result = _predict(task, questions)
        except Exception as exc:
            self._json(500, {"error": f"local inference failed: {type(exc).__name__}: {exc}"})
            return
        self._json(200, {
            "advisory": True,
            **result,
            "inference": {
                "device": DEVICE,
                "preferred_device": PREFERRED_DEVICE,
                "fallback_reason": FALLBACK_REASON,
                "valid": INFERENCE_VALID,
            },
        })


if __name__ == "__main__":
    print(f"Quyet local ready: {MODEL.name} on {DEVICE} at http://{HOST}:{PORT}", flush=True)
    HTTPServer((HOST, PORT), Handler).serve_forever()
