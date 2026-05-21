"""desk-ids — stamp [bN] block ids onto tool results.

Stage 1 of the model-driven context-curation feature. Every tool result is
given a consecutive, stable ``[bN]`` id at creation time — the handle the model
uses to address a block for ``archive`` / ``recall``.

The counter is per-session and persisted to disk (``$HERMES_HOME/desk-ids/
<session>.count``), so ids never collide or reset across a hermes restart.
Ids are stable for the life of a block; archiving a block leaves a gap, which
is fine — the curation tool handler validates targets.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover — defensive
    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        v = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(v).resolve() if v else (Path.home() / ".hermes").resolve()

_lock = threading.Lock()
_STAMPED = re.compile(r"^\s*\[b\d+\]")


def _counter_dir() -> Path:
    d = get_hermes_home() / "desk-ids"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _next_id(session_id: str) -> int:
    """Return the next consecutive block number for *session_id* (persisted)."""
    f = _counter_dir() / f"{session_id}.count"
    with _lock:
        try:
            n = int(f.read_text().strip())
        except Exception:
            n = 0
        n += 1
        try:
            f.write_text(str(n))
        except Exception:
            pass
    return n


def _stamp(tool_name: str = "", args=None, result=None,
           session_id: str = "", tool_call_id: str = "", **_) -> "str | None":
    """transform_tool_result hook — prepend ``[bN]`` to a tool result."""
    if not isinstance(result, str) or not session_id:
        return None
    if _STAMPED.match(result):          # already stamped — never double-stamp
        return None
    return f"[b{_next_id(session_id)}] {result}"


def register(ctx) -> None:
    ctx.register_hook("transform_tool_result", _stamp)
