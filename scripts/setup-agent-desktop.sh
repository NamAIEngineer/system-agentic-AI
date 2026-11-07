#!/usr/bin/env bash
set -euo pipefail

version="1.2.0"
package="agent-desktop@${version}"

usage() {
  cat <<'EOF'
Usage: scripts/setup-agent-desktop.sh [--check|--install|--uninstall]

  --check      Read-only executable/version probe (default)
  --install    Install the pinned agent-desktop package for the current user
  --uninstall  Remove agent-desktop from the user npm prefix when present
EOF
}

mode="--check"
if [ "$#" -gt 1 ]; then usage >&2; exit 2; fi
if [ "$#" -eq 1 ]; then mode="$1"; fi

case "$mode" in
  --check)
    executable="$(command -v agent-desktop || true)"
    if [ -z "$executable" ] && [ -x "${HOME}/.local/bin/agent-desktop" ]; then
      executable="${HOME}/.local/bin/agent-desktop"
    fi
    if [ -n "$executable" ]; then
      "$executable" version
    else
      printf '%s\n' '{"status":"UNAVAILABLE","reason":"agent-desktop executable not found"}'
      exit 1
    fi
    ;;
  --install)
    command -v npm >/dev/null 2>&1 || { echo "npm is required" >&2; exit 1; }
    user_prefix="${HOME}/.local"
    mkdir -p "$user_prefix"
    exec npm install --prefix "$user_prefix" -g "$package" --allow-scripts=agent-desktop
    ;;
  --uninstall)
    command -v npm >/dev/null 2>&1 || { echo "npm is required" >&2; exit 1; }
    if [ -d "${HOME}/.local/lib/node_modules/agent-desktop" ]; then
      npm uninstall --prefix "${HOME}/.local" -g agent-desktop
    else
      echo "agent-desktop is not installed in the user prefix"
    fi
    ;;
  *) usage >&2; exit 2 ;;
esac
