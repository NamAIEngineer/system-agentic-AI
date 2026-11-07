# System Agentic AI repository instructions

This repository is a provider-neutral AI workspace framework: agent brain,
operating contract, bounded runtime, host skills, provider discovery, and safe
bootstrap. It is not a GUI, SaaS service, dashboard, account store, or project
repository.

## First-run workflow

1. Read `README.md`, `WORKFLOW.md`, and `agent-brain/operating-contract.md`.
2. Detect OS and architecture.
3. Run `python3 scripts/setup.py --check-only` (Windows: `py -3`).
4. Run `./scripts/setup.sh` or `scripts/setup.ps1` only after reviewing the
   planned local writes.
5. Run `./scripts/doctor.sh` or `scripts/doctor.ps1`.
6. Run `scripts/system-agentic providers` and classify each provider as
   `AVAILABLE`, `AUTH_REQUIRED`, `NOT_CONFIGURED`, `UNAVAILABLE`,
   `UNSUPPORTED`, or `UNVERIFIED`.
7. Install a reference repository only with the explicit opt-in
   `scripts/install-references.py --install --id ... --allow-network`.
8. Report `READY`, `PARTIAL`, or `BLOCKED` with exact evidence before doing
   framework work.

## Boundaries

- Keep credentials, cookies, browser profiles, account homes, model weights,
  databases, logs, plans, product projects, and generated artifacts outside Git.
- Do not install global software, modify shell profiles, change PATH, alter
  keychains, or change existing host configuration.
- Do not require or bundle 9router. It is an optional external integration.
- Do not claim Claude/Gemini execution without an included adapter, own auth,
  and an explicit data-flow decision.
- Use `READ_ONLY` for inspection and `WORKSPACE_WRITE` only for requested
  changes in the assigned workspace. Classifier output never grants elevation.
- Never reset, clean, force-push, overwrite dirty checkouts, or silently retry
  an uncertain external operation.
- Read reference reports and licenses before using an external module. Keep
  reference repositories independent; do not vendor their source.

## Host compatibility

Hosts may support instruction reading without supporting execution. Report the
actual state separately:

```text
INSTRUCTIONS_SUPPORTED
EXECUTION_ADAPTER_VERIFIED
EXECUTION_ADAPTER_NOT_INCLUDED
AUTH_REQUIRED
UNSUPPORTED
UNVERIFIED
```

Codex/ORCA is verified only on the owner's macOS host. Claude and Gemini are
instruction-compatible but execution adapters are not included. Windows has a
portable setup/doctor path; the macOS-specific local Qwen service and some
desktop integrations remain optional or unsupported.

## Completion report

For meaningful framework changes, report files changed, commands run, evidence,
data flow, limitations, and remaining `UNKNOWN`. Do not call a service healthy
based only on a registry entry, a process launch, or an agent self-report.
