# Host support

The framework is provider-neutral. Instruction support and execution support are
separate claims. Run `python3 scripts/doctor.py --json` and
`python3 scripts/system-agentic providers` on the target host.

| Host/integration | Instructions | Execution adapter | Auth/state |
|---|---|---|---|
| ORCA + Codex | `AGENTS.md`, Markdown skills | `codex-runtime-queue` verified on owner macOS only | user-owned Codex/ORCA login required |
| Claude Code | `CLAUDE.md` imports canonical `AGENTS.md` / operating contract | `EXECUTION_ADAPTER_NOT_INCLUDED` | separate Anthropic auth required |
| DeepSeek-backed host | Explicit `DEEPSEEK.md`; Claude Code host also reads `CLAUDE.md` | `EXECUTION_ADAPTER_NOT_INCLUDED` | backend/data flow must be configured explicitly |
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

## Loading Claude and DeepSeek instructions

`CLAUDE.md` uses `@AGENTS.md` and `@agent-brain/operating-contract.md` imports.
Claude Code supports these local imports; check `/memory` in the actual client
to confirm loaded files. A successful Markdown check is not a live Claude run.

`DEEPSEEK.md` is a repository-defined manual guide, not a claimed universal
DeepSeek context-file standard. Host applications choose what instruction files
they load. When using DeepSeek through Claude Code, retain the Claude Code
entrypoint and explicitly read the DeepSeek backend guide. Configuring that
backend can send task data externally; no endpoint, key or model is changed by
adding these documents. Native Claude/DeepSeek account execution remains
unverified until configured and exercised separately.

Startup prompts are in the two root files. For a chat-only client without local
tools, report instruction compatibility separately from filesystem/command
execution. Never claim local changes based on a text-only answer.

Primary references checked 2026-11-18:

- [Claude Code memory and imports](https://code.claude.com/docs/en/memory)
- [DeepSeek's Claude Code integration](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/)

These sources document host/backend behavior, not authorization to enable or
switch providers for this workspace.
