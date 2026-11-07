#!/usr/bin/env python3
"""Offline System 1 hint for a Codex task. Output is advisory, never authorization."""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

from classifier_contract import QUESTIONS

os.environ["HF_HUB_OFFLINE"] = "1"


def main() -> int:
    task = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not task:
        print("Usage: classify-task.py <task text> (or provide the task on stdin)", file=sys.stderr)
        return 2

    backend = os.environ.get("SYSTEM_AGENTIC_CLASSIFIER", "laya").strip().lower()
    if backend == "quyet":
        request = urllib.request.Request(
            "http://127.0.0.1:8766/predict",
            data=json.dumps({"task": task}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(
                "Quyet local service is unavailable. Start it with "
                "scripts/quyet-local-service.py install.",
                file=sys.stderr,
            )
            print(str(exc), file=sys.stderr)
            return 3
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if backend == "quyet-large":
        runtime = os.path.expanduser("~/.local/share/system-agentic-ai/quyet-mlx/.venv/bin/python")
        script = os.path.join(os.path.dirname(__file__), "quyet-mlx-large.py")
        try:
            result = subprocess.run(
                [runtime, script],
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
                input=task,
                env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
            )
        except subprocess.CalledProcessError as exc:
            print(exc.stderr or str(exc), file=sys.stderr)
            return exc.returncode or 1
        except subprocess.TimeoutExpired:
            print("Quyet-Large local inference exceeded 10 minutes", file=sys.stderr)
            return 4
        print(result.stdout.strip())
        return 0
    if backend != "laya":
        print("SYSTEM_AGENTIC_CLASSIFIER must be 'laya', 'quyet', or 'quyet-large'", file=sys.stderr)
        return 2

    try:
        import laya_mlx as laya
    except ImportError:
        print("Laya local runtime is not installed; set SYSTEM_AGENTIC_CLASSIFIER=quyet or install the optional reference.", file=sys.stderr)
        return 3
    model_id = os.environ.get("SYSTEM_AGENTIC_LAYA_MODEL", "aac6fef/laya-multilingual-mlx")
    model = laya.load(model_id)
    result = model.predict(task, QUESTIONS)
    print(json.dumps({"model": model_id, "advisory": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
