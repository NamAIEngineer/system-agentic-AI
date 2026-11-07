# Provider and integration status

Provider policy is in `agent-brain/providers/registry.json`; live readiness is
host-specific. Use `python3 scripts/system-agentic providers` and
`python3 scripts/doctor.py --json` before a task.

| Provider/integration | Default | Data boundary | Public status |
|---|---|---|---|
| Codex/OpenAI | disabled until selected per task | user-approved host/provider flow | adapter source included; auth required |
| Local Qwen | optional | local model/runtime only | adapter source included; model not bundled |
| Claude | disabled | no configured external flow | instructions compatible; execution adapter not included |
| Gemini | disabled | no configured external flow | instructions compatible; execution adapter not included |
| REA | optional | local read-only MCP checkout | external reference; unavailable without checkout |
| agent-desktop | optional | local native desktop process | adapter included; executable installed separately |
| ChatGPT bridge | optional | approval-gated user browser session | credentials/profile never bundled |
| 9router | optional external | user-configured loopback gateway | never required by core |

`AVAILABLE`, `AUTH_REQUIRED`, `NOT_CONFIGURED`, `UNAVAILABLE`, `UNSUPPORTED`,
and `UNVERIFIED` are distinct states. A classifier recommendation cannot grant
permission, enable a provider, or authorize an external write.
