# Shared native desktop capability

System Agentic AI registers Vercel's `agent-desktop` as the `agent-desktop` module and exposes `desktop.automation` through one MCP stdio adapter at [`agent-brain/modules/agent_desktop_adapter.py`](../agent-brain/modules/agent_desktop_adapter.py). Projects inherit that adapter through the shared module registry; they do not copy code or add a project-specific master prompt.

## Pinned runtime

- Package: `agent-desktop@1.2.0`
- Source: <https://github.com/vercel-labs/agent-desktop>
- npm integrity: `sha512-sp8hHs8JfnuZe8ZQwiKZrqSOlu26SSeZXkjDog4KsU31L09G3uZJa8pITGN2vORif9uuh0iOSJHD146CEyTqag==`
- MCP command: executable and argv are passed separately as `agent-desktop mcp`
- MCP stdio framing: newline-delimited JSON-RPC (NDJSON), verified against the real binary
- MCP protocol observed: `2024-11-05`
- Upstream tools observed: 67 from `tools/list`
- Supported upstream platforms: macOS, Windows, Linux; this host is macOS 15.8.1 arm64, Node 24.21.0

Install is intentionally opt-in and machine-local:

```bash
./scripts/setup-agent-desktop.sh --install
```

Use `./scripts/setup-agent-desktop.sh --check` for a read-only probe and
`./scripts/setup-agent-desktop.sh --uninstall` to remove only this package.

The module requires the `agent-desktop` executable on PATH. Install it with the upstream documented method or configure an explicit executable path. A container, remote worker, or cloud workspace does not gain access to the user's display by registering this module.

## Project configuration

The host frames a module task with `targetModuleId: "agent-desktop"`, `requestedCapability: "desktop.automation"`, and a `context.desktop` object. Minimal read-only observation:

```json
{
  "enabled": true,
  "executable": "agent-desktop",
  "argv": ["mcp"],
  "session": "project-run-id",
  "headed": false,
  "trace": false,
  "max_actions": 0,
  "timeout_ms": 15000,
  "target": {"app": "com.example.App", "window_id": "window-ref"},
  "actions": []
}
```

`executable`, `session`, target app/window, timeout, trace, and action limit are project/run context. A project can set `enabled: false`; an executable override is checked by the same health probe. Relative `trace_output` paths must remain inside the task workspace.

Actions require `execution_policy: "WORKSPACE_WRITE"` plus `action_authorization: "explicit-task"`. This is a fail-closed task boundary: the shared adapter does not infer approval from a project file or from `ok: true`. Disallowed destructive notification/clipboard operations stay blocked.

## Runtime loop and evidence

The adapter starts one child MCP process per run, negotiates `initialize` and `tools/list`, loads the upstream desktop/platform skills, then performs an initial `desktop_snapshot`. Each action uses a ref from that latest snapshot, followed by a new snapshot. `STALE_REF`, `AMBIGUOUS_TARGET`, incomplete observations, MCP errors, timeout, cancellation, and uncertain delivery remain structured outcomes. A retry occurs only when the tool explicitly returns `disposition.retry: "safe"`, at most once, after a fresh snapshot.

An action without a caller-supplied `verify` assertion is `PARTIAL`/unverified even when the transport call returns successfully. Session IDs default to the task ID. Child processes are terminated on completion, cancellation, timeout, or failure. Evidence records the pinned version, tool result envelope, delivery field, observations, assertions, and optional trace path.

Actions may resolve a semantic selector against the latest snapshot instead of carrying a ref from an earlier session:

```json
{
  "tool": "desktop_click",
  "selector": {
    "name": "1",
    "role": "button",
    "native_id": {"kind": "ax_identifier", "value": "One"}
  },
  "verify": {"expect_change": true}
}
```

The adapter requires exactly one match, records the resolved snapshot ref, and returns `AMBIGUOUS_TARGET` for zero or multiple matches. `expect_change=true` compares semantic before/after snapshots after the fresh readback; volatile ref and snapshot IDs are excluded.

## Routing boundary

Use existing browser/API/DOM tools for web content and forms. Use `desktop.automation` for native apps, OS menus, file dialogs, and other surfaces exposed by agent-desktop. Hybrid/Electron flows must split those boundaries explicitly. On the verified Mac, Accessibility and Screen Recording are granted and live snapshot plus a harmless Escape action passed through dispatch. Automation is `unknown`; only flows that actually require System Events need that permission. Other hosts require their own binary, GUI session, and permissions.

The real upstream envelope may report a protocol envelope version (`2.6`) separately from the package version (`1.2.0`). Read `data.version` for the package. Tool failures preserve `structuredContent.error`, `ok`, and `disposition`; `ok=true` only proves transport/tool completion, so callers must assert the postcondition.

Check status and candidates without running a desktop action:

```bash
./scripts/system-agentic modules candidates --capability desktop.automation
./scripts/system-agentic modules candidates --all
```

Natural-language routing is shared. Prompts mentioning native apps, accessibility trees, OS menus, file dialogs, or desktop windows are prefiltered to `route_lane=DESKTOP` and `agent-desktop` when its live registry health is `READY`. Website/DOM prompts remain on `route_lane=BROWSER`; source/test/build prompts remain on `route_lane=CODING`. The decision is advisory and never grants permissions. A fresh project only supplies its workspace and optional `context.desktop`; it does not name an adapter in its prompt.

Focused adapter verification:

```bash
python3 -m unittest agent-brain/modules/test_agent_desktop_adapter.py
```
