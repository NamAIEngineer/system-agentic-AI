# Troubleshooting

- Python too old: install Python 3.10+ and rerun `./scripts/doctor.sh`.
- Provider unavailable: run `./scripts/system-agentic providers`; check binary, login, loopback endpoint, and registry flag separately.
- Queue remains pending: inspect the user's private worker service and runtime state; do not create a second worker or resubmit an uncertain ID.
- Codex rejected: authenticate Codex and select `--backend codex` explicitly. Codex login does not authenticate Claude or Gemini.
- Browser unavailable: install/configure a disposable browser runtime. Never point the candidate at a personal profile or copy cookies.
- ORCA/9router mismatch: check the host's own loopback URL and Codex home. Keep router config and authorization outside Git.
- hstry unavailable: omit it or set `HSTRY_BIN`; static checks do not require hstry.
- Personal path error: set `SYSTEM_AGENTIC_WORKSPACE_ROOT`; do not add machine-specific absolute paths to source.
