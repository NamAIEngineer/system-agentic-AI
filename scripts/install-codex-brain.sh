#!/usr/bin/env bash
set -euo pipefail

brain_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$brain_root/scripts/system-agentic" sync-provider codex
