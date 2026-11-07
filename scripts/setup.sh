#!/usr/bin/env bash
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3.12 >/dev/null 2>&1; then PYTHON_BIN=python3.12
  elif command -v python3 >/dev/null 2>&1; then PYTHON_BIN=python3
  elif command -v python >/dev/null 2>&1; then PYTHON_BIN=python
  else echo "ERROR: Python 3.10+ is required" >&2; exit 1; fi
fi
if [ "$#" -eq 0 ]; then
  exec "$PYTHON_BIN" "$ROOT/scripts/setup.py" --install --skip-references
fi
exec "$PYTHON_BIN" "$ROOT/scripts/setup.py" "$@"
