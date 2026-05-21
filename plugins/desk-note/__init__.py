"""desk-note — inject the escalating desk-state note (Stage 4).

A `pre_llm_call` hook. Estimates how full the context ("desk") is and, when it
crosses a threshold, prepends a short descriptive note to the turn so the model
curates. No percentages face the model — only the descriptive note. Below the
first threshold there is no note at all: the model just works (restraint).

  calm    (< notice)        : no note
  notice  (notice .. urgent): "[desk: filling up — archive a spent block when you can]"
  urgent  (urgent .. forced): "[desk: nearly full — archive a spent block before continuing]"
  forced  (>= forced)       : "[desk: full — archive spent blocks now]" + the tool
                              whitelist is restricted to curation tools (archive,
                              recall) — never shred; shred is never forced.

Whenever a note fires it carries the actual set of block ids on the desk,
collapsed into ranges, e.g. "; ids on the desk: b1–b43, b45–b104]". This is
the F5 fix: the model curates only ids it can see, so it cannot extrapolate a
non-existent id (e.g. `shred b5` when the desk ends at b4).

Thresholds are env-tunable fractions of DESK_CTX_LEN:
  DESK_NOTICE_PCT (0.50)  DESK_URGENT_PCT (0.65)  DESK_FORCED_PCT (0.73)
  DESK_HYSTERESIS (0.05)  DESK_CTX_LEN (262144)
Hysteresis on band-exit avoids flapping at a boundary.
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

CTX_LEN = int(os.environ.get("DESK_CTX_LEN", "262144"))
NOTICE = float(os.environ.get("DESK_NOTICE_PCT", "0.50"))
URGENT = float(os.environ.get("DESK_URGENT_PCT", "0.65"))
FORCED = float(os.environ.get("DESK_FORCED_PCT", "0.73"))
HYST = float(os.environ.get("DESK_HYSTERESIS", "0.05"))

CURATION_ONLY = {"archive", "recall"}
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


def _on_pre_llm_call(session_id: str = "", conversation_history=None, **_):
    msgs = conversation_history or []
    est_tokens = sum(_msg_chars(m) for m in msgs) / 4.0
    frac = est_tokens / max(CTX_LEN, 1)

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
