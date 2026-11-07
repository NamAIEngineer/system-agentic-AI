# Workbench architecture

## Provider-neutral shared core

The shared source of truth is `agent-brain/`: provider-neutral operating policy, capability knowledge, provider registry, and adapter contract. Provider account homes only consume/link to this source. The current interactive host is Codex through Orca; that is one provider adapter, not the identity of `SYSTEM_AGENTIC_AI`.

The host layer provides Codex account discovery and brain sync in `agent-brain/providers/runtime.py`, invoked by `scripts/system-agentic`. The existing `products/agent-runtime/` remains the execution adapter: local Qwen is the default queued worker, and Codex/OpenAI is used only when explicitly selected. It records attempts and resumes interrupted Codex jobs only from their recorded session. The task queue has no network listener. Multi-agent delegation remains opt-in and governed by Orchestration System by Jang.

Provider roles and allowed capabilities are policy metadata. Live discovery separately reports installed, authenticated, available, healthy, and enabled state. The candidate query does not dispatch tasks, perform quota failover, or authorize provider data flow. Claude and Gemini are currently disabled and have no execution adapter or credentials configured.

## Reference repositories

The six cloned repositories contribute patterns and tools, not an automatic runtime dependency:

| Repository | Useful reference | Boundary |
|---|---|---|
| `design-os-system-one` | Laya MLX, TypeSafe JEV, structured classification | JEV sends prompts to a provider; local model needs its runtime/model |
| `orchestration-system-by-jang` | Frame, acceptance, routing, dispatch, verification, reporting | Delegation only when requested; configured runtime is Codex CLI |
| `gstack` | Product, design, engineering, QA, security, release skills | Skills run in their supported agent host; not a background worker pool |
| `design-os-pedagogy` | Lesson/rehearsal structures and learning assessment | Existing apps remain source-repo products |
| `design-os-knowledge-builder` | Knowledge ingestion, provenance and packaging | Respect source licenses and trust boundaries |
| `caveman` | Context/cost tools, CLI/MCP, agent workflows | Host-specific tools are not automatically available in every generated product |

Read each repository's `REPORT.md` before selecting a pattern; confirm live source instructions and runtime requirements before integrating it. The central install report is `REPO_INSTALL_REPORT.md`.

## Work products

- `products/<slug>/` holds an independent system/product implemented in this workspace.
- `product-kits/` supplies optional domain outlines. A scaffold is not a completed system.
- `packages/agent-core/` holds reusable TypeScript intent/risk, verifier and capability contracts where they fit.
- `plans/<task>/acceptance.md` and `report.md` hold traceable execution evidence for larger tasks.
- `TASK.md` is a simple file-based alternative to assigning a task in chat.

## Data and external actions

Do not move the dashboard's SQLite database or TypeSafe key into a product. Product data isolation, authentication, secrets, connectors, and retention must be designed for the actual task. Treat imported knowledge as untrusted, preserve source/provenance/license, and scope retrieval to the product's authorization boundary.

One-way actions (publish, deploy, message people, migrate/delete real data, incur spending) require explicit user authorization. A plan or approval inside generated code is not evidence that an external side effect occurred.

## Dormant dashboard

`apps/agentic-platform/` contains the dashboard/API built earlier when the purpose was misunderstood. It is stopped, excluded from the active pnpm workspace, and not part of the primary task flow. Keep it only as a historical local artifact; do not start or extend it unless the human later asks for a UI/control plane.
