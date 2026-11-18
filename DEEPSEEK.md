# DeepSeek — System Agentic AI workspace guide

This is an explicit project entrypoint for a host using a DeepSeek model. Do not
assume a universal `DEEPSEEK.md` auto-loader: file discovery is controlled by the
host application, not by the model API.

## Load the shared brain

Read these local files in order before project work:

1. `AGENTS.md` — canonical repository/workspace rules.
2. `agent-brain/operating-contract.md` — provider-neutral permissions and lifecycle.
3. `README.md` and `WORKFLOW.md` — this checkout's entrypoints and workflow.
4. `agent-brain/skills/system-agentic-ai/SKILL.md` — shared operating guidance.
5. `agent-brain/providers/registry.json` — configured provider and role policy.

Do not copy those policies into a separate DeepSeek brain. Use only documented
commands and files that actually exist in this checkout.

## Host and data-flow boundary

- Identify the host (for example ORCA or Claude Code), its tools, and the actual
  selected model/backend. DeepSeek is not itself proof of filesystem, shell,
  browser, queue, MCP or Git execution support.
- With Claude Code as host, `CLAUDE.md` imports the canonical shared rules. This
  guide adds backend context; it does not replace the host's permission checks.
- Use a DeepSeek backend only when explicitly configured and authorized for the
  task's data. If it is unconfigured, report `NOT_CONFIGURED`. If this framework
  has no execution adapter, report `EXECUTION_ADAPTER_NOT_INCLUDED`.
- Do not enable a provider, change API endpoints, copy another account's keys,
  send a test API request or install a model just because this guide exists.
- Never send workspace content, prompts, secrets or page data to an unconfigured
  external API. Keep credentials out of repo files, logs and shared memory.
- Live discovery and model classification are advisory, never write authority.
  Follow READ_ONLY/WORKSPACE_WRITE, workspace scope, human approvals and verified
  results. A worker must not dispatch another worker. Never replay UNKNOWN work.

## Start in a host that can read this workspace

> Read DEEPSEEK.md and AGENTS.md, then the shared files they reference. Inspect
> this workspace read-only and report the actual host/model, available tools,
> configured providers and missing prerequisites. Do not install, download,
> change provider settings, call an unconfigured API or submit a task yet.

If the application is a chat-only website without local tools, report
`INSTRUCTIONS_SUPPORTED` and `EXECUTION_ADAPTER_NOT_INCLUDED`; do not claim that
it has opened, edited, tested or executed files on the user's machine.
