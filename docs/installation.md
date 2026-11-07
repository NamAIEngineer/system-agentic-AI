# Installation

1. Install Python 3.10+ and Git.
2. Clone this repository.
3. Run `./scripts/setup.sh`.
4. Run `./scripts/doctor.sh` and `./scripts/smoke-test.sh`.

The static framework has no mandatory Python lockfile. The optional queue and provider adapters add host-specific requirements documented in `products/agent-runtime/README.md`.

The local Qwen worker needs an Apple Silicon MLX environment, a user-downloaded model, and a per-user service configuration. This candidate does not bundle weights or launch agents.

Install and authenticate Codex separately. `scripts/install-codex-brain.sh` can sync the shared skill into a user-owned Codex home after review; it never copies account credentials.

ORCA, 9router, ChatGPT Desktop, browser profiles, and their launch agents are external host components. Install them through their official distribution. This candidate neither starts nor stops them.

## Optional native desktop capability

Install the pinned `agent-desktop` executable for the current user:

```sh
./scripts/setup-agent-desktop.sh --install
./scripts/setup-agent-desktop.sh --check
```

The executable is not vendored. The adapter uses a per-task stdio MCP process and keeps native desktop mutation behind explicit authorization.
