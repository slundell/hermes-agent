"""desk_core — shared primitives for the desk (model-driven context tidying).

Single source of truth for the desk plugins. Before the desk cut the block-id
regex, content flattening, the get_hermes_home shim, and the watermark/budget
math were copy-pasted across four plugins (and had drifted). They live here
now; both desk plugins import this module.

Importable from both plugin loaders: it is a top-level repo module, like
``hermes_constants`` — the desk plugins already import repo-level modules, so
``import desk_core`` resolves identically from the general plugin loader and
the context-engine loader.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

logger = logging.getLogger("hermes.desk")

try:
    from hermes_constants import get_hermes_home as _get_hermes_home
except Exception:  # pragma: no cover — defensive
    def _get_hermes_home() -> Path:  # type: ignore[no-redef]
        v = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(v).resolve() if v else (Path.home() / ".hermes").resolve()


def desk_home() -> Path:
    """The hermes home directory (profile-aware)."""
    return _get_hermes_home()


# --- block ids -------------------------------------------------------------
# Canonical [bN] matcher. The capturing group is the bare id ("b37"). ONE
# definition — the pre-cut copies had drifted (desk-note captured the digits,
# the others captured "bN").
BLOCK_ID_RE = re.compile(r"^\s*\[(b\d+)\]")
_STAMPED_RE = re.compile(r"^\s*\[b\d+\]")


def content_str(msg) -> str:
    """Flatten an OpenAI-format message's content to a plain string."""
    c = msg.get("content", "") if isinstance(msg, dict) else msg
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    if isinstance(c, str):
        return c
    return str(c) if c else ""


def block_id(content) -> "str | None":
    """Return the bare block id ('b37') stamped on a content string, else None."""
    s = content if isinstance(content, str) else content_str(content)
    m = BLOCK_ID_RE.match(s)
    return m.group(1) if m else None


def is_stamped(content: str) -> bool:
    """True if a tool-result string already carries a [bN] id."""
    return bool(_STAMPED_RE.match(content or ""))


def block_ids_on_desk(messages) -> "list[int]":
    """Sorted block numbers of every [bN]-stamped tool result in messages
    (live blocks AND archived placeholders). Use for diagnostics that need a
    full view, like the context-trace rollback detector."""
    ids = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        bid = block_id(content_str(m))
        if bid:
            ids.append(int(bid[1:]))
    return sorted(set(ids))


def live_block_ids_on_desk(messages) -> "list[int]":
    """Sorted block numbers of LIVE (non-archived) tool-result blocks.

    Archived blocks remain as one-line placeholders in the message stream so
    `recall` can still find them, but they take negligible space. Listing
    placeholders in the desk-note's "ids on the desk" caused the model to
    misread them as "occupied slots" and deadlock-narrate at the forced band.
    The note now lists only live ids — placeholders are invisible there, so
    archiving visibly clears the desk."""
    ids = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        c = content_str(m)
        bid = block_id(c)
        if bid and "(archived:" not in c:
            ids.append(int(bid[1:]))
    return sorted(set(ids))


def collapse_ranges(nums) -> str:
    """Render a sorted id list as compact ranges: [1,2,3,5,6] -> 'b1–b3, b5, b6'."""
    runs: "list[list[int]]" = []
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


# --- global block-id counter ----------------------------------------------
_counter_lock = threading.Lock()


def _counter_file() -> Path:
    d = desk_home() / "desk-ids"
    d.mkdir(parents=True, exist_ok=True)
    return d / "_global.count"


def next_block_id() -> int:
    """Next number from the global monotonic counter (one sequence, all sessions)."""
    f = _counter_file()
    with _counter_lock:
        try:
            n = int(f.read_text(encoding="utf-8").strip())
        except Exception:
            n = 0
        n += 1
        try:
            f.write_text(str(n), encoding="utf-8")
        except Exception:
            # A failed persist means the next call re-reads the stale value
            # and returns a duplicate id — log it so the cause is traceable.
            logger.warning("desk block-id counter write failed; id %d may not persist", n)
    return n


# --- state directories -----------------------------------------------------
def archive_dir() -> Path:
    """Flat, session-independent archive store (block ids are globally unique)."""
    d = desk_home() / "desk-archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_dir() -> Path:
    """Per-session watermark-level files."""
    d = desk_home() / "desk-state"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- watermark / budget math ----------------------------------------------
# The desk has no LLM compaction any more, so there is no "compaction
# threshold" — the budget is the model window minus a small reserve that
# guarantees room for the model to emit tidy-tool calls at the forced level.
DESK_MODEL_MAX_CTX = int(os.environ.get("DESK_MODEL_MAX_CTX", "262144") or 262144)
# Headroom reserved above the forced ceiling. The forced band only ever emits
# tidy-tool calls (archive/shred — tens of tokens), so 4k is ample; this is
# much tighter than a general output reserve and widens normal working room.
FORCED_HEADROOM_TOKENS = int(os.environ.get("DESK_FORCED_HEADROOM", "4096") or 4096)
# Budget the watermark fractions are measured against.
EFFECTIVE_CTX = max(1, DESK_MODEL_MAX_CTX - FORCED_HEADROOM_TOKENS)

# Watermark bands, as fractions of EFFECTIVE_CTX. forced == 1.0 means the
# forced band begins exactly at the ceiling (max_ctx - 4k).
NOTICE_PCT = float(os.environ.get("DESK_NOTICE_PCT", "0.80"))
URGENT_PCT = float(os.environ.get("DESK_URGENT_PCT", "0.90"))
FORCED_PCT = float(os.environ.get("DESK_FORCED_PCT", "1.00"))
HYSTERESIS = float(os.environ.get("DESK_HYSTERESIS", "0.05"))

LEVELS = ["calm", "notice", "urgent", "forced"]
_ENTRY = {"calm": 0.0, "notice": NOTICE_PCT, "urgent": URGENT_PCT, "forced": FORCED_PCT}

# Descriptive notes — no percentages ever face the model.
NOTES = {
    "calm": None,
    "notice": "[desk: filling up — archive a spent block when you can]",
    "urgent": "[desk: nearly full — archive a spent block before continuing]",
    "forced": "[desk: full — archive spent blocks now]",
}
# Tools permitted at the forced level — context-REDUCING tidy ops only.
# `recall` is excluded: it grows the desk.
FORCED_TIDY_TOOLS = {"archive", "shred"}


def fill_fraction(tokens: float) -> float:
    """Desk fill as a fraction of the effective budget."""
    return tokens / EFFECTIVE_CTX


def level_for(frac: float, prev: str = "calm") -> str:
    """Watermark band for a fill fraction, with hysteresis on band-exit."""
    if frac >= FORCED_PCT:
        lvl = "forced"
    elif frac >= URGENT_PCT:
        lvl = "urgent"
    elif frac >= NOTICE_PCT:
        lvl = "notice"
    else:
        lvl = "calm"
    # hysteresis: don't drop below the previous band until frac falls a margin
    # below that band's entry point
    if prev in LEVELS and LEVELS.index(lvl) < LEVELS.index(prev):
        if frac >= _ENTRY[prev] - HYSTERESIS:
            lvl = prev
    return lvl


# --- loud overflow diagnostic ----------------------------------------------
def log_overflow(offending: "tuple[str, int] | None", n_messages: int) -> None:
    """Loud diagnostic when the desk overflowed before any tidy turn could run.

    Emits to the hermes error log and appends to $HERMES_HOME/desk/overflow.log.
    """
    bid, size = offending if offending else ("?", 0)
    banner = (
        "\n" + "!" * 74 + "\n"
        "!!! DESK OVERFLOW — request exceeded the context window before a\n"
        "!!! tidy turn could run. The desk cannot reduce context synchronously.\n"
        f"!!!   messages on the desk : {n_messages}\n"
        f"!!!   largest block        : {bid}  ({size:,} chars)\n"
        "!!!   the turn is being aborted; the session is preserved.\n"
        + "!" * 74
    )
    try:
        logger.error(banner)
    except Exception:
        pass
    try:
        d = desk_home() / "desk"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "overflow.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + banner + "\n")
    except Exception:
        pass
