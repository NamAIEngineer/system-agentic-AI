# Optional paired Chromium browser extension

The existing `browser-agent` / `browser.automation` module accepts
`context.browser.transport: "extension"`. Default disposable Playwright is
unchanged. No new model, provider, account, service at boot, or implicit external
write is enabled.

## Install explicitly

Review the local plan and upstream license first:

```sh
python3 scripts/install-browser-extension.py
```

After approving the displayed public Git/npm downloads and local dependency
execution, install into the ignored `.system-agentic-local/` directory:

```sh
python3 scripts/install-browser-extension.py --install --allow-network --allow-dependency-execution
scripts/system-agentic-browser-extension start
```

The installer fetches a pinned independent upstream repository, verifies the
implementation patch checksum, applies our implementation additions, installs
`ws` from its lockfile without npm lifecycle scripts, and builds the unpacked
extension. It never resets or overwrites an existing checkout. A failed install
remains for inspection. No reference source, model, profile, account, generated
extension build, or live connection file is vendored in this repository. The
patch preserves the upstream PolyForm Shield license and required notice; it is
not relicensed under the framework's MIT license.

The bridge runs in the foreground and prints a private connection-file path.
Load the resulting `dist/` folder in a **dedicated Chromium profile**. Review the
debugger, tabs, webNavigation, storage and activeTab permissions. Open its popup,
load that connection JSON, choose a tab and click **Pair selected tab**. The user
can revoke access from the same popup. Do not put the file or its tokens in a
prompt, task, Git commit, shared log, or Notion.

```sh
scripts/system-agentic-browser-extension status
scripts/system-agentic-browser-extension receipt RECEIPT_ID
```

`open` is an optional owner-host demonstration using an already installed
Playwright runtime and Chrome for Testing. It opens a dedicated profile and
pairs the public upstream GitHub repository. It never imports personal cookies.
Set `SYSTEM_AGENTIC_PLAYWRIGHT_PROJECT` to the installed Playwright project's
absolute directory (containing package.json and node_modules), and
`CHROMIUM_EXECUTABLE_PATH` if the browser is not at the owner's macOS cache path.
No Playwright/browser/model is downloaded automatically. macOS Chromium was
verified; other operating systems and browser locations require separate live
verification. Firefox, Safari and cross-origin iframe actions are unavailable.

## Task permission and result contract

Read-only context:

```json
{"browser":{"transport":"extension","authorized_origins":["https://github.com"],"actions":[]}}
```

A user-authorized write additionally requires `WORKSPACE_WRITE`, explicit
`allow_external_mutation: true` and a final `expect_text`. Actions are bounded to
40 sequential semantic `click` / `fill` entries with exact unique `role` and
`name` obtained from observation. There is no arbitrary script, CSS action, login,
payment, file upload, drag/drop or automatic rollback RPC.

```json
{"browser":{"transport":"extension","authorized_origins":["http://127.0.0.1:8000"],"allow_external_mutation":true,"actions":[{"action":"fill","role":"textbox","name":"Title","text":"New title"},{"action":"click","role":"button","name":"Save"}],"expect_text":"Saved: New title"}}
```

A paired tab is scoped to its selected origin. Each mutation is fenced by lease,
controller epoch, live page/loader identity, navigation invalidation, deadline
and observed element identity. Chromium native input handles rich editable blocks,
closed shadow roots and nested buttons. Occluded, ambiguous, disabled and stale
targets fail closed. Visible challenge/auth/payment indicators trigger human
handoff. Detection is conservative, not a guarantee for every custom UI/language.
Resolve gates directly; no bypass or external AI fallback is included.

Duplicate request IDs return their receipt; changed payloads conflict. Timeout,
disconnect, or interrupted dispatch becomes UNCERTAIN, normalized to UNKNOWN
with a recovery reference. **Never replay an uncertain mutation.** Use the
receipt command and read-only observation to reconcile. Partial batches retain
completed-action receipts and stop on the first failure; prior edits remain.
A visible text assertion is not proof of a remote commit unless the application
exposes that saved-state signal.

The session directory and files are private (0700 / 0600), ignored by Git, and
protected by an exclusive bridge process lock. After a crash inspect the recorded
PID before manually removing a stale lock. Journals fail closed at capacity;
do not delete uncertain receipts to force a replay. Session storage retains
request bodies including entered text; avoid secrets in agent actions.

## Evidence and limits

Owner-host verification used actual isolated Chromium and the real Python module:
read/edit/save/reload, a three-row batch, trusted events, duplicate suppression,
closed shadow roots, unnamed rich blocks, nested buttons, occlusion, full/SPA stale
fences, forbidden origins and human gates. Bridge tests cover authorization,
process locking, timeout, late response, conflict and restart receipts. Shared
unit tests cover permissions, ambiguity, postconditions, partial results and
UNKNOWN normalization.

Public reads were also verified against GitHub, Notion database help and Example
Domain. These do **not** certify authenticated GitHub/Notion writes. Sign in
personally and pair the intended tab before scoped account operations. Snapshots
are bounded at 200 controls and 12,000 text characters and flag truncation; there
is no implicit pagination. This transport itself makes zero model calls; a local
planner may choose semantic targets but cannot grant permissions. No token or
whole-agent speed improvement is claimed from payload byte reductions alone.

To rerun browser tests after installation, provide the framework root and an
already installed Playwright project, then run `npm test` in the implementation
checkout. `npm test` rebuilds the extension before its real Chromium tests.

Primary platform references:

- https://developer.chrome.com/docs/extensions/reference/api/debugger
- https://developer.chrome.com/docs/extensions/reference/api/storage
- https://developer.chrome.com/docs/extensions/how-to/web-platform/websockets

Research source: https://github.com/jangtrinh/design-os-browser-extension at
`27ccff76fd151d37b05e104d29b4d95d76b59666`. Its wider knowledge-pack saturation
remains DEEPEN; the implementation tests do not relabel that research gate.
