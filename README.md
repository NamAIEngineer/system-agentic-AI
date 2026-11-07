# System Agentic AI Workbench

Provider-neutral agent framework for Codex/ORCA, local Qwen, and other hosts.
The repository contains the shared brain, routing contracts, bounded runtime,
host setup scripts, and capability documentation. Product workspaces and their
private plans stay outside this public framework bundle.

## Clone and install

```sh
git clone https://github.com/NamAIEngineer/system-agentic-AI.git
cd system-agentic-AI
./scripts/setup.sh
./scripts/doctor.sh
```

The setup script is the cross-platform bootstrap for this workspace. It creates
only ignored local state, checks the host tools, and installs the public
reference repositories declared in `sources.manifest.json` when Git is
available. It never copies credentials or runtime databases into the
repository. Re-running it is safe.

On Windows, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\doctor.ps1
```

The framework remains usable for interactive ORCA work even when optional
local models or providers are unavailable.

## Use from ORCA

Open this cloned directory as the ORCA workspace. ORCA reads the root
`AGENTS.md`, then follows the setup and doctor commands for the host operating
system. The shared operating contract lives in
`agent-brain/operating-contract.md`; the installable skill lives in
`agent-brain/skills/system-agentic-ai/SKILL.md`.

Reference repositories are installed into ignored local directories. They are
not copied into the public Git history and their own licenses remain in force.

## Use from a host

```sh
./scripts/system-agentic providers
./scripts/classify-task.sh "describe a task"
./scripts/agentic-runtime service status
./scripts/agentic-runtime submit --workspace "$PWD" "Inspect this workspace and report evidence."
```

Codex/ORCA is the verified host adapter. Claude Code can consume the same
`AGENTS.md`, `README.md`, and operating contract, but Claude authentication and
a Claude execution adapter remain a separate configuration. Gemini and other
hosts are catalogued only when their own credentials and adapters are present.
See [docs/HOSTS.md](docs/HOSTS.md).

## What is included

- `agent-brain/`: provider-neutral operating contract, provider registry,
  Decision Plane, module registry, bounded adapters, optimization policy, and
  the `system-agentic-ai` host skill.
- `scripts/`: provider discovery, task/runtime CLI, local classifiers, browser
  boundary helpers, Codex brain installer, and this workbench setup script.
- `products/agent-runtime/`: persistent SQLite queue and local worker source,
  tests, acceptance contract, and runtime report.
- `packages/`: reusable TypeScript capability and risk contracts.
- `product-kits/`: optional project scaffolds.
- `docs/`: host instructions, ORCA integration notes, desktop/REA boundaries,
  and live repository status.

Optional native desktop capability:

```sh
./scripts/setup-agent-desktop.sh --install
./scripts/setup-agent-desktop.sh --check
```

This installs the pinned `agent-desktop` executable for the current user. The
candidate does not bundle the executable or any desktop session state. See
[docs/agent-desktop-release-note.md](docs/agent-desktop-release-note.md).

The optional reference installer fetches the public repositories declared in
`sources.manifest.json`. Their upstream licenses and histories stay with their
own repositories. 9router is deliberately outside this bundle and is never a
required dependency.

## Provider boundary

The registry separates policy from live availability. Current capabilities are:

- Codex/OpenAI: authenticated queue adapter when the user's Codex home and
  runtime worker are available.
- Local Qwen3: optional offline worker on a configured macOS host.
- REA: local read-only reverse-engineering provider when its checkout is ready.
- Claude/Gemini: disabled placeholders; no silent fallback, credential sharing,
  or claim of execution support.

Run `./scripts/system-agentic providers` before relying on a capability. A
classifier result is advisory; it never grants permission for external writes.

## Repository boundary

This public repository intentionally excludes `plans/`, product repositories
under `products/` except the framework runtime, generated artifacts, screenshots
and media runs, SQLite databases, logs, model weights, browser profiles,
credentials, and nested Git histories. See [docs/REPOSITORY_STATUS.md](docs/REPOSITORY_STATUS.md)
for the checked upstream revisions and known local checkout issues.

## Operating contract

Read [agent-brain/operating-contract.md](agent-brain/operating-contract.md)
before adding an adapter or changing provider behavior. Shared source remains
in `agent-brain/`; provider account homes consume it through the installer.
