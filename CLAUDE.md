# Claude Code — System Agentic AI

@AGENTS.md
@agent-brain/operating-contract.md

These imports keep shared policy canonical; do not maintain a second brain here.
Before project work, read `README.md`, `WORKFLOW.md`,
`agent-brain/skills/system-agentic-ai/SKILL.md`, and
`agent-brain/providers/registry.json`. Use only commands present in this checkout.

## Host and provider boundary

- Identify the actual host, model backend, available tools and authentication.
  Claude Code's own tools and a framework provider adapter are separate facts.
- Check live availability before selecting an adapter. Never assume a Codex/ORCA
  account, installed skill, queue adapter or credential also works for Claude.
- Instructions loading is not proof of execution. Until a Claude framework
  adapter and its auth/data-flow policy are configured, report
  `EXECUTION_ADAPTER_NOT_INCLUDED` for framework-managed Claude execution.
- Keep provider accounts and credentials outside the shared brain. Never change
  endpoints, models, keys or fallback providers just because this file exists.
- Follow the shared permission, workspace, verification and UNKNOWN reconciliation
  rules. A worker executes its assignment directly and never dispatches a child.
- If the selected backend is DeepSeek, also read `DEEPSEEK.md`; model routing
  remains explicit and cannot inherit permission to send private workspace data.

## Start and verify

Suggested prompt:

> Read CLAUDE.md and the shared instructions. Inspect this workspace read-only,
> report available tools/providers and missing prerequisites, then propose the
> next step. Do not install, download, change provider settings or submit a task
> during this initial inspection.

In Claude Code, use `/memory` to check the loaded instruction files. Missing
imports, tools or credentials must be reported rather than assumed available.
