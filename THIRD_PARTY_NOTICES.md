# Third-party notices

The release repository contains original System Agentic AI framework source under `LICENSE` (MIT). It does not vendor the reference repositories listed below. The optional installer fetches each upstream repository into a user-selected private references directory, preserving that repository's own license and history.

| Reference | Upstream | Observed license | Distribution boundary |
|---|---|---|---|
| Design OS System One | upstream reference | UNKNOWN in local checkout | fetched separately; owner/license review required |
| Orchestration System by Jang | upstream reference | MIT | fetched separately; retain upstream notice |
| gstack | upstream reference | MIT + NOTICE | fetched separately; retain upstream notice |
| Design OS Pedagogy | upstream reference | MIT | fetched separately; retain upstream notice |
| Design OS Knowledge Builder | upstream reference | MIT | fetched separately; retain upstream notice |
| Caveman | upstream reference | Apache-2.0 + third-party notices | fetched separately; retain all upstream notices |
| REA | upstream reference | MIT | fetched separately; read-only optional integration |
| agent-desktop | npm package `agent-desktop@1.2.0` | verify upstream package metadata at install time | executable is not vendored |

This file is a release inventory, not a grant of rights to third-party code. Review upstream license files before redistributing or modifying fetched references.

## Optional browser extension implementation patch

`integrations/browser-extension/implementation.patch` contains implementation
additions intended for the independent
[jangtrinh/design-os-browser-extension](https://github.com/jangtrinh/design-os-browser-extension)
checkout pinned in its manifest. The upstream source and knowledge pack are not
vendored. Preserve its PolyForm Shield 1.0.0 and required notice; the optional
implementation patch is distributed for use under that license, not relicensed
as MIT. See `integrations/browser-extension/LICENSE.upstream`.

Required Notice: Copyright (c) 2026 Jang Trinh (https://github.com/jangtrinh)

The optional installed runtime uses `ws@8.21.3` (MIT) fetched separately by npm
with its own notice retained. No node_modules or built extension is included.
