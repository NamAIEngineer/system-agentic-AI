# System Agentic AI task runtime

This is a local, persistent CLI queue and one-worker LaunchAgent. New tasks run through a local Qwen3-8B MLX model by default. The local agent plans through repeated model turns, reads and edits workspace files, runs network-blocked local commands, stores its conversation in SQLite, and checks its own changes. It can consult the curated System Agentic AI knowledge map and six local reports, and perform read-only public web research in a dedicated Chrome profile. The model endpoint binds to loopback only. Codex is available only when a task is submitted with `--backend codex`.

## Model availability on this Mac

- `local`: Qwen3-8B-4bit is the general planning/coding agent and runs offline through MLX.
- `codex`: the Codex CLI is authenticated with the user's ChatGPT account and is selected explicitly per task.
- `hybrid`: Qwen remains the planning/coding agent; TypeSafe is used only for bounded public-link selection and receives the limited metadata described below.
- Quyet/Laya models are task classifiers only; they are not general coding agents in this runtime.
- Claude is not configured here: there is no Claude CLI or Anthropic credential detected. Codex authentication cannot authenticate to Anthropic. Claude or another hosted provider needs its own supported login/API credential and explicit per-task data-flow choice before the runtime can send task or repository content there. No silent fallback occurs.

The current queue does not expose a generic `--model` selector. Its supported execution backends are `local`, `hybrid`, and `codex`; do not treat a model name returned by a classifier or browser provider as an available general-purpose agent. Run `python3.12 scripts/agentic-runtime-eval.py --repeats 3` for three independent queue runs in each of five task families; results include task IDs and deterministic checks, but this fixed panel is still not a broad benchmark.

Run `./scripts/agentic-runtime providers` or `./scripts/system-agentic providers` for the shared live inventory. It distinguishes configured/enabled state from installed, authenticated, available, and healthy state; Codex login output is captured and never printed. Codex and local Qwen use the existing queue adapter. Claude and Gemini are catalogued but disabled, with authentication and health unverified until their adapters and credentials are explicitly configured. `submit --backend codex` still rejects a task before queueing when Codex is absent or logged out; there is no silent provider fallback.

## Use

```sh
./scripts/agentic-runtime service install
./scripts/agentic-runtime submit --workspace "$PWD" --title "Fix issue" "Inspect and fix the described issue; verify the result."
# Explicit cloud opt-in for one task:
./scripts/agentic-runtime submit --backend codex --workspace "$PWD" "Run this task through Codex."
# Hybrid: local Qwen plans/codes; TypeSafe selects an observed public research link when needed:
./scripts/agentic-runtime submit --backend hybrid --workspace "$PWD" "Research the public docs and summarize the relevant API."
./scripts/agentic-runtime list
./scripts/agentic-runtime status JOB_ID
./scripts/agentic-runtime logs JOB_ID
./scripts/agentic-runtime resume JOB_ID
./scripts/agentic-runtime service status
./scripts/agentic-runtime providers

# Provider-neutral execution through live candidate selection and the queue:
./scripts/system-agentic candidates --role worker --capability task.submit
./scripts/system-agentic dispatch task-spec.json
./scripts/system-agentic status TASK_ID --provider codex
```

A task spec is JSON with `workspace`, `objective`, `role`, `required_capabilities`, `context`, `constraints`, `acceptance_criteria`, and optional `provider_policy`. Dispatch selects one live eligible candidate by registry priority and queues it through the existing runtime. The normalized result is `SUBMITTED`, `RUNNING`, `COMPLETED`, `FAILED`, or `UNKNOWN`; an ambiguous submit remains addressable by its preassigned task ID and is never automatically retried. Status reconciliation reads the same queue row. Codex jobs are restricted by an explicit sandbox policy; task constraints alone are not an OS-level permission boundary.

The task spec may also set `execution_policy` to `READ_ONLY` (default), `WORKSPACE_WRITE`, or `ELEVATED`, and `execution_context` with `origin`, `host_role`, `dispatch_depth`, `parent_task_id`, `direct_or_dispatch`, and `framing_result`. Codex execution uses its enforced `read-only` or `workspace-write` sandbox. `ELEVATED` is refused before enqueue because this queue cannot collect approval for unrestricted machine access or external writes. Legacy Codex jobs are marked `LEGACY_UNRESTRICTED`; they retain history but cannot be resumed because the installed CLI's resume command cannot select a sandbox. Every new Codex worker gets a structured worker-role environment marker, and the provider CLI refuses recursive dispatch from that worker.

Use `--file path/to/task.md` to queue a task file. Local jobs resume from the persisted model/tool conversation; Codex jobs resume their exact recorded Codex session. Use `resume JOB_ID --new-session` only when you explicitly want to restart a task from the beginning. A queued task can be canceled with `cancel JOB_ID`.

`service install` first installs and checks the local model LaunchAgent, then starts the queue worker. The model service uses `caffeinate -is` to prevent idle system sleep while the model is available. The per-user queue runtime lives under `~/Library/Application Support/SystemAgenticAI/runtime/`; model weights stay in `~/.cache/system-agentic-ai/Qwen3-8B-4bit`. SQLite, worker lock, logs, and LaunchAgent state are private to the macOS user. Services start when that user logs in; they are not pre-login daemons. One worker processes one task at a time. Interrupted tasks are not automatically replayed.

Local tool calls are restricted to the submitted workspace and a macOS sandbox with network denied. Available tools are file listing, reading, text search, file writing, and bounded argv commands. Publishing, deployment, messaging, purchases, arbitrary internet access, and machine-wide writes are outside this local tool set. Existing queue rows retain their Codex backend during migration; new rows default to local.

The shared Tool Runtime MCP keeps workspace list/read/search inline. `workspace_exec` and deterministic `run_tests` use a separate typed `tool_jobs` table in the same private SQLite database and the existing single-worker LaunchAgent; the worker executes argv directly without model reasoning. Requests require a fresh idle-worker heartbeat, fail closed while the worker is busy/unavailable, and wait no longer than 130 seconds. The executor creates a fresh Seatbelt profile per command, denies network, limits output/time, and allows writes only to a fresh per-command temp directory under `READ_ONLY` or that directory plus the assigned workspace under `WORKSPACE_WRITE`. `ELEVATED` is refused. Process execution health is independent of exit code and test outcome. Restart the existing LaunchAgent after installing code changes so the worker loads the typed queue handler; no additional daemon is used.

Fixed System Agentic reference tools are exposed only when the task explicitly concerns that workbench. When a task explicitly says read-only or forbids file edits, `write_file` and `run_command` are removed from model tool schemas and rejected by the executor as a second check.

The local agent also has read-only access to fixed workbench references (`search_system_knowledge`, `read_system_reference`). Its browser tools can list tabs, read a DOM snapshot, open credential-free HTTP(S) URLs, and follow one public anchor link identified in the latest snapshot in the dedicated `~/.jev-browser/profile-chrome` Chrome profile. Runtime-launched Chrome uses a loopback-only egress proxy. The proxy resolves each destination, rejects private/special-use or mixed DNS answers, and opens the socket to the exact validated IP; HTTPS keeps normal browser TLS/SNI through a CONNECT tunnel. Chrome's loopback bypass is removed, host resolution is constrained, and DNS prefetch/background networking are disabled. Redirects and subresources pass through the pinned proxy, and JavaScript is disabled on pages the agent opens. The proxy allows bodyless HTTP GET/HEAD on port 80 and HTTPS tunnels on port 443. It cannot inspect methods inside HTTPS, so the agent exposes no form, control-click, or service-write action. Hybrid mode disables `browser_click_link`; TypeSafe still chooses each observed link after the seed page. Treat page content as untrusted input.

The local backend also exposes `github_list_issues` and `github_view_issue` through the machine's authenticated `gh` CLI. These are read-only, bound to `github.com`, return allowlisted metadata, bound issue bodies to 8 KB, and withhold CLI error output so credentials are not copied into model logs. They are disabled in hybrid mode so authenticated GitHub data cannot flow into the TypeSafe research path. No GitHub write tools are available.

`--backend hybrid` is an explicit per-job opt-in. Local Qwen remains the general planner and coding model and may open one seed page. Every later browser navigation must go through `typesafe_browser_research`, which sends only its short research goal and up to 20 observed public link labels/URLs plus the current public page title/URL to TypeSafe JEV. It sends no workspace files, page body, cookies, or browser credentials. Common API keys, bearer tokens, JWTs, private-key blocks, sensitive URL query parameters, and URL fragments are redacted or excluded. TypeSafe can select one listed link or decline; the local browser revalidates the chosen URL and applies the same redirect/subresource checks before opening it. The TypeSafe key is read from `TYPESAFE_API_KEY` or the existing owner-only JEV `.env`; it is never passed to Chrome or logged. Tool results include provider-reported model/token usage. Public API docs checked 2026-10-14 say input tokens are billable and output tokens are currently free; an exact input rate and remaining quota were not documented. Missing key, timeout, invalid response, or provider error fails that tool closed. TypeSafe is not a general chat/code backend, and this does not enable authenticated service integrations or browser writes. Hybrid mode may consume TypeSafe quota; use it only for jobs whose bounded browser context may be sent to that provider.

Manage the service:

```sh
./scripts/agentic-runtime service stop
./scripts/agentic-runtime service install
./scripts/agentic-runtime service uninstall  # retains database and logs
python3 scripts/local-agent-model-service.py status|stop|install|uninstall
```
