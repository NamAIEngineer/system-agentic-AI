# System Agentic AI — shared operating contract

This file defines the provider-neutral operating rules for System Agentic AI. A host or model is an adapter that consumes this contract; it is not the system itself.

## Task lifecycle

1. Frame the user's requested outcome and constraints.
2. Define acceptance evidence before multi-step implementation.
3. Inspect the target workspace and choose capabilities from verified provider/tool availability.
4. Execute only through an explicitly selected, enabled adapter. Do not silently fall back or expose task data to an unconfigured provider.
5. Verify outcomes against the acceptance evidence, report uncertainty, and stop when satisfied.

## Host, orchestrator, and worker

- The interactive host receives the user's prompt and owns the decision to answer directly or dispatch a worker. Ordinary conversation and simple explanations stay on the host; concrete repository/filesystem execution, implementation, QA, and tasks needing an independent execution pass may be framed as a `ProviderTask`.
- A host dispatch extends the existing task request with `execution_policy` (`READ_ONLY`, `WORKSPACE_WRITE`, or `ELEVATED`) and `execution_context` (`origin`, `host_role`, `dispatch_depth`, `parent_task_id`, `direct_or_dispatch`, and `framing_result`). This is execution metadata on the same task contract, not a second schema.
- A dispatched worker has role `worker`, receives `SYSTEM_AGENTIC_EXECUTION_ROLE=worker`, and executes only the already-selected task. It must not recursively dispatch. The dispatch CLI rejects worker-originated submissions and only accepts the current single hop from host to worker (`dispatch_depth=1`).
- `READ_ONLY` and `WORKSPACE_WRITE` are enforced by the selected adapter where supported. `ELEVATED` means machine-wide, unrestricted, external-write, or other permissions beyond the declared sandbox. If the adapter/runtime cannot enforce it or collect approval, fail closed before enqueue; model classification is never approval.
- An `UNKNOWN` submit/result stays associated with its original task ID. Reconcile that ID; never automatically submit again.

## Provider boundary

- Provider identity, account identity, host environment, model, role policy, capabilities, and authentication state are separate facts.
- Read `providers/registry.json` for policy and use live adapter discovery for installed, authenticated, available, and healthy state. Registry declarations are not proof of runtime availability or model quality.
- A provider may be considered for a role only when it is enabled, available, implements the requested capability, and is allowed that role. Candidate listing does not dispatch or authorize an external side effect.
- No automatic quota failover or cross-provider task forwarding is enabled. Provider changes must preserve the user's data-flow authorization.
- Unavailable or unauthenticated providers are skipped with an explicit status; they do not make other providers unavailable.
- Accounts belong to providers. Changing an account must not change or delete this shared source tree.

## Optimization layer

- Tools such as Caveman are middleware, not providers or domain modules. Apply them only when local policy predicts a useful reduction for the specific output type and size.
- A lossy output replacement requires verified exact recovery and preservation of execution status plus relevant errors, findings, paths, line numbers, and numeric evidence. Structured artifacts, diffs, and tracebacks bypass unless their semantics can be preserved.
- Optimization failure returns the original output. Local optimization must not upload private task content or enable telemetry implicitly.

## Tool Runtime boundary

- SYSTEM_AGENTIC_AI owns provider-neutral tool execution, policy enforcement, result normalization, optimization, and recovery. Host protocols such as MCP are adapters and must not own tool semantics.
- A host may use the shared Tool Runtime for supported output-heavy operations. The runtime validates workspace paths and arguments, applies its configured execution policy, and rejects `ELEVATED` unless an approval-capable policy is explicitly implemented.
- Tool outputs are optimized only after execution. A compact result is usable only when its fidelity checks pass and the exact original can be recovered; otherwise return the original output.
- Native host tools remain available for small, interactive, or unsupported operations. The Tool Runtime is not a reason to route every tool call through a gateway.

## Host adapters

Codex is the active interactive host on this machine. Its account homes are consumers of this shared brain. The local task runtime also has a Qwen worker adapter. Claude and Gemini are catalogued for future use but are disabled and have no verified execution adapter or credentials. This host state does not change the provider-neutral rules above.

Provider-specific skill formats, account discovery, and execution belong in host/provider adapters. Shared policy and registry remain in `agent-brain/`; do not put the brain's only copy inside a provider account home.
