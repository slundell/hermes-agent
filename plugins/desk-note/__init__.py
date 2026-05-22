"""desk-note — inject the escalating desk-state note (Stage 4).

A `pre_llm_call` hook. Measures how full the context ("desk") is and, when it
crosses a threshold, prepends a short descriptive note to the turn so the model
curates. No percentages face the model — only the descriptive note. Below the
first threshold there is no note at all: the model just works (restraint).

  calm    (< notice)        : no note
  notice  (notice .. urgent): "[desk: filling up — archive a spent block when you can]"
  urgent  (urgent .. forced): "[desk: nearly full — archive a spent block before continuing]"
  forced  (>= forced)       : "[desk: full — archive spent blocks now]" + the tool
                              whitelist is restricted to the context-reducing
                              curation ops (archive, shred); recall is excluded —
                              recalling a block only grows the desk.

Whenever a note fires it carries the actual set of block ids on the desk,
collapsed into ranges, e.g. "; ids on the desk: b1–b43, b45–b104]". This is
the F5 fix: the model curates only ids it can see, so it cannot extrapolate a
non-existent id (e.g. `shred b5` when the desk ends at b4).

Thresholds are env-tunable fractions of the effective desk budget
(DESK_EFFECTIVE_CTX = model window × compaction threshold − output reserve):
  DESK_NOTICE_PCT (0.80)  DESK_URGENT_PCT (0.90)  DESK_FORCED_PCT (0.95)
  DESK_HYSTERESIS (0.05)
The budget's three inputs are hardcoded constants — see the comments at
their definitions below.
Hysteresis on band-exit avoids flapping at a boundary.

Fill is the real prompt-token count of the last API response — a
post_api_request hook captures it. Before the first response of a process,
and right after a session reset, it falls back to a chars/4 estimate.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover
    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        v = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(v).resolve() if v else (Path.home() / ".hermes").resolve()

try:
    from hermes_cli.plugins import set_thread_tool_whitelist, clear_thread_tool_whitelist
except Exception:  # pragma: no cover
    def set_thread_tool_whitelist(*a, **k):  # type: ignore[no-redef]
        pass

    def clear_thread_tool_whitelist(*a, **k):  # type: ignore[no-redef]
        pass

# --- Effective desk budget ---------------------------------------------
# The water-line markers are fractions of DESK_EFFECTIVE_CTX, not of the raw
# model window. All three inputs below are hardcoded constants: the LLM
# endpoint (the aina-llm proxy) does not expose them (`/v1/models` carries
# no context length, `/props` is 404), and this plugin's `pre_llm_call` hook
# is not handed them either — the desk ContextEngine gets context_length via
# `update_model()`, but the hook does not. Switch these to dynamic
# resolution once the upstream `pre_llm_call` hook carries them. See
# ISSUES.md #8.
#
# Model max context window, in tokens (config `context_length`).
DESK_MODEL_MAX_CTX = 262144
# Fraction of the model window at which the wrapped compressor compacts. Past
# this point the desk regime is in last-resort territory, so the water-line
# notes escalate below it to make the model curate first.
#
# DUPLICATED: plugins/context_engine/desk defines DESK_COMPACTION_THRESHOLD
# under the same name and value (the two desk plugins share no module). Keep
# the two equal. Replace both with a dynamic calc when feasible.
DESK_COMPACTION_THRESHOLD = 0.92
# Tokens reserved for the model's reply (config `max_tokens`). Netted out so
# the desk-fill fraction is measured against space usable for input, not
# space the response will consume.
DESK_MAX_OUTPUT_TOKENS = 16384
# Usable input budget before compaction, net of the output reserve. This is
# the denominator for the desk-fill fraction.
DESK_EFFECTIVE_CTX = int(
    DESK_MODEL_MAX_CTX * DESK_COMPACTION_THRESHOLD - DESK_MAX_OUTPUT_TOKENS
)

# Water-line markers — env-tunable fractions of DESK_EFFECTIVE_CTX.
NOTICE = float(os.environ.get("DESK_NOTICE_PCT", "0.80"))
URGENT = float(os.environ.get("DESK_URGENT_PCT", "0.90"))
FORCED = float(os.environ.get("DESK_FORCED_PCT", "0.95"))
HYST = float(os.environ.get("DESK_HYSTERESIS", "0.05"))

# Tools allowed at the `forced` level — only the context-REDUCING curation
# ops. `archive` (reversible) and `shred` (irreversible) both shrink the desk;
# `recall` is excluded because it brings an archived block back and *grows*
# the desk — the opposite of what `forced` needs. `archive` stays the safe
# default, so the model is never compelled to shred — only permitted to.
CURATION_ONLY = {"archive", "shred"}
_ORDER = ["calm", "notice", "urgent", "forced"]
_ENTRY = {"calm": 0.0, "notice": NOTICE, "urgent": URGENT, "forced": FORCED}
NOTES = {
    "calm": None,
    "notice": "[desk: filling up — archive a spent block when you can]",
    "urgent": "[desk: nearly full — archive a spent block before continuing]",
    "forced": "[desk: full — archive spent blocks now]",
}


_BID = re.compile(r"^\s*\[b(\d+)\]")


def _content_str(m) -> str:
    c = m.get("content", "")
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return c or ""


def _desk_block_ids(msgs) -> "list[int]":
    """Block numbers currently on the desk, in order — every tool result that
    carries a [bN] id (live blocks and archived placeholders alike)."""
    ids = []
    for m in msgs or []:
        if m.get("role") != "tool":
            continue
        mm = _BID.match(_content_str(m))
        if mm:
            ids.append(int(mm.group(1)))
    return sorted(set(ids))


def _collapse(nums) -> str:
    """Render a sorted id list as compact ranges: [1,2,3,5,6] -> 'b1–b3, b5, b6'."""
    runs: list[list[int]] = []
    for n in nums:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    parts = []
    for a, b in runs:
        if a == b:
            parts.append(f"b{a}")
        elif b == a + 1:
            parts.append(f"b{a}, b{b}")
        else:
            parts.append(f"b{a}–b{b}")
    return ", ".join(parts)


def _msg_chars(m) -> int:
    c = m.get("content", "")
    if isinstance(c, list):
        c = " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    n = len(c or "")
    for tc in (m.get("tool_calls") or []):
        try:
            n += len(str(tc.get("function", {}).get("arguments", "")))
        except Exception:
            pass
    return n


def _level(frac: float, prev: str) -> str:
    if frac >= FORCED:
        lvl = "forced"
    elif frac >= URGENT:
        lvl = "urgent"
    elif frac >= NOTICE:
        lvl = "notice"
    else:
        lvl = "calm"
    # hysteresis: don't drop below the previous level until frac falls a margin
    # below that level's entry point
    if prev in _ORDER and _ORDER.index(lvl) < _ORDER.index(prev):
        if frac >= _ENTRY[prev] - HYST:
            lvl = prev
    return lvl


def _state_file(session_id: str) -> Path:
    d = get_hermes_home() / "desk-state"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{session_id or 'default'}.level"


# Last real prompt-token count seen from the API, keyed by session. Populated
# by the post_api_request hook (which carries the actual usage); read by the
# pre_llm_call hook so the desk-fill measure is real tokens, not a chars/4
# estimate. In-memory per gateway process — on a fresh process the first turn
# of each session falls back to the estimate until the first response lands.
_LAST_PROMPT_TOKENS: "dict[str, int]" = {}


def _on_post_api_request(usage=None, session_id: str = "", **_):
    """Capture the real prompt-token count from each API response."""
    if not isinstance(usage, dict):
        return
    try:
        pt = int(usage.get("prompt_tokens"))
    except (TypeError, ValueError):
        return
    if pt > 0:
        _LAST_PROMPT_TOKENS[session_id or "default"] = pt


def _on_session_reset(session_id: str = "", **_):
    """Drop the cached count on reset. The post-reset context is small but the
    last-seen count is stale-high; without this the first post-reset turn
    would read as 'forced' and wrongly restrict tools to curation."""
    _LAST_PROMPT_TOKENS.pop(session_id or "default", None)


def _on_pre_llm_call(session_id: str = "", conversation_history=None, **_):
    msgs = conversation_history or []
    real = _LAST_PROMPT_TOKENS.get(session_id or "default")
    if real is not None:
        tokens = float(real)
    else:
        # No real count yet (first turn after a fresh process start, or just
        # after a session reset) — fall back to a chars/4 estimate.
        tokens = sum(_msg_chars(m) for m in msgs) / 4.0
    frac = tokens / max(DESK_EFFECTIVE_CTX, 1)

    sf = _state_file(session_id)
    try:
        prev = sf.read_text().strip()
    except Exception:
        prev = "calm"
    lvl = _level(frac, prev)
    try:
        sf.write_text(lvl)
    except Exception:
        pass

    # forced level: restrict this turn's tools to curation only; otherwise lift.
    if lvl == "forced":
        set_thread_tool_whitelist(
            CURATION_ONLY,
            deny_msg_fmt="The desk is full — only curation tools are available "
                         "until you have made room. Tool '{tool_name}' is held back.")
    else:
        clear_thread_tool_whitelist()

    note = NOTES.get(lvl)
    if not note:
        return None
    # carry the real id set so the model curates only ids it can see (F5).
    ids = _desk_block_ids(msgs)
    if ids:
        note = note[:-1] + f"; ids on the desk: {_collapse(ids)}]"
    return {"context": note}


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("on_session_reset", _on_session_reset)
