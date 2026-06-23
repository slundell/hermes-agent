# Hermes-side issues — Aina agent/MCP ergonomics

Forwarded + investigated 2026-06-20 (urd-session ergonomics audit). urd-side findings
(`recall(offset)`, self-documenting `inbox` actions, `whatif` op enumeration) already fixed+deployed.
Evidence: `/data/.hermes/logs/agent.log`, `request_dump_*.json`. See memory: [[std-qwen27b-rpc]],
[[mop-person-identity-cli-flailing]], [[hermes-mcp-migration]], [[skill-references-contamination-vector]].

**Investigation re-prioritized both items DOWN — neither is actively harming the current Knutby runs.**

---

## 1. `terminal` / `restish` — LARGELY ALREADY FIXED (low; latent cleanup only)

**Investigated:** the raw signal (478 `terminal` calls, 84 errors, 3 `restish: unknown flag`) is
**historical**. All 18 `restish` log lines are dated **2026-06-16 / 06-18 — none since**; last use
06-18 08:34. The 06-18 SOUL.md rewrite ([[hermes-mcp-migration]]) corrected the behavior: she's used
native `mcp_mop_*` ever since (06-19/06-20 Knutby work shows ~1 terminal error total). The 84 errors
accumulated over days and are mostly benign: `&`-backgrounding guidance, 30s/180s timeouts, upstream
mop `503`/`malformed_pid`, and `No matches found (not an error)`.

**Latent vectors that remain (cleanup, not active bugs):**
- `restish` is **still installed + mop-configured** — `/usr/local/bin/restish` 0.21.2 and
  `/data/.restish/apis.json` still points at `mop-api.mop-data-dev` (the entrypoint "retirement" that
  was supposed to empty apis.json did NOT take on the persistent `/data`). So it still half-works.
- **~33 `references/` files still teach `restish` patterns** (the [[skill-references-contamination-vector]]
  pending sweep) — loaded only via `skill_view`, so latent, not always-in-context.

**Recommended (low urgency, behavior already correct):**
- Cheap guard: neuter `/data/.restish/apis.json` (drop the mop entry) so `restish mop` fails fast
  rather than half-working.
- Larger/optional: sweep the ~33 reference files restish→native `mcp_mop_*` (the known pending sweep).
- Possibly a longer default `terminal` timeout for legit long commands.

## 2. std tool-call arg degeneration — RARE; fix needs a std roll (low)

**Investigated:** 7 `Unrepairable tool_call arguments` total over all logs (5× `mcp_urd_recall`
`skottskott…`, 1 terminal, 1 execute_code) — low frequency. std (`aina-llm-27b-rpc`) sampler is
`--temp 0.7 --top-k 20` with **no `--repeat-penalty`** (so repetition isn't penalized).

**Fix:** add `--repeat-penalty 1.1` (and optionally `--repeat-last-n 256`) to the `aina-llm-27b-rpc`
args. **Blocked on timing:** applying it rolls std, which would interrupt Aina's live run — do it when
she's idle, not mid-investigation. Low frequency → no urgency. See [[std-qwen27b-rpc]] WATCH-3.
