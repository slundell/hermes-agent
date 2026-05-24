"""desk_core — shared primitives for the desk (model-driven context tidying).

Single source of truth for the desk plugins. Before the desk cut the paper-id
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


# --- paper ids -------------------------------------------------------------
# Canonical [bN] matcher. The capturing group is the bare id ("p37"). ONE
# definition — the pre-cut copies had drifted (desk-note captured the digits,
# the others captured "bN").
PAPER_ID_RE = re.compile(r"^\s*\[(p\d+)\]")
_STAMPED_RE = re.compile(r"^\s*\[p\d+\]")


def content_str(msg) -> str:
    """Flatten an OpenAI-format message's content to a plain string."""
    c = msg.get("content", "") if isinstance(msg, dict) else msg
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    if isinstance(c, str):
        return c
    return str(c) if c else ""


def paper_id(content) -> "str | None":
    """Return the bare paper id ('p37') stamped on a content string, else None."""
    s = content if isinstance(content, str) else content_str(content)
    m = PAPER_ID_RE.match(s)
    return m.group(1) if m else None


def is_stamped(content: str) -> bool:
    """True if a tool-result string already carries a [bN] id."""
    return bool(_STAMPED_RE.match(content or ""))


def paper_ids_on_desk(messages) -> "list[int]":
    """Sorted paper numbers of every [bN]-stamped tool result in messages
    (live papers AND archived placeholders). Use for diagnostics that need a
    full view, like the context-trace rollback detector."""
    ids = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        bid = paper_id(content_str(m))
        if bid:
            ids.append(int(bid[1:]))
    return sorted(set(ids))


def live_paper_ids_on_desk(messages) -> "list[int]":
    """Sorted paper numbers of LIVE (non-archived) tool-result papers.

    Archived papers remain as one-line placeholders in the message stream so
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
        bid = paper_id(c)
        if bid and "(archived:" not in c:
            ids.append(int(bid[1:]))
    return sorted(set(ids))


def live_message_count(messages) -> int:
    """Logical message count — total list length minus archived placeholders.

    Archived papers stay in the message stream as one-line placeholders so
    `recall` can find them, but they cost only ~50 tokens each. Callers that
    gate on history length (e.g. the gateway's hard message-count safety
    valve) should compare against the *logical* conversation count, not raw
    `len(history)`. Otherwise the desk's success at trimming tokens triggers
    opaque LLM compression: list entries accumulate while tokens stay low,
    crossing a count-based threshold that was meant for sessions where
    tokens were unknown and messages were exploding.

    A message is treated as an archived placeholder iff it's a `role="tool"`
    message stamped with a `[pN]` id AND its content carries the
    `(archived:` marker written by the desk engine's archive op. Other
    occurrences of the marker (prose mentioning it, user text, etc.) are
    not stripped — placeholder detection requires both signals."""
    if not messages:
        return 0
    archived = 0
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        c = content_str(m)
        if paper_id(c) and "(archived:" in c:
            archived += 1
    return len(messages) - archived


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
            parts.append(f"p{a}")
        elif b == a + 1:
            parts.append(f"p{a}, p{b}")
        else:
            parts.append(f"p{a}–p{b}")
    return ", ".join(parts)


# --- global paper-id counter ----------------------------------------------
_counter_lock = threading.Lock()


def _counter_file() -> Path:
    d = desk_home() / "desk-ids"
    d.mkdir(parents=True, exist_ok=True)
    return d / "_global.count"


def next_paper_id() -> int:
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
            logger.warning("desk paper-id counter write failed; id %d may not persist", n)
    return n


# --- state directories -----------------------------------------------------
def archive_dir() -> Path:
    """Flat, session-independent archive store (paper ids are globally unique)."""
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
# Compressor trigger anchor — the token threshold past which the gateway
# hygiene's LLM compressor will step in (gateway/run.py, default 0.85 of
# model context). The desk's working budget and band thresholds are
# computed relative to THIS, not to model max, so the desk bands stay
# below the compressor's trigger by construction.
HYGIENE_PCT = float(os.environ.get("DESK_HYGIENE_PCT", "0.85"))
HYGIENE_THRESHOLD = max(1, int(DESK_MODEL_MAX_CTX * HYGIENE_PCT))
# Live-message-count parallel of HYGIENE_THRESHOLD — the gateway hygiene's
# hard_message_limit (compression.hygiene_hard_message_limit in config.yaml,
# default 1000). The desk bands track this axis too via fill_fraction's
# live_msgs arg: msg-count growth (lots of small tool results) escalates
# the band even when tokens stay low. Operator must keep this in sync
# with the gateway config; sync drift just means the desk's msg-band
# fires earlier or later than ideal relative to the actual valve.
HYGIENE_MSGS = int(os.environ.get("DESK_HYGIENE_MSGS", "1000") or 1000)
# Effective working budget for the desk. The bands below are fractions of
# this, so forced < hygiene by design — the model has a safety margin
# (forced..hygiene) during which it can still archive proactively before
# the compressor takes over.
EFFECTIVE_CTX = HYGIENE_THRESHOLD

# Watermark bands, as fractions of EFFECTIVE_CTX. forced = 0.95 means the
# forced band fires at 95% of the hygiene trigger — leaving ~5% (about 11k
# tokens at default settings) for the model to actually archive before the
# LLM compressor preempts. notice 0.80, urgent 0.90, forced 0.95.
NOTICE_PCT = float(os.environ.get("DESK_NOTICE_PCT", "0.80"))
URGENT_PCT = float(os.environ.get("DESK_URGENT_PCT", "0.90"))
FORCED_PCT = float(os.environ.get("DESK_FORCED_PCT", "0.95"))
HYSTERESIS = float(os.environ.get("DESK_HYSTERESIS", "0.05"))

LEVELS = ["clean", "notice", "urgent", "forced"]
_ENTRY = {"clean": 0.0, "notice": NOTICE_PCT, "urgent": URGENT_PCT, "forced": FORCED_PCT}

# Descriptive notes — no percentages ever face the model. Each note opens
# with the band name so the model has a positive signal of which band it's
# in (it can't infer that from "full" alone, and was stalling at forced
# asking the user to "raise the level" because it didn't know it was
# already AT forced — see v6 drive observation). Each band also names the
# tidy tools currently available to it.
#
# Clean gets a short note too (was None), to fix a turn/tool-call sync issue
# seen in v7: within a multi-iteration turn the band can drop (e.g.
# forced → clean after archives), but if clean emits no note the model still
# sees the old forced note in history and acts on it (e.g. tries to shred).
# Always emitting the current band's note gives every iteration's prompt a
# fresh, current state signal — no stale-memory mismatches with the gate.
NOTES = {
    "clean": "[desk: clean]",
    "notice": "[desk: notice — filling up; archive a spent paper when you can. shred is unavailable below urgent.]",
    "urgent": "[desk: urgent — nearly full; archive a spent paper before continuing. shred is now available.]",
    "forced": "[desk: forced — full; archive or shred spent papers now (only archive and shred are usable until you make room).]",
}
# Tools permitted at the forced level — context-REDUCING tidy ops only.
# `recall` is excluded: it grows the desk.
FORCED_TIDY_TOOLS = {"archive", "shred"}


def fill_fraction(tokens: float, live_msgs: int = 0) -> float:
    """Desk fill as a fraction of the effective budget.

    Returns max(token_fraction, msg_fraction):
      - token_fraction = tokens / EFFECTIVE_CTX  (HYGIENE_THRESHOLD)
      - msg_fraction   = live_msgs / HYGIENE_MSGS

    The desk bands escalate on whichever pressure axis is closer to its
    hygiene trigger. With live_msgs omitted (default 0) only the token
    axis applies — back-compat for callers that don't supply a count."""
    token_frac = tokens / EFFECTIVE_CTX
    if live_msgs <= 0:
        return token_frac
    msg_frac = live_msgs / HYGIENE_MSGS
    return token_frac if token_frac >= msg_frac else msg_frac


def level_for(frac: float, prev: str = "clean") -> str:
    """Watermark band for a fill fraction, with hysteresis on band-exit."""
    if frac >= FORCED_PCT:
        lvl = "forced"
    elif frac >= URGENT_PCT:
        lvl = "urgent"
    elif frac >= NOTICE_PCT:
        lvl = "notice"
    else:
        lvl = "clean"
    # hysteresis: don't drop below the previous band until frac falls a margin
    # below that band's entry point
    if prev in LEVELS and LEVELS.index(lvl) < LEVELS.index(prev):
        if frac >= _ENTRY[prev] - HYSTERESIS:
            lvl = prev
    return lvl


# --- token counting --------------------------------------------------------
# DESK_TOKENIZER_URL — if set, points at a tokenizer endpoint (llamacpp's
# `POST /tokenize` shape: body `{"content": text}` → `{"tokens": [int,...]}`).
# Lets archive placeholders carry an *accurate* token count instead of the
# chars/4 estimate. Set to the worker URL matching the model the agent
# actually uses (e.g. http://qwen36-27b-scan.llm.svc.cluster.wpu.nu/tokenize).
# Empty / unset → fall back to chars/4. A short timeout protects the agent
# loop from a slow tokenizer; any failure falls back to chars/4.
DESK_TOKENIZER_URL = os.environ.get("DESK_TOKENIZER_URL", "").strip()
# Optional fallback URL — tried when the primary tokenizer endpoint fails
# (5xx, timeout, network error). Lets ops swap the std backend behind the
# pass-through (e.g. 27b → 9b) without breaking token counts during the
# transition. Both endpoints should accept `{"content":text}` and return
# `{"tokens":[...]}` (llama.cpp shape) or the alt shapes token_count
# already recognises. Empty / unset disables the fallback.
DESK_TOKENIZER_FALLBACK_URL = os.environ.get("DESK_TOKENIZER_FALLBACK_URL", "").strip()
DESK_TOKENIZER_TIMEOUT = float(os.environ.get("DESK_TOKENIZER_TIMEOUT", "2.0"))


def _tokenize_via(url: str, text: str) -> "int | None":
    """POST `text` to a tokenizer endpoint and return the token count, or
    None on any failure (network, parse, unrecognised shape). Used by
    token_count to walk primary → fallback URLs without raising."""
    if not url or not text:
        return None
    try:
        import json as _json
        import urllib.request
        body = _json.dumps({"content": text}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=DESK_TOKENIZER_TIMEOUT) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
        # llamacpp: {"tokens": [int,...]} — preferred shape
        toks = data.get("tokens") or data.get("token_ids")
        if isinstance(toks, list):
            return len(toks)
        if isinstance(data.get("count"), int):
            return int(data["count"])
        if isinstance(data.get("n_tokens"), int):
            return int(data["n_tokens"])
        if isinstance(data.get("total_tokens"), int):
            return int(data["total_tokens"])
    except Exception:
        return None
    return None


def token_count(text: str) -> int:
    """Return the token count of `text`.

    Tries `DESK_TOKENIZER_URL` first; if that fails (5xx, timeout, parse),
    tries `DESK_TOKENIZER_FALLBACK_URL`; if that also fails (or neither is
    configured), falls back to a chars/4 estimate. Never raises — the
    caller can use the result as a placeholder size unconditionally."""
    s = text or ""
    chars4 = max(1, len(s) // 4) if s else 0
    if not s:
        return chars4
    n = _tokenize_via(DESK_TOKENIZER_URL, s)
    if n is not None:
        return n
    n = _tokenize_via(DESK_TOKENIZER_FALLBACK_URL, s)
    if n is not None:
        return n
    return chars4


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
        f"!!!   largest paper        : {bid}  ({size:,} chars)\n"
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
