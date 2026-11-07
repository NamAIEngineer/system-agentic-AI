# Host support

The framework is provider-neutral. Instruction support and execution support are
separate claims. Run `python3 scripts/doctor.py --json` and
`python3 scripts/system-agentic providers` on the target host.

| Host/integration | Instructions | Execution adapter | Auth/state |
|---|---|---|---|
| ORCA + Codex | `AGENTS.md`, Markdown skills | `codex-runtime-queue` verified on owner macOS only | user-owned Codex/ORCA login required |
| Claude Code | Markdown instructions supported | `EXECUTION_ADAPTER_NOT_INCLUDED` | separate Anthropic auth required |
| Gemini CLI | Markdown instructions may be consumed | `EXECUTION_ADAPTER_NOT_INCLUDED` | separate Google auth required |
| Local Qwen | runtime docs supported | optional local queue adapter | model/runtime supplied by user |
| ChatGPT web bridge | documentation/adapter boundaries | optional approval-gated bridge | user browser session; never bundled |
| REA | optional provider contract | local read-only MCP when checkout ready | external checkout required |
| agent-desktop | adapter/docs supported | optional native desktop MCP adapter | executable installed separately; host/display required |
| 9router | optional transport documentation | not a core dependency | user-configured external gateway |

## Standard first run

```sh
python3 scripts/setup.py --check-only
./scripts/setup.sh
./scripts/doctor.sh
```

Windows:

```powershell
py -3 scripts/setup.py --check-only
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\doctor.ps1
```

Opening the folder in ORCA does not imply that ORCA automatically reads
`AGENTS.md`; ask the host to initialize the workspace and read the canonical
files. No host inherits credentials from another host.
