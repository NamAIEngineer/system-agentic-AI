# Installation

1. Install Python 3.10+ and Git.
2. Clone this repository.
3. Run `./scripts/setup.sh`.
4. Run `./scripts/doctor.sh` and `./scripts/smoke-test.sh`.

After opening the folder in ORCA, give it one instruction:

```text
Read AGENTS.md and fully initialize this System-Agentic-AI workspace.
```

The host must show approval before network downloads, model downloads,
dependency execution, or writes outside the checkout. Native AGENTS.md
auto-loading is host-specific; mark it `UNVERIFIED` without evidence.

The static framework has no mandatory Python lockfile. The optional queue and provider adapters add host-specific requirements documented in `products/agent-runtime/README.md`.

The local Qwen worker needs an Apple Silicon MLX environment, a user-downloaded model, and a per-user service configuration. This candidate does not bundle weights or launch agents.

Install and authenticate Codex separately. `scripts/install-codex-brain.sh` can sync the shared skill into a user-owned Codex home after review; it never copies account credentials.

ORCA, 9router, ChatGPT Desktop, browser profiles, and their launch agents are external host components. Install them through their official distribution. This candidate neither starts nor stops them.

## Optional seven-reference bootstrap

Review the plan without network access:

```sh
python3 scripts/install-references.py --check-only
```

After one explicit approval for the displayed public downloads:

```sh
python3 scripts/install-references.py --install --all --allow-network
```

Checkouts go to the user-local references directory, never into tracked source.
The receipt separates cloned source, pinned commits, capability registration,
and execution verification. Dirty or mismatched existing checkouts are kept and
reported; they are never reset or overwritten.

## Optional Quyet Tiny bootstrap

Quyet is a local advisory classifier, not an execution model:

```sh
python3 scripts/setup-quyet.py --check-only --json
python3 scripts/setup-quyet.py --verify --json
```

Install dependencies and model only after separate approval for network and
model download:

```sh
python3 scripts/setup-quyet.py --install --allow-network --allow-model-download
```

Weights stay outside Git. Missing or low-confidence Quyet returns control to
host reasoning and never authorizes a task.

## Optional native desktop capability

Install the pinned `agent-desktop` executable for the current user:

```sh
./scripts/setup-agent-desktop.sh --install
./scripts/setup-agent-desktop.sh --check
```

The executable is not vendored. The adapter uses a per-task stdio MCP process and keeps native desktop mutation behind explicit authorization.
