# WPU local patches (`wpu-lcm-plugin` vs upstream)

Ledger of what our fork carries on top of upstream `NousResearch/hermes-agent`, for the
**release-sync** (we track release TAGS, not main HEAD).

- **Current base:** `v2026.6.5` (`git describe` = `v2026.6.5-203`)
- **Sync target:** `v2026.6.19` (latest tag)
- **Disposition legend:** `CARRY` = re-apply onto target · `REWORK` = carry but expect conflicts / re-validate · `DROP-up` = already upstreamed · `DROP-x` = cancels out, skip
- **Decision column** = filled during the walk-through.

> Snapshot commit `2817e3a48` bundles several logical patches (P2, P8, P9, P10, P13, P17, P25); they share that SHA.

## Decisions summary (walk-through 2026-06-23)

| disposition | items |
|---|---|
| **CARRY (rework onto v2026.6.19)** | P2 (compress no-op), P3+P4 (bg-review prefix parity), P6 (replay interrupt), P7 (budget 32, drop flash-routing), P8 (guards+tests; drop `review_replay_draft.py`), P9+P10 (prefill error class+recovery), P12 (Qwen XML scrub), P13 (MCP lazy-install), P14 (document_read), P15 (multimodal_analyze), P18+P19 (kanban), P20+P21 (skill_manager), P22+P23 (always_preload), P24 (lazy_deps PEP668), ISSUES.md |
| **DROP — LCM is upstream-wired (≈config only)** | P1 (verify zero `agent_init` edits on v2026.6.19) |
| **DROP — already upstreamed** | P11 (#16587), P16 (use_llm_processing), D1 (interrupt-retry), D2 (#42314) |
| **DROP — cancel/dead/churn** | D3 (vendored-LCM squash), D4 (max_tokens floor+revert), P17 (holo pruner — memory toolset disabled), `review_replay_draft.py` (unused draft), package-lock.json (npm churn) |
| **SHELVE** | P5 (interactive preemption — would block 2-slot concurrency; revive only if foreground latency suffers) |
| **DROP from fork → maybe deploy** | P25 (`claude_oai_wrapper.py`) |

**Headline:** LCM divergence collapses to ~config-only (P1); 4 patches already upstreamed; the real carry is the **bg-review/replay** + **error-recovery** + **MCP/tools** sets, all needing a re-fit (not a clean cherry-pick) onto v2026.6.19's hot core files. **2-slot lens:** the whole bg-review group was built for single-slot std — re-validate prefix-sharing (host-RAM bridge) + the dropped preemption in the 2-slot config.

---

## 1. LCM integration — *the reason this fork exists*

### P1 — Wire `hermes-lcm` plugin as the context engine  · `REWORK`
- **Commits:** `87eb65e09` (forward lcm_config), `9b7b37110` (align `update_model()` api_mode), net of `1952ddb0a` (drop vendored, wire plugin) − `4ff1f504c` (add vendored, → DROP-x)
- **Files:** `agent/agent_init.py`, `agent/context_engine.py`, `agent/model_metadata.py`, `hermes_cli/config.py`, `hermes_cli/models.py`
- **What it does:** registers the standalone `stephenschoettler/hermes-lcm` plugin as the `lcm` context engine, forwards `lcm_config` from `config.yaml` through `agent_init`, and matches the engine to the context-engine ABC (`api_mode` kwarg). Replaces the earlier *vendored* LCM (upstream PR #6464).
- **Sync notes:** highest-conflict area — `agent_init.py`/`context_engine.py` moved a lot upstream. The vendored-LCM add/drop cancels (don't re-pick the `plugins/context_engine/lcm/**` tree); keep only the plugin-wiring deltas.
- **Decision (walk-through):** **DROP almost entirely — LCM is wired by UPSTREAM, not us.** v2026.6.19 already supports standalone context-engine plugins (`register_context_engine` in `plugins.py:502`; engine docstring "plugin register() or default … replace via the plugin system"). The running `hermes-lcm` plugin self-registers; we only need **config** (`plugins.enabled: [hermes-lcm]` + `engine: lcm`) + **deploy** (plugin checkout) + **`LCM_*` env**. Therefore: P1a = no-op (no `lcm:` block; plugin reads env) → drop. P1b = modifies the deleted vendored tree → drop. P1c = its only value was *removing* vendored LCM, which we never add when starting from v2026.6.19. **Action on sync:** deploy v2026.6.19 + hermes-lcm + `engine: lcm`, verify LCM loads with **zero `agent_init` edits**; add a minimal engine-selection bridge ONLY if that empirical test fails. Expected surviving LCM code-divergence: **~none**.

### P2 — `conversation_compression`: handle LCM "leaf no-op"  · `CARRY`
- **Commit:** `2817e3a48` · **Files:** `agent/conversation_compression.py` (+ `tests/agent/test_compression_concurrent_fork.py`)
- **What it does:** when `compress()` returns input unchanged (backlog below leaf-chunk threshold) it's *not* a session boundary — don't rotate session/rebuild prompt, because that forces a non-byte-identical prefix re-derivation = cold reprefill. Prefix-cache correctness.
- **Decision (walk-through):** **CARRY (rework).** High value (prefix-cache, matches the prefill-first priority). Re-fit onto v2026.6.19's `compress_context`; first verify v2026.6.19 doesn't already short-circuit the no-op. **Upstream candidate** (generic "don't rotate on no-op").

---

## 2. Background review + replay — *big local feature*

### P3 — bg-review inherits parent `tools[]` + parent model  · `CARRY`
- **Commit:** `78d53c167` · **Files:** `agent/background_review.py`
- **What it does:** bg-review reuses the main conversation's exact tools[] and model → shares its warm KV prefix (prefix-cache parity) instead of paying a cold prefill.
- **Decision (walk-through): CARRY (rework).** Still the prerequisite for *any* prefix sharing (byte-identical body → shareable prefix). **2-slot caveat (user):** the realized win is now mostly the **cross-slot host-RAM ctx-checkpoint bridge** (restore on slot-2), not a free in-slot hit, since bg-review may land on the other slot when main is busy. [[std-mtp-alternating-cache]] confirms the bridge works for shared content (main↔bg-review). **TODO at rework/test:** (a) measure bg-review prefix-hit % in the live 2-slot config (agent.log `cache=X/Y`); (b) consider pinning bg-review to main's `id_slot` for a guaranteed in-slot hit (trades concurrency for cache).

### P4 — byte-identical replay (reuse foreground warm prefix)  · `CARRY`
- **Commit:** `30165d1c6` · **Files:** `agent/background_review.py`, `run_agent.py`
- **What it does:** replays the foreground's warm prefix verbatim rather than re-deriving via LCM (avoids cold reprefill on review).
- **Decision (walk-through): CARRY (rework).** P3's deeper half (full-payload byte-identity); same 2-slot caveat. Verify the helper APIs on v2026.6.19. Long-term: belongs in the LCM plugin (preserve byte-identity in on_session_start).

### P5 — interactive-priority preemption  · `CARRY`
- **Commit:** `e5675b5f5` · **Files:** `agent/background_review.py`, `agent/chat_completion_helpers.py`, `agent/interactive_preemption.py` (new)
- **What it does:** kills in-flight bg-review calls the moment a live user turn starts (user latency > background work).
- **Decision (walk-through): SHELVE.** Built for single-slot std; with 2 slots it would block the concurrency we now have (kills bg-review on every foreground turn). Don't carry. Revive only if foreground latency under 2-slot GPU-compute-sharing proves a real problem. (Mechanism preserved in git history / archive.)

### P6 — don't mislabel preemption as failure  · `CARRY`
- **Commit:** `272cd9b73` · **Files:** `agent/background_review.py`
- **What it does:** a preempted/interrupted bg-review isn't an error → don't log failure or trigger fallback on interrupt.
- **Decision (walk-through): CARRY (rework).** Refines P4's replay path; checks `_interrupt_requested` (any interrupt source — user/gateway/watchdog), NOT P5-specific. Valid with P5 shelved.

### P7 — bg-review iteration budget 16 → 32 (env-tunable)  · `CARRY`
- **Commit:** `e7effb763` · **Files:** `agent/background_review.py`
- **Decision (walk-through): CARRY (rework) — budget bump only.** Take `max_iterations=32` env-tunable; DROP the `HERMES_REVIEW_MODEL` flash-routing (superseded by P3; flash decommissioned).

### P8 — bg-review/replay snapshot deltas + draft + guards  · `CARRY`
- **Commit:** `2817e3a48` · **Files:** `agent/background_review.py` (+152), `agent/review_replay_draft.py` (new), `tests/run_agent/test_background_review_replay_guard.py`, `test_review_prompt_case_material_guard.py`
- **What it does:** latest bg-review/replay working-tree changes + the replay-draft module + guard tests (replay correctness, case-material leak guard).
- **Decision:**

---

## 3. Error recovery / robustness

### P9 — `error_classifier`: new deterministic error classes  · `CARRY`
- **Commit:** `2817e3a48` · **Files:** `agent/error_classifier.py` (+ `tests/agent/test_error_classifier.py`)
- **What it does:** classifies `assistant_prefill_unsupported` ... → routes to the right recovery.
- **Decision (walk-through): CARRY (rework).** ik_llama/Qwen-specific error phrasings; paired with P10. Check v2026.6.19 for overlapping classes.

### P10 — `conversation_loop`: assistant-prefill recovery  · `CARRY`
- **Commit:** `2817e3a48` · **Files:** `agent/conversation_loop.py` (+ `tests/run_agent/test_assistant_prefill_recovery.py`)
- **What it does:** strips the partial assistant message + retries when a thinking-template provider refuses a prefill.
- **Decision (walk-through): CARRY (rework).** Recovery half of P9; re-fit into v2026.6.19's loop.

### P11 — `fix(auxiliary)`: retry transient transport once before fallback  · `CARRY`
- **Commit:** `baab1216e` (#16587) · **Files:** `agent/auxiliary_client.py` (+ test)
- **Decision (walk-through): DROP — already upstreamed.** v2026.6.19 has the same fix (PR #16587, auxiliary_client.py:5296/5788, refactored 'unified home'). `git cherry` missed it (different patch-id). Redundant.

### P12 — scrub orphan tool-call XML from `extract_reasoning`  · `CARRY`
- **Commit:** `7d0ea242b` · **Files:** `agent/agent_runtime_helpers.py`
- **What it does:** strips leaked `<tool_call>`/`<function=>` XML out of Qwen reasoning output.
- **Decision (walk-through): CARRY (rework).** NOT in v2026.6.19 (extract_reasoning has no scrub). Qwen-specific, relevant to our std backend.

---

## 4. MCP enablement

### P13 — lazy-install the MCP client SDK when configured  · `CARRY`
- **Commit:** `2817e3a48` · **Files:** `tools/mcp_tool.py`, `tools/lazy_deps.py`
- **What it does:** `ensure("tool.mcp")` installs the `mcp` SDK when `mcp_servers` configured.
- **Decision (walk-through): CARRY (rework).** v2026.6.19 only prints 'pip install hermes-agent[mcp]' + degrades (no auto-install) → our lazy-install still needed for the PVC venv. Add `tool.mcp` to v2026.6.19's lazy_deps + mcp_tool.

---

## 5. New tools / plugins

### P14 — `document_read` (Tika PDF/Office/RTF/ODF)  · `CARRY`
- **Commit:** `f125843e7` · **Files:** `plugins/document_read/**`
- **Decision (walk-through): CARRY.** Ours (not in v2026.6.19); clean new-dir add; verify Tika dep. Upstream has doc *skills* but no doc-read *tool* — coexist.

### P15 — `multimodal_analyze` (audio + video frames)  · `CARRY`
- **Commit:** `de24ed5bf` · **Files:** `plugins/multimodal_analyze/**`
- **Decision (walk-through): CARRY.** Ours (not in v2026.6.19); clean new-dir add.

### P16 — `web_extract`: full-page LLM summary default  · `CARRY`
- **Commit:** `4f143fc8b` · **Files:** `tools/web_tools.py`
- **What it does:** defaults web_extract to a full-page LLM summary; exposes `use_llm_processing`.
- **Decision (walk-through): DROP — already upstreamed.** v2026.6.19 web_tools.py has `use_llm_processing` (default True, line 897) + LLM path (1060). Verify upstream does full-page coverage (not truncate-then-summarize); carry only that delta if it differs.

### P17 — holographic memory `pruner.py`  · `CARRY`
- **Commit:** `2817e3a48` · **Files:** `plugins/memory/holographic/pruner.py` (new)
- **Decision (walk-through): DROP.** `memory` toolset is disabled in our config; likely the upstream PR #30199 pruner with the missing-`import re` bug. Dead + buggy → not carried.

---

## 6. Kanban

### P18 — `kanban.default_model` config fallback  · `CARRY`
- **Commit:** `d02c7e000` · **Files:** `hermes_cli/kanban_db.py`
- **Decision (walk-through): CARRY.** Ours (v2026.6.19 has default_assignee, not default_model). Configurable kanban worker model; flash example moot.

### P19 — kanban `-m` placement fix (subparser overwrite)  · `CARRY`
- **Commit:** `5c9eea3e6` · **Files:** `hermes_cli/kanban_db.py`
- **Decision (walk-through): CARRY (rework).** Verify the `-m`-before-`chat` subparser-overwrite bug still exists in v2026.6.19's worker spawn.

---

## 7. Skill manager

### P20 — detect unquoted-colon YAML errors + docs  · `CARRY`
- **Commit:** `35d5dced3` · ...
- **Decision (walk-through): CARRY.** Ours; skill-authoring UX.

### P21 — alias/anchor frontmatter hints + action cheatsheet  · `CARRY`
- **Commit:** `2b8f455e3` · ...
- **Decision (walk-through): CARRY.** Ours; skill-authoring UX.

---

## 8. Gateway / skills

### P22 — `skills.always_preload` (session-start injection)  · `CARRY`
- **Commit:** `7982d1a9d` · ...
- **Decision (walk-through): CARRY (rework).** Ours; `skills.always_preload` — how Aina preloads research-methodology/learning-mindset. Important.

### P23 — fire always_preload once per gateway lifetime  · `CARRY`
- **Commit:** `ca05f3df8` · ...
- **Decision (walk-through): CARRY (rework).** Ours; fire always_preload once/lifetime.

---

## 9. Ops / misc

### P24 — `lazy_deps`: pip `--break-system-packages` on PEP 668  · `CARRY`
- **Commit:** `90cef0aee` · ...
- **Decision (walk-through): CARRY.** Ours; PVC-venv pip PEP668 fix.

### P25 — `claude_oai_wrapper.py` (dormant OpenAI→Anthropic shim)  · `REVIEW`
- **Commit:** `2817e3a48` · **Files:** `claude_oai_wrapper.py`
- **What it does:** this session's fallback shim (Aina via Claude during std outages). Currently dormant. Keep in-tree or move to deploy?
- **Decision (walk-through): DROP** from the fork (standalone shim, not hermes core). If needed for a future outage it can live under `/wpu/homelab/k3s/hermes` (deploy), not the code repo.

### P26 — `ISSUES.md` + `package-lock.json`  · `REVIEW`
- **Commit:** `2817e3a48`
- **What it does:** notes file + a 229-line package-lock change (all deletions of electron desktop-build deps = npm-prune churn).
- **Decision (walk-through): `ISSUES.md` CARRY (keep locally); `package-lock.json` DROP (npm churn; sync uses v2026.6.19's lock).**

---

## DROP (do not carry into the sync)

- **D1** `e4c8306dd` fix(agent): don't retry interrupt-induced transport errors — **DROP-up** (upstreamed)
- **D2** `edf758c1e` fix(stream): #42314 truncation — **DROP-up** (upstreamed)
- **D3** `4ff1f504c` LCM: squash-merge vendored PR #6464 — **DROP-x** (we use the plugin; cancels with `1952ddb0a`)
- **D4** `f73436bc9` auxiliary max_tokens floor + `c74f2adef` its Revert — **DROP-x** (cancel pair)

---

## Sync progress (2026-06-23) — `sync/v2026.6.19` (worktree `/wpu/src/hermes-sync`)

Live `wpu-lcm-plugin` untouched (pod-safe); sync done in a worktree, will ff `wpu-lcm-plugin` only when green.

**Applied CLEAN (9):** P14 document_read, P24 lazy_deps PEP668, P7 bg-review budget *(trim flash-routing line)*,
P20+P21 skill_manager, P22+P23 gateway always_preload, P12 agent XML-scrub, P15 multimodal_analyze.

**Remaining — conflict resolution (hot files):**
- `hermes_cli/kanban_db.py` — P18 (`d02c7e000`), P19 (`5c9eea3e6`)
- `agent/background_review.py` — P3 (`78d53c167`), P4 (`30165d1c6`), P6 (`272cd9b73`) + snapshot (`2817e3a48`); interdependent, resolve in order
- `agent/conversation_loop.py`, `conversation_compression.py` (P2), `error_classifier.py` (P9), `mcp_tool.py`+`lazy_deps.py` (P13), `conversation_loop.py` (P10) — from snapshot `2817e3a48` (when re-applying the snapshot, DROP its dead files: review_replay_draft.py / holo pruner / oai-wrapper / package-lock.json)

**Then:** (1) **P1 empirical** — deploy v2026.6.19 + hermes-lcm plugin + `engine: lcm`, confirm LCM loads with ZERO agent_init edits; (2) drop the moot flash-routing line in P7; (3) update the bg-review headroom guard 131072→262144; (4) **test** (LCM, bg-review, MCP); (5) ff `wpu-lcm-plugin` → `sync/v2026.6.19` and rollout.
