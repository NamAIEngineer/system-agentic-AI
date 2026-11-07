# Domain module registry

`registry.json` is the canonical map from a stable module ID to its existing
workspace. Module lookup is provider-neutral; it does not invoke a provider or
make a module executable. `workspace_resolver.py` resolves the registered root
first, then accepts only relative paths contained by that root. It never
derives a sibling path from the current task workspace.

## Registered workspaces

The public registry contains only framework-owned or explicitly optional public
capabilities. Product-specific workspaces and private downstream projects are
intentionally not registered or distributed. External reference checkouts are
resolved under the user-selected references root and remain independent.

Registered workspace health is `UNKNOWN` until a module-specific health check is
implemented. Registry presence does not mean a capability has an execution
adapter.

## Task dispatch

A task may set `targetModuleId` and `requestedCapability`. Dispatch validates
the capability against the registry, resolves the registered root, stores
`targetModuleId`, `requestedCapability`, and `resolvedWorkspace` in task
context, and assigns that root as the provider task workspace. A caller-supplied
`resolvedWorkspace` must match the registry. Existing tasks without a target
module keep their supplied workspace.

Every queue task now carries `assignedWorkspace` with
`workspaceAuthority=SYSTEM_AGENTIC_QUEUE`; the worker validates it against the
persisted queue workspace before invoking tools. Module-aware work additionally
carries `moduleExecutionContext` with `workspaceAuthority=MODULE_REGISTRY`,
revalidated against the registry and queue root at both enqueue and worker
startup. Tool paths are relative to the assigned root and reject repeating
that root folder (or target module ID) as the first path component. Existing
traversal and symlink checks remain active. Legacy queued rows are promoted
from their persisted workspace column at worker start; no module is searched
for by the worker.

## Handoff and pipeline contracts

`ProductArtifact` is a serialization contract for module-produced files,
previews, metadata, and technical specifications. `PipelinePlan` validates
step identities and dependency acyclicity and marks independent steps
`RUNNABLE` while unmet dependencies remain `WAITING_FOR_DEPENDENCIES`.
These contracts do not execute modules or transfer files.

`pipeline_executor.py` executes validated plans with SQLite-backed run/step
state, atomic `(pipelineId, stepId)` claims, bounded worker threads, explicit
failure policy, structured events, artifact handoff, and an SQLite workspace
lease table. One process coordinator shares that lease manager across executor
instances even when their pipeline run stores differ; executors in separate
processes share leases when configured with the same lease database. A process
crash leaves its lease for operator inspection; automatic
lease reclamation and crash recovery are deliberately not implemented. Stale
leases require explicit inspection and safe operator action; runtime does not
auto-steal them. The executor never retries a step. `SystemAgenticDispatchAdapter` wraps the shared
task parser/provider dispatch path and defaults to a local-Qwen-only policy.

`pipeline-run` executes a plan through that adapter, and `pipeline-inspect`
reads its durable state without dispatching. Fixture adapters are used by
tests and are not exposed as production module adapters.

No marketing pipeline, Blender render, app generation, SVG generation, browser
session, or social action is started by this foundation.

## Dynamic products and capability providers

`agent-desktop` is the shared native desktop capability. Its registry entry is
configuration only; `modules.agent_desktop_adapter` owns the single MCP stdio
implementation. A project selects `desktop.automation` with `context.desktop`
and inherits executable/session/target/timeout/action-limit overrides. The
adapter probes the configured executable, negotiates the real MCP schema, and
keeps native desktop actions separate from browser/DOM capabilities. It fails
closed without a binary or explicit per-task action authorization.

`ModuleRegistry` remains the configuration boundary. `CapabilityProviderRegistry`
indexes registered providers for a requested capability and reports health,
availability, execution modes, priority, workspace, and adapter metadata. The
executor accepts an explicit module or resolves a capability-only step using a
single-provider, explicit-provider, or uniquely highest-priority policy. Empty
provider sets and ties fail closed. The resolved module and absolute workspace
are written into the plan, step record, and resolution event before dispatch.
Onboarding a module changes registry/adapter configuration; the scheduler has
no module-name branches.

`ProcessRuntimeCoordinator` is shared by default-created executors in one
process. It also keeps one SQLite workspace lease manager for all executors
using that coordinator. Its `GlobalExecutionBudget` defaults to one active step and enforces
global, per-product, and per-module limits across `run()`, `run_many()`, and
separate executor instances. Per-pipeline worker limits apply inside each
plan; SQLite workspace leases are the final workspace-scope guard and
coordinate mutations across executors in the process. Effective
concurrency is bounded by the strictest active scope. Distribution account and
platform limits remain outside this executor.

`ProductRun` is persisted independently from a pipeline and can link multiple
pipeline IDs. Product, media, and content-variant outputs are also recorded as
immutable references in the SQLite `ArtifactStore`, with producer provenance,
product identity, explicit source refs, producer workspace, and safe file
manifests. A later pipeline can consume only declared `inputRefs`; the store
rechecks provenance, artifact type, containment, and hashes. Product IDs must
match unless the consuming plan names an `authorizedProductIds` entry and an
explicit `authorize_product_relation(source, target, reason)` record exists
for the persisted ProductRuns. Legacy artifact rows without producer workspace
evidence are not imported automatically. `DistributionIntent` contains
normalized product/content/media references and audience/platform hints only;
it does not encode social accounts, quotas, authorization, or dispatch details.
`DistributionMetric` is a validated extensible feedback contract. Metrics are
not fetched and no optimizer or live distribution handoff is implemented.

`PipelineTemplate` is a reusable capability graph. Instantiating it yields a
product-bound pipeline plan, which is independently validated and run. The safe
test suite exercises registries from one through twenty entries, concurrent
fixture ProductRuns, media/content fan-out, and typed distribution handoff.
These remain fixture contracts, not production domain adapters.
