# Agent Desktop status

The host audit found `agent-desktop` version `1.2.0` installed in the user's local npm prefix. The public candidate includes the provider-neutral adapter, tests, registry metadata, documentation, and `scripts/setup-agent-desktop.sh`.

The executable itself is intentionally not bundled. A fresh user installs it with:

```sh
./scripts/setup-agent-desktop.sh --install
./scripts/setup-agent-desktop.sh --check
```

The adapter resolves `agent-desktop` from `PATH` or the user's standard local npm bin directory. It starts a per-task stdio MCP process, keeps executable/argv separate, requires snapshot/readback around actions, and permits native mutation only with explicit task authorization. Installation does not grant access to the current user's display, ORCA session, cookies, or credentials.
