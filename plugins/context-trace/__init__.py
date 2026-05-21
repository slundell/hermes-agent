"""context-trace — detect and loudly log context-window rollbacks.

A `pre_api_request` hook. For each session it remembers the previous outgoing
request's message count and block-id set. When a request suddenly drops a large
number of messages / block-ids that the previous request had — a context
rollback — it emits a LOUD diagnostic:

  - to the hermes error log (logger.error), and
  - to a dedicated file: $HERMES_HOME/context-trace/rollbacks.log

The diagnostic carries:
  - prev vs current message count and the dropped block-ids,
  - the on-disk session checkpoint's message count (a checkpoint >> request size
    is the smoking gun for a resume that lost context),
  - **process uptime** — a rollback within ~2 min of process start is almost
    certainly restart-induced, not a spontaneous continuity fault,
  - a stack trace of the hermes call path that built the request.

Built to diagnose the turn-to-turn rollback first seen in session
20260521_091417 (turn 2's work absent from turn 3's request).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
import traceback
from pathlib import Path

logger = logging.getLogger("hermes_plugins.context_trace")
_PROC_START = time.time()

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover
    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        v = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(v).resolve() if v else (Path.home() / ".hermes").resolve()

_BID = re.compile(r"^\s*\[(b\d+)\]")
DROP_MSGS = int(os.environ.get("CTRACE_DROP_MSGS", "8"))
DROP_IDS = int(os.environ.get("CTRACE_DROP_IDS", "5"))


def _dir() -> Path:
    d = get_hermes_home() / "context-trace"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _block_ids(msgs):
    out = set()
    for m in msgs:
        if m.get("role") != "tool":
            continue
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
        mm = _BID.match(c or "")
        if mm:
            out.add(mm.group(1))
    return out


def _checkpoint_len(session_id):
    try:
        fs = glob.glob(str(get_hermes_home() / "sessions" / f"session_*{session_id}*.json"))
        if not fs:
            return None
        d = json.load(open(max(fs, key=os.path.getmtime)))
        m = d.get("messages", d) if isinstance(d, dict) else d
        return len(m) if isinstance(m, list) else None
    except Exception:
        return None


def _check(session_id: str = "", request_messages=None, **_):
    msgs = request_messages or []
    if not session_id or not msgs:
        return
    cur_n = len(msgs)
    cur_ids = _block_ids(msgs)
    last_role = msgs[-1].get("role", "") if msgs else ""

    sf = _dir() / f"{session_id}.json"
    try:
        prev = json.loads(sf.read_text())
    except Exception:
        prev = None
    try:
        sf.write_text(json.dumps({"n": cur_n, "ids": sorted(cur_ids)}))
    except Exception:
        pass
    if not prev:
        return

    dropped = set(prev.get("ids", [])) - cur_ids
    count_drop = prev.get("n", 0) - cur_n
    if count_drop < DROP_MSGS and len(dropped) < DROP_IDS:
        return

    uptime = time.time() - _PROC_START
    ckpt = _checkpoint_len(session_id)
    last_user = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            lu = m.get("content", "")
            last_user = (lu if isinstance(lu, str) else str(lu))[:160]
            break
    likely = "RESTART-INDUCED (process just started)" if uptime < 150 else \
             "uptime normal — investigate as a genuine rollback"

    banner = (
        "\n" + "!" * 74 + "\n"
        "!!! CONTEXT ROLLBACK DETECTED  [context-trace]\n"
        f"!!!   session         : {session_id}\n"
        f"!!!   previous request: {prev.get('n')} messages\n"
        f"!!!   this request    : {cur_n} messages   (dropped {count_drop})\n"
        f"!!!   block-ids gone  : {len(dropped)}  {sorted(dropped)}\n"
        f"!!!   on-disk checkpoint: {ckpt} messages"
        f"   {'<-- checkpoint >> request: resume lost context' if ckpt and ckpt > cur_n + DROP_MSGS else ''}\n"
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
        with open(_dir() / "rollbacks.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + banner + "\n")
    except Exception:
        pass


def register(ctx) -> None:
    ctx.register_hook("pre_api_request", _check)
