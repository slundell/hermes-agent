# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Canonical developer guide

`AGENTS.md` (repo root, ~1100 lines) is the authoritative, detailed reference for this
codebase — architecture deep-dives, per-subsystem conventions, and a "Known Pitfalls"
section. **Read the relevant section of `AGENTS.md` before working in an unfamiliar
subsystem.** `CONTRIBUTING.md` covers the skill-vs-tool decision and PR process. This
file is the short orientation; `AGENTS.md` is the depth.

## Commands

```bash
source .venv/bin/activate          # or: source venv/bin/activate

# Tests — ALWAYS use the wrapper, never call pytest directly. The wrapper
# enforces CI parity (unset API keys, TZ=UTC, LANG=C.UTF-8, subprocess-per-test).
scripts/run_tests.sh                                   # full suite
scripts/run_tests.sh tests/agent/                      # one directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x   # one test
scripts/run_tests.sh --no-isolate tests/foo/           # faster, for debugging only
scripts/run_tests.sh -- -v --tb=long                   # pass-through pytest flags

# Lint — only PLW1514 (unspecified-encoding) is enforced and blocks merge.
ruff check .                       # blocking rule set
ruff check --diff .                # advisory diff (ty + ruff also run as advisory in CI)

# Run the agent locally
./hermes                           # interactive CLI (auto-detects venv)
hermes doctor                      # diagnose install issues
```

Install for development: `./setup-hermes.sh` (or `uv pip install -e ".[all,dev]"`).
Python 3.11+. Dependencies are **exact-pinned** (`==X.Y.Z`, no ranges) — see the
dependency-pinning rationale in `pyproject.toml`; regenerate `uv.lock` with `uv lock`
after any bump.

## Architecture

Hermes is a self-improving AI agent: a synchronous tool-calling loop wrapped by
multiple front-ends (CLI, TUI, messaging gateway, ACP server) and extended by an
auto-discovered tool/skill/plugin ecosystem.

**Core loop** — `run_agent.py` (`AIAgent` class, ~12k LOC). `run_conversation()` is the
synchronous loop: call model → dispatch tool calls via `handle_function_call()` →
append tool-result messages → repeat until no tool calls or budget exhausted. Messages
use OpenAI chat format; reasoning lives in `assistant_msg["reasoning"]`.

**Tool layer** — import-time registration chain:
`tools/registry.py` (no deps) ← `tools/*.py` (each calls `registry.register()`) ←
`model_tools.py` (discovery + `handle_function_call()`) ← `run_agent.py`/`cli.py`.
`toolsets.py` groups tools into toolsets. Terminal backends (local, docker, ssh,
modal, daytona, singularity, vercel) live in `tools/environments/`.

**Front-ends:**
- `cli.py` — `HermesCLI` interactive terminal (Rich + prompt_toolkit, skin engine).
- `ui-tui/` — Ink/React TUI; `tui_gateway/` is its Python JSON-RPC backend.
- `gateway/` — single messaging-gateway process; one adapter per platform under
  `gateway/platforms/` (telegram, discord, slack, signal, matrix, email, …).
- `acp_adapter/` — ACP server for VS Code / Zed / JetBrains.

**Slash commands** — defined once in `hermes_cli/commands.py` (`COMMAND_REGISTRY`);
the CLI, gateway help, Telegram/Slack menus all derive from it. Add a command there,
not ad hoc.

**Extension points:**
- `skills/` — bundled procedural skills (active by default); `optional-skills/` ship
  but inactive. Skill slash commands are injected as **user messages** (preserves
  prompt caching).
- `plugins/` — memory providers, model providers, context engines, dashboards, etc.
  New memory providers must ship as standalone plugin repos (see `CONTRIBUTING.md`).
- `cron/` — built-in scheduler (`jobs.py`, `scheduler.py`).

**State & paths** — `hermes_state.py` (`SessionDB`, SQLite + FTS5 search).
All runtime paths are profile-aware: use `get_hermes_home()` from `hermes_constants.py`,
**never hardcode `~/.hermes`**. User config in `~/.hermes/config.yaml`; secrets only in
`~/.hermes/.env`.

## Conventions that bite

- Tests must never write to `~/.hermes/` — the `_isolate_hermes_home` autouse fixture
  redirects `HERMES_HOME` to a temp dir; profile tests must also mock `Path.home()`.
- Don't write change-detector tests (asserting model-catalog/config-version literals).
- Don't break prompt caching — see the "Prompt Caching Must Not Break" policy in `AGENTS.md`.
- Open files with explicit `encoding=` (ruff `PLW1514` enforces this; bare `open()` in
  text mode corrupts non-ASCII on Windows).
- See `AGENTS.md` § "Known Pitfalls" before touching display/spinner code, the gateway
  message guards, or `_last_resolved_tool_names` in `model_tools.py`.

## This checkout (`wpu` branch)

This fork adds context-curation research and tooling not in upstream hermes-agent:
the `wpu-curation/` directory (drift checks, research, ground-truth batteries) and
the desk context-curation plugins below. Keep fork-specific work scoped to those areas.
Note `agent/curator.py` itself is the upstream *skill-lifecycle* curator — a different
subsystem (see `AGENTS.md` § Curator).

### Desk context-curation plugins

Model-driven context curation: instead of an opaque summariser, the model addresses
its own context by block id and decides what to set aside. Four cooperating plugins
implement it as numbered stages (see each `plugin.yaml`):

| Plugin | Hook / type | Role |
|---|---|---|
| `plugins/desk-ids/` | `transform_tool_result` | **Stage 1** — stamps a consecutive `[bN]` block id on every tool result; this is the addressable handle the model curates by. |
| `plugins/context_engine/desk/` | `ContextEngine` | **Stage 3** — the `desk` engine. Adds the model-driven curation tools `archive` / `recall` (`shred` at Stage 5); wraps the built-in `ContextCompressor` as a raised-threshold (`0.92`) last-resort fallback. Activate with `context.engine: desk` in `config.yaml`. |
| `plugins/desk-note/` | `pre_llm_call` | **Stage 4** — injects the escalating "desk-state" note as context fills, prompting the model to curate. |
| `plugins/context-trace/` | `pre_api_request` | Diagnostic (not a stage) — detects context-window rollbacks (a request that drops messages/blocks a prior one had) and logs a loud trace to `$HERMES_HOME/context-trace/rollbacks.log`, including process uptime so restart-induced drops are distinguishable. |

`archive` moves a spent block's content to a flat plain-text archive store on the PVC,
leaving a one-line placeholder; `recall` brings it back verbatim — archiving is
reversible and never loses anything. Block ids are global/consecutive across the
session (see commit `83862d9` "global block ids + flat archive store"). The engine
only renders the means and executes the model's calls — the model decides what to curate.

## Deployment (homelab k3s)

This checkout is not just source — it is the **live deployment**. The agent runs as
"Aina" in a k3s pod that hostPath-mounts this very directory. **Editing a file here and
restarting the pod ships it.** Treat the `wpu` branch as production.

Deployment artifacts live in a **separate** directory, `/wpu/homelab/k3s/hermes/` (not
in this repo). The one rule: edit hermes/curation code **only** in `/wpu/src/hermes`
(branch `wpu`); never edit a deployed copy — everything the pod runs is a *mount* of
this checkout.

Inventory of `/wpu/homelab/k3s/hermes/`:

| Path | What it is |
|---|---|
| `hermes.yaml` | The k8s manifest — namespace `wpu-hermes`, `Deployment/hermes` (1 replica, `Recreate`, pinned to node `master`), PVs/PVCs, SSH `Service`, dashboard `Service` + Tailscale `Ingress`. |
| `Containerfile` | Image build — Playwright/Ubuntu-24.04 base + supervisor/sshd/Xvfb + deps. Image: `registry.cluster.wpu.nu/hermes-full:v18`. |
| `build-shell.sh` | `podman build` + push wrapper; carries the full image version log (v1→v18). |
| `entrypoint-v15.sh` | Pod entrypoint (v15+ mounted-checkout model): host keys, builds the web UI on startup if source changed, hands off to supervisord. (`entrypoint.sh` is the pre-v15 baked-source version, kept for reference.) |
| `supervisord.conf` | Hermes daemons: `hermes-gateway` (messaging) + `hermes-dashboard` (web UI on `:9119`). Image-baked `sshd`/`xvfb`/`signal-cli` come from `conf.d/`. |
| `conf.d/` | supervisord includes — `signal-cli.conf` (Signal adapter), `sshd.conf`, `xvfb.conf`. |
| `hermes-exec-guard` | Pre-launch readiness check; hard-fails the pod (crashloop) on missing required env/secrets. |
| `aina-tools/` | Custom CLI toolkit mounted RO at `/opt/aina-tools`, prepended to `PATH`: `nextcloud`, `sql`, `mop`, `obsidian-cli`, `wikipedia`, `lt`/`ltrs`, and `claude` (transparent shim that SSH-forwards `claude-code` to the `aina` user on master). |
| `tap/` | `aina-llm-tap` — transparent MITM logging proxy for LLM traffic; the on-the-wire ("A") capture point for the curation R/I/A divergence check. |
| `bin/`, `build-shell.sh`, `sshd_config`, `authorized_keys` | Misc deploy plumbing. SSH keys are edited here on master, not via ConfigMap. |
| `ISSUES.md` | Running list of issues found from production session reviews. |

Pod runtime shape (from `hermes.yaml`): `HERMES_HOME=/data/.hermes`, state on master-local
hostPath `/wpu/services/hermes/state` (NOT NFS — avoids NFSv4 lease hangs on WAL SQLite);
this checkout hostPath-mounted at both `/opt/hermes` (editable install) and `/wpu/src/hermes`;
LLM endpoint is the in-cluster `aina-llm` service; dashboard reachable over the tailnet.
Rebuild the image **only** when dependencies change — otherwise edit + restart the pod.
For ground truth, read `hermes.yaml` and `build-shell.sh`'s version log directly.
