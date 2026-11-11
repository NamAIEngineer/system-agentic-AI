# System Agentic AI

Provider-neutral AI workspace framework: shared operating contract, agent brain,
bounded runtime, provider discovery, host skills, cross-platform bootstrap, and
verification. It is a repository framework, not a GUI, SaaS product, dashboard,
model bundle, account manager, or 9router distribution.

## Introduction film

Watch the [54-second English introduction](https://github.com/NamAIEngineer/system-agentic-AI/releases/download/intro-video-2026-11-11/system-agentic-intro-1080p.mp4) or the lightweight [GIF preview](media/assets/system-agentic-intro-preview.gif). The film walks through a safe provider-readiness prompt, the decision-plane result, live provider inventory, `READ_ONLY` boundaries, and a verification receipt.

The composition is reproducible from [`media/`](media/), with [captions](media/system-agentic-intro.vtt), a [transcript](media/transcript.md), and sanitized [evidence readbacks](media/evidence/). JSON values come from a local run pinned to public commit `7b7168a`; the advisory result did not authorize or submit a task. Connector geometry and policy panels are labeled illustrative in the film. No credentials, account data, or external writes are included.

## First run

macOS, Linux, or Windows with Python 3.10+ and Git:

```sh
git clone https://github.com/NamAIEngineer/system-agentic-AI.git
cd system-agentic-AI
python3 scripts/setup.py --check-only
./scripts/setup.sh
./scripts/doctor.sh
```

Windows PowerShell:

```powershell
git clone https://github.com/NamAIEngineer/system-agentic-AI.git
cd system-agentic-AI
py -3 scripts/setup.py --check-only
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\doctor.ps1
```

Then open the cloned folder in ORCA or another compatible AI coding host and
ask the host to read `AGENTS.md`, `README.md`, `WORKFLOW.md`, and
`agent-brain/operating-contract.md` before initializing the workspace.

In ORCA, use this single first-run instruction:

```text
Read AGENTS.md and fully initialize this System-Agentic-AI workspace.
```

ORCA auto-loading is host-specific and remains `UNVERIFIED` without host
evidence. This sentence is the supported fallback. The host must show approval
plans before network/model downloads, dependency execution, or writes outside
the checkout.

`setup.py` and `doctor.py` do not install global software, modify shell profiles,
change credentials, start services, or require ORCA/9router. The default setup
path is local and offline. Reference repositories are optional and require an
explicit command:

```sh
python3 scripts/install-references.py --check-only
python3 scripts/install-references.py --install --all --allow-network
```

Use `--skip-references` in CI. Never point the installer at an existing dirty
checkout owned by another project.

The installer keeps the seven references outside Git and emits independent
counts for source cloning, pinned revision verification, capability
registration, and actual execution verification. A cloned or pinned reference
is not automatically execution-ready. `NOASSERTION_REVIEW` entries require
license/ownership review before running their code.

Quyet Tiny is an optional local advisory classifier:

```sh
python3 scripts/setup-quyet.py --check-only --json
python3 scripts/setup-quyet.py --verify --json
python3 scripts/setup-quyet.py --install --allow-network --allow-model-download
```

The install command requires separate approval for public downloads and model
weights. Weights remain in user cache, outside Git. Quyet output never grants
execution permission; unavailable or low-confidence results fall back to host
reasoning.

## What the framework provides

- `agent-brain/`: provider-neutral contract, provider/module registries,
  decision plane, bounded adapters, tool runtime, and host skill.
- `products/agent-runtime/`: optional local queue/worker source. Runtime SQLite,
  logs, model weights, and service state stay outside Git.
- `packages/agent-core/`: reusable TypeScript capability/risk contracts.
- `product-kits/`: generic scaffolds, not user projects.
- `scripts/`: setup, doctor, reference installer, provider inventory, runtime
  CLI, browser helpers, and smoke checks.
- `docs/`: architecture, host boundaries, provider status, configuration,
  troubleshooting, and source reports.

## Host/provider support

Codex/ORCA is the only execution adapter verified on the owner machine. Claude
and Gemini can consume Markdown instructions but have no included execution
adapter. Local Qwen, REA, browser, ChatGPT bridge, and `agent-desktop` are
optional integrations whose live readiness must be checked on the user's host.
Run:

```sh
python3 scripts/system-agentic providers
python3 scripts/doctor.py --json
```

Doctor reports `core_status`, `references_status`, `quyet_status`, per-provider
optional status, and `host_execution_status`. Missing optional providers do not
downgrade a healthy core. A real host task is required before claiming
execution readiness.

`agent-desktop` is optional and never bundled as a binary:

```sh
./scripts/setup-agent-desktop.sh --install
./scripts/setup-agent-desktop.sh --check
```

9router is an optional external integration. System Agentic AI does not require
9router. Do not copy its config, account pool, credentials, or runtime state.

## Runtime task example

After a user has deliberately configured a compatible local worker:

```sh
./scripts/agentic-runtime providers
./scripts/agentic-runtime submit --workspace "$PWD" "Inspect this workspace and report evidence."
```

Explicit Codex processing is per-task and uses the user's own host login:

```sh
./scripts/agentic-runtime submit --backend codex --workspace "$PWD" "Run this task through Codex."
```

No provider is silently selected as a fallback. An unavailable optional provider
returns `AUTH_REQUIRED`, `NOT_CONFIGURED`, `UNAVAILABLE`, `UNSUPPORTED`, or
`UNVERIFIED`; it does not block the static framework.

## Safety and data boundary

Keep credentials, cookies, browser profiles, account homes, model caches, queue
databases, logs, private projects, plans, and generated artifacts outside Git.
The framework never inherits the owner's credentials. Read [SECURITY.md](SECURITY.md)
and [docs/configuration.md](docs/configuration.md) before enabling a provider.

## Verification

```sh
./scripts/smoke-test.sh
```

The smoke test is deterministic and offline. It checks Python syntax, manifest
and registry shape, setup/doctor check-only paths, shell syntax, symlink safety,
and CLI help. It does not claim that ORCA, a browser, 9router, a model, or a
cloud account is authenticated.

## License

Original System Agentic AI code in this repository is MIT-licensed where the
owner has distribution rights. Reference repositories remain separate and
retain their own licenses. See [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
