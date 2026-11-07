# Configuration

`agent-brain/providers/registry.json` is the provider-neutral policy source. Live availability is separate; run `./scripts/system-agentic providers` on the target host. A registry entry is not proof that a provider is installed, authenticated, reachable, or healthy.

Common environment variables:

- `SYSTEM_AGENTIC_WORKSPACE_ROOT`: portable module discovery root.
- `SYSTEM_AGENTIC_RUNTIME_ROOT`: private queue state root.
- `SYSTEM_AGENTIC_CHATGPT_STATE_ROOT`: private state root for an explicitly configured bridge.
- `SYSTEM_AGENTIC_CHATGPT_BRIDGE_URL`: loopback bridge URL.
- `HSTRY_BIN`: optional local hstry executable.
- `TYPESAFE_API_KEY`: only for an explicitly authorized hybrid public-research task.

Keep cookies, refresh tokens, private keys, API keys, browser profiles, and service databases outside Git. Empty or unavailable provider settings fail closed; they do not silently fall back to another account.
