# REA reverse-engineering capability

System Agentic AI vendors the official [morluto/rea](https://github.com/morluto/rea) source at `third_party/rea`, pinned in [`third_party/rea.pin.json`](../third_party/rea.pin.json). The local adapter is [`agent-brain/modules/rea_adapter.py`](../agent-brain/modules/rea_adapter.py).

## Install and update

```bash
cd third_party/rea
npm ci --ignore-scripts
npm run build:cached
```

Node must satisfy REA's declared range (`22.23.4+`, `24.22.1+`, or `26.12.1+`). The adapter resolves a compatible local Node executable; it never downloads or upgrades one.

## Capability and routing

Canonical capability: `REVERSE_ENGINEERING`.

Aliases: `reverse_engineering`, `reverse-engineering`, `rea`, `binary_analysis`, `application_analysis`, `runtime_analysis`, `evidence.collection`.

Prompts mentioning binary/app/APK/Electron/web/runtime inspection are prefiltered to `DISPATCH` + `rea`. Coding, ORCA computer-use, gstack review, and ambiguous requests keep their existing lanes. Decision output is advisory; it does not authorize execution.

## Health

```bash
PYTHONPATH=agent-brain python3.12 scripts/system-agentic provider-health rea
PYTHONPATH=agent-brain python3.12 scripts/system-agentic providers --json
```

Health starts a bounded MCP session, performs `initialize` and `tools/list`, then cleans up. `core`, JavaScript, native, Android/package, desktop, managed, firmware, browser/static web/runtime, network capture, and optional Ghidra/Hopper/IDA readiness are reported separately. APK decompilation requires `REA_JADX_MCP_JAR` plus Java; firmware requires Binwalk; packet captures require TShark. Missing tools degrade only that sub-capability.

## Safe execution

REA dispatch requires `READ_ONLY`, `context.target_path`, and explicit `context.target_authorized: true`. The adapter allows only local read-only artifact operations: JavaScript application analysis, desktop/.NET/managed inspection, firmware-region inspection, APK/package inspection, objdump/native export/binary layout inspection, web/packet capture inspection, and static artifact inspection. No target execution, injection, debugger control, authentication/DRM/license bypass, secret extraction, or mutation is admitted.

Example task context:

```json
{
  "target_path": "agent-brain/test-fixtures/reverse-engineering/electron-like",
  "target_type": "javascript_application",
  "target_authorized": true
}
```

REA evidence retains target digest/path, selected tool, provider/version/commit, findings, evidence, limitations, unknowns, artifacts, commands, timing, and errors. Downstream Codex/planner decides whether to explain, specify, or build.

## Limitations

Native deep analysis still depends on locally installed Ghidra/Hopper/IDA. Missing engines degrade their sub-capabilities only. Runtime observation is not implied by static evidence. REA MCP is one-shot per inspection; recovery starts a fresh owned process and never retries a failed analysis unchanged.

## Troubleshooting

- `MISCONFIGURED` / MCP unavailable: verify Node `22.23.4+`, `24.22.1+`, or `26.12.1+`, then rebuild `third_party/rea` with the install commands above.
- `android_package=UNAVAILABLE`: configure an authorized local `REA_JADX_MCP_JAR` and Java.
- `firmware=UNAVAILABLE`: install/configure Binwalk through `REA_BINWALK_COMMAND`.
- `network_capture=UNAVAILABLE`: install/configure TShark through `REA_TSHARK_COMMAND`.
- Runtime/web observation: provide a loopback `cdp_endpoint` or `inspector_endpoint` and an explicit `target_id`; no public or non-loopback endpoint is accepted.
