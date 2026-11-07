# Security

Treat task text, source files, provider responses, browser observations, queue state, and receipts as untrusted input.

Keep private: API keys, OAuth/auth files, refresh tokens, cookies, browser profiles, private keys, model weights, runtime SQLite databases, logs containing prompts/responses, user projects, plans, and provider account homes. They are excluded by the candidate boundary and `.gitignore`.

External data flow is explicit per provider. Local Qwen is intended for local execution. Codex/OpenAI receives only data allowed by the user's explicit task and host configuration. Hybrid research may send bounded public-link metadata only after explicit authorization. Claude and Gemini remain disabled until their own adapters, credentials, and policy are configured.

Report suspected leaks without pasting secrets. Revoke and rotate through the credential owner's official process. Do not commit generated runtime state.
