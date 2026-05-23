"""desk — the desk hook-plugin (model-driven context tidying).

One plugin, all the desk hooks. Before the desk cut these were three separate
plugins (desk-ids, desk-note, context-trace) that shared no code and had
drifting copies of the same primitives:

  transform_tool_result : stamp a global [bN] block id on every tool result —
                          the handle the model uses to archive / recall.
  pre_llm_call          : inject the escalating watermark note; at the forced
                          level restrict tools to the context-reducing tidy
                          ops (archive, shred).
  post_api_request      : capture the real prompt-token count for the fill
                          measure.
  pre_api_request       : context-rollback diagnostic — logs a loud trace when
                          a request suddenly drops messages/blocks.
  on_session_reset      : drop the cached fill count (post-reset context is
                          small but the last-seen count is stale-high).

Shared primitives live in the top-level `desk_core` module. The ContextEngine
itself stays separate in `plugins/context_engine/desk/`: the context-engine
loader cannot register hooks, so the engine and these hooks cannot live in one
plugin — they do share `desk_core`.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import time
import traceback

import desk_core

try:
    from hermes_cli.plugins import (
        set_thread_tool_whitelist, clear_thread_tool_whitelist)
except Exception:  # pragma: no cover — defensive
    def set_thread_tool_whitelist(*a, **k):  # type: ignore[no-redef]
        pass

    def clear_thread_tool_whitelist(*a, **k):  # type: ignore[no-redef]
        pass

logger = logging.getLogger("hermes.desk")
_PROC_START = time.time()

# Real prompt-token count seen from the API, keyed by session. Fed by
# post_api_request, read by pre_llm_call so the fill measure is real tokens,
# not a chars/4 estimate. In-memory per process — on a fresh process the first
# turn of each session falls back to the estimate until the first response.
_LAST_PROMPT_TOKENS: "dict[str, int]" = {}

CTRACE_DROP_MSGS = int(os.environ.get("CTRACE_DROP_MSGS", "8"))
CTRACE_DROP_IDS = int(os.environ.get("CTRACE_DROP_IDS", "5"))


# --- block-id stamping (was: desk-ids) ------------------------------------
def _stamp(tool_name="", args=None, result=None, session_id="",
           tool_call_id="", **_):
    """transform_tool_result hook — prepend [bN] to a tool result."""
    if not isinstance(result, str) or not session_id:
        return None
    if desk_core.is_stamped(result):
        return None
    return f"[b{desk_core.next_block_id()}] {result}"


# --- fill measurement (was: desk-note) ------------------------------------
# Local to this plugin, not desk_core: it adds tool-call argument bytes to the
# content length — a fill-estimate concern specific to the watermark note.
def _msg_chars(m) -> int:
    n = len(desk_core.content_str(m))
    for tc in (m.get("tool_calls") or []):
        try:
            n += len(str(tc.get("function", {}).get("arguments", "")))
        except Exception:
            pass
    return n


def _on_post_api_request(usage=None, session_id="", **_):
    """Capture the real prompt-token count from each API response."""
    if not isinstance(usage, dict):
        return
    try:
        pt = int(usage.get("prompt_tokens"))
    except (TypeError, ValueError):
        return
    if pt > 0:
        _LAST_PROMPT_TOKENS[session_id or "default"] = pt


def _on_session_reset(session_id="", **_):
    """Drop the cached count on reset — the post-reset context is small but
    the last-seen count is stale-high; without this the first post-reset turn
    would read as 'forced' and wrongly restrict tools."""
    _LAST_PROMPT_TOKENS.pop(session_id or "default", None)


# --- watermark note (was: desk-note) --------------------------------------
def _level_file(session_id):
    return desk_core.state_dir() / f"{session_id or 'default'}.level"


def _on_pre_llm_call(session_id="", conversation_history=None, **_):
    msgs = conversation_history or []
    sid = session_id or "default"
    real = _LAST_PROMPT_TOKENS.get(sid)
    if real is not None:
        tokens = float(real)
    else:
        # No real count yet (first turn after a fresh process start, or just
        # after a session reset) — fall back to a chars/4 estimate.
        tokens = sum(_msg_chars(m) for m in msgs) / 4.0
    frac = desk_core.fill_fraction(tokens)

    lf = _level_file(sid)
    try:
        prev = lf.read_text(encoding="utf-8").strip()
    except Exception:
        prev = "calm"
    lvl = desk_core.level_for(frac, prev)
    try:
        lf.write_text(lvl, encoding="utf-8")
    except Exception:
        pass

    # forced level: restrict this turn's tools to the tidy ops; otherwise lift.
    if lvl == "forced":
        set_thread_tool_whitelist(
            desk_core.FORCED_TIDY_TOOLS,
            deny_msg_fmt="The desk is full — only desk-tidy tools (archive, "
                         "shred) are available until you have made room. Tool "
                         "'{tool_name}' is held back.")
    else:
        clear_thread_tool_whitelist()

    note = desk_core.NOTES.get(lvl)
    if not note:
        return None
    # carry the real id set so the model tidies only ids it can see.
    ids = desk_core.block_ids_on_desk(msgs)
    if ids:
        note = note[:-1] + f"; ids on the desk: {desk_core.collapse_ranges(ids)}]"
    return {"context": note}


# --- context-rollback diagnostic (was: context-trace) ---------------------
def _ctrace_dir():
    d = desk_core.desk_home() / "context-trace"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _checkpoint_len(session_id):
    try:
        fs = glob.glob(str(
            desk_core.desk_home() / "sessions" / f"session_*{session_id}*.json"))
        if not fs:
            return None
        with open(max(fs, key=os.path.getmtime), encoding="utf-8") as fh:
            d = json.load(fh)
        m = d.get("messages", d) if isinstance(d, dict) else d
        return len(m) if isinstance(m, list) else None
    except Exception:
        return None


def _on_pre_api_request(session_id="", request_messages=None, **_):
    msgs = request_messages or []
    if not session_id or not msgs:
        return
    cur_n = len(msgs)
    cur_ids = {f"b{n}" for n in desk_core.block_ids_on_desk(msgs)}
    last_role = msgs[-1].get("role", "") if msgs else ""

    ctrace = _ctrace_dir()
    sf = ctrace / f"{session_id}.json"
    try:
        prev = json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        prev = None
    try:
        sf.write_text(json.dumps({"n": cur_n, "ids": sorted(cur_ids)}),
                      encoding="utf-8")
    except Exception:
        pass
    if not prev:
        return

    dropped = set(prev.get("ids", [])) - cur_ids
    count_drop = prev.get("n", 0) - cur_n
    if count_drop < CTRACE_DROP_MSGS and len(dropped) < CTRACE_DROP_IDS:
        return

    uptime = time.time() - _PROC_START
    ckpt = _checkpoint_len(session_id)
    last_user = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            lu = m.get("content", "")
            last_user = (lu if isinstance(lu, str) else str(lu))[:160]
            break
    likely = ("RESTART-INDUCED (process just started)" if uptime < 150
              else "uptime normal — investigate as a genuine rollback")

    banner = (
        "\n" + "!" * 74 + "\n"
        "!!! CONTEXT ROLLBACK DETECTED  [desk/context-trace]\n"
        f"!!!   session         : {session_id}\n"
        f"!!!   previous request: {prev.get('n')} messages\n"
        f"!!!   this request    : {cur_n} messages   (dropped {count_drop})\n"
        f"!!!   block-ids gone  : {len(dropped)}  {sorted(dropped)}\n"
        f"!!!   on-disk checkpoint: {ckpt} messages"
        f"   {'<-- checkpoint >> request: resume lost context' if ckpt and ckpt > cur_n + CTRACE_DROP_MSGS else ''}\n"
        f"!!!   process uptime  : {uptime:.0f}s  => {likely}\n"
        f"!!!   last msg role   : {last_role}   last user msg: {last_user!r}\n"
        "!!!   --- hermes call path that built this request ---\n"
        + "".join("!!!   " + ln for ln in traceback.format_stack()[:-1])
        + "!" * 74
    )
    try:
        logger.error(banner)
    except Exception:
        pass
    try:
        with open(ctrace / "rollbacks.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + banner + "\n")
    except Exception:
        pass


# --- shred gating ---------------------------------------------------------
# shred is irreversible. At calm and notice the desk is comfortably below
# its budget — archive (reversible) is enough, and gating shred at those
# bands prevents the model from defeating recall with an archive-then-shred
# pattern just to clear placeholder lines. shred remains available at
# urgent and forced where the extra reduction it offers genuinely matters.
_SHRED_BANDS = {"urgent", "forced"}
_SHRED_BLOCK_MSG = (
    "shred is unavailable at the {lvl} level. Use archive instead — it is "
    "reversible (the block can be brought back with recall). shred becomes "
    "available at urgent and forced, where its irreversibility is justified "
    "by the desk pressure.")


def _on_pre_tool_call(tool_name="", args=None, session_id="",
                      task_id="", tool_call_id="", **_):
    """pre_tool_call hook — gate `shred` by the current watermark band.

    Returns a {action: block, message} dict to refuse the call; returns None
    (or anything non-dict) to let it through. The current band is the level
    `_on_pre_llm_call` wrote to $HERMES_HOME/desk-state/<session>.level just
    before this turn.
    """
    if tool_name != "shred":
        return None
    sid = session_id or "default"
    try:
        lvl = _level_file(sid).read_text(encoding="utf-8").strip() or "calm"
    except Exception:
        lvl = "calm"
    if lvl in _SHRED_BANDS:
        return None
    return {"action": "block", "message": _SHRED_BLOCK_MSG.format(lvl=lvl)}


def register(ctx) -> None:
    ctx.register_hook("transform_tool_result", _stamp)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("pre_api_request", _on_pre_api_request)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("on_session_reset", _on_session_reset)
