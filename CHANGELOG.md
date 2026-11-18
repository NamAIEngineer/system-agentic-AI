# Changelog

## 2026-11-18 — Claude and DeepSeek instruction entrypoints

- Import canonical shared rules from CLAUDE.md and add explicit DEEPSEEK.md guidance.
- Document host/backend and data-flow boundaries without enabling providers.
- Check both instruction entrypoints in setup, doctor and smoke validation.

## 2026-11-16 — optional paired Chromium transport

- Extend browser.automation with explicit extension transport and scoped task policy.
- Add opt-in pinned installer and checksummed implementation patch; preserve upstream license.
- Add native semantic input, batching, navigation fences and uncertain receipt recovery.
- Keep profiles/credentials out of Git and default Playwright unchanged; document verified scope.

## v0.1.0-rc2 — 2026-11-07

- Cross-platform smoke gate fixed for disposable bytecode and Windows shell path behavior.
- REA documentation now points to the optional external reference installer; no source is vendored.

## v0.1.0-rc1 — 2026-11-07

- Public framework boundary for the provider-neutral brain, runtime source, contracts, skills, bootstrap, doctor, and optional reference installer.
- Cross-platform check/install paths for macOS, Linux best-effort, and Windows.
- Explicit optional 9router, provider, browser, REA, Qwen, ChatGPT bridge, and agent-desktop boundaries.
- Excluded user projects, plans, credentials, runtime state, model weights, and nested reference repositories.
