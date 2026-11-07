# Provider adapter contract

`registry.json` stores policy and adapter identity, never credentials. The Python standard-library implementation is `runtime.py`.

Every executable adapter exposes these operations:

- `detect()` reports installed, authentication state, availability, and evidence without returning credentials.
- `health()` reports the adapter's supported health signal and its limits.
- `capabilities()` returns adapter capabilities that have an implementation; registry roles remain policy.
- `execute(task)` performs an explicit task request through that provider's existing execution path and observes that same queue job to a terminal state or bounded `UNKNOWN`.
- `status(task_id)` reconciles a known task without enqueueing it again.
- `normalize(result)` maps queue state to the provider-neutral `SUBMITTED`, `RUNNING`, `COMPLETED`, `FAILED`, or `UNKNOWN` result and withholds raw process output.

The shared task request also carries an execution policy (`READ_ONLY`, `WORKSPACE_WRITE`, or `ELEVATED`) and execution context (`origin`, `host_role`, `dispatch_depth`, `parent_task_id`, `direct_or_dispatch`, and `framing_result`). The host decides DIRECT vs DISPATCH; only DISPATCH tasks enter provider selection. A dispatched worker runs with a structured worker-role marker and cannot recursively call the dispatch entrypoint. Current single-hop policy accepts host-to-worker dispatch at depth 1.

`ProviderStatus` keeps `installed`, `authenticated`, `available`, `healthy`, `execution_runtime_ready`, and policy `enabled` separate. For Codex, health checks login status against the Codex home configured for the running queue worker and confirms the worker LaunchAgent is loaded; this still does not prove quota or model inference. For a local model, authentication is `not_required`, model health checks its loopback endpoint, and availability also requires the task queue worker. `authenticated` may be `null` when an adapter cannot safely determine it.

The Codex adapter queues work through the existing `scripts/agentic-runtime submit --backend codex` path, with a caller-assigned task ID persisted as the queue job ID. `READ_ONLY` maps to Codex CLI's `read-only` sandbox and `WORKSPACE_WRITE` maps to `workspace-write`; `ELEVATED` is refused before enqueue. Legacy queue jobs created without a verified sandbox are not relabeled safe and cannot be resumed as Codex sessions. It does not invoke a second execution loop or silently select another account/provider. The local Qwen adapter continues to use the same queue with `--backend local`. A timeout or unparseable queue receipt returns `UNKNOWN` with the known ID when possible; callers may use `system-agentic status <task_id>` to reconcile and must not resubmit automatically. Completed Codex summaries come from the queue's final agent-message event; raw logs remain internal. Claude and Gemini have catalog metadata only; their execution/status operations raise `ProviderUnavailable` and never fabricate a result.

The REA adapter is a local specialist provider, not an orchestrator. It starts one pinned REA stdio MCP process per `READ_ONLY` inspection, performs `initialize` and `tools/list`, then calls one allowlisted local-artifact tool. It requires an explicit `context.target_authorized: true`, refuses `WORKSPACE_WRITE`/`ELEVATED`, bounds request/response bytes and deadlines, redacts transport credentials, preserves REA Evidence provenance, and terminates its owned process. A failed process returns a normalized provider error; the host can start a fresh session without crashing or silently retrying the analysis.

`eligible_providers(role, required_capabilities)` remains a candidate query. `select_provider(task)` chooses the eligible provider with the lowest registry `fallback_priority`, further restricted by an optional `provider_policy.allowed_providers` list. Selection is single-shot: execution failure does not dispatch to another provider. `scripts/system-agentic dispatch <task-spec.json>` is the explicit execution entrypoint; `candidates` remains read-only and does not dispatch.

Codex host discovery combines the active `CODEX_HOME`, `~/.codex`, and valid Orca-managed account homes. Orca account IDs are read from the account directory and `.orca-managed-home` marker at runtime; no ID is stored in code or registry. Provider home sync links the three configured skills to their source directories and updates only the marked `SYSTEM_AGENTIC_AI` block in `AGENTS.md`.
