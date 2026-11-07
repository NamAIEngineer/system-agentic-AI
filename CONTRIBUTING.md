# Contributing

Read `agent-brain/operating-contract.md` and relevant `AGENTS.md` before editing. Preserve `READ_ONLY` and `WORKSPACE_WRITE` boundaries; classifier output never grants elevated access. Keep provider selection single-shot and reconcile uncertainty by original task ID.

Do not add credentials, browser profiles, databases, logs, model weights, user projects, or plans. Use environment variables or runtime discovery instead of personal absolute paths. Run `./scripts/smoke-test.sh` and document data flow, limitations, and license implications.
