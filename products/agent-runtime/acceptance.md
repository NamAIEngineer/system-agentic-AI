# Product acceptance

- A1. Task CLI persists queue entries and rejects missing/invalid workspaces and empty prompts.
- A2. New tasks invoke local inference and retain attempt logs plus the model/tool conversation.
- A3. Interruptions require deliberate local conversation resume or explicit new-session restart; existing Codex jobs retain exact-session resume.
- A4. Per-user LaunchAgent runs the worker on login with no network endpoint.
- A5. Local jobs can search/read fixed workbench references and perform read-only public web research through a profile-checked Chrome instance; browser page text is untrusted, and no form or account write actions are exposed.
- A6. Local jobs may read bounded GitHub issue metadata through the authenticated local `gh` CLI; hybrid jobs must not expose this tool to the TypeSafe path, and no GitHub mutation is available.
- A7. A local evaluation runner covers code, knowledge, browser research, planning, and safe refusal with deterministic checks and no external model provider.
- V1. Local model inference and a bounded queue-to-tool task are evidenced in `REPORT.md`.
