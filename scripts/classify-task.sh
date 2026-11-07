#!/usr/bin/env bash
set -euo pipefail

brain_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${PYTHON_BIN:-python3}"
exec "$python" "$brain_root/scripts/classify-task.py" "$@"
