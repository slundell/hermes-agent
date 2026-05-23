"""desk_preview — render an Aina session's prompts as a foldable HTML preview.

Reads request_dumps for one session from a hermes state dir and produces a
self-contained static HTML page with foldable sections:

    Session
    └── Turn 1                  [N iterations, archives, shreds]
        └── Iteration 1.1       [msgs, ~tokens, band]
            └── added messages (each foldable)
        └── Iteration 1.2
    └── Turn 2
        └── ...

The grouping is derived from the dumps themselves — there is no session-level
metadata file. A new turn boundary is detected when the user-message count in
the messages list increases between consecutive dumps.

Pure-Python, no JS, no server. Uses native <details>/<summary> for folding.
Importable as a module for in-process auto-regen, or called via the CLI in
`wpu-curation/desk-preview.py`.

Env knobs (mirror `desk_core` so the band thresholds match what the
watermark uses in the pod):
  DESK_MODEL_MAX_CTX       (default 262144) — model context window in tokens
  DESK_FORCED_HEADROOM     (default 4096)   — output reserve at forced
  DESK_NOTICE_PCT/URGENT_PCT/FORCED_PCT     — band fractions of effective
"""
from __future__ import annotations

import glob
import html as _html
import json
import os
import re
from datetime import datetime
from pathlib import Path

BID = re.compile(r"^\s*\[(b\d+)\]")

# --- thresholds (read same env as desk_core in the pod) -------------------
DESK_MODEL_MAX_CTX = int(os.environ.get("DESK_MODEL_MAX_CTX", "262144") or 262144)
FORCED_HEADROOM_TOKENS = int(os.environ.get("DESK_FORCED_HEADROOM", "4096") or 4096)
EFFECTIVE_CTX = max(1, DESK_MODEL_MAX_CTX - FORCED_HEADROOM_TOKENS)
NOTICE_PCT = float(os.environ.get("DESK_NOTICE_PCT", "0.80"))
URGENT_PCT = float(os.environ.get("DESK_URGENT_PCT", "0.90"))
FORCED_PCT = float(os.environ.get("DESK_FORCED_PCT", "1.00"))


def _band(toks: int) -> str:
    """Return the watermark band for a token estimate."""
    if toks >= int(EFFECTIVE_CTX * FORCED_PCT):
        return "forced"
    if toks >= int(EFFECTIVE_CTX * URGENT_PCT):
        return "urgent"
    if toks >= int(EFFECTIVE_CTX * NOTICE_PCT):
        return "notice"
    return "calm"


# --- helpers --------------------------------------------------------------
def _cstr(m: dict) -> str:
    c = m.get("content", "")
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return c or ""


def _block_id(s: str) -> str | None:
    m = BID.match(s or "")
    return m.group(1) if m else None


def _is_archived(s: str) -> bool:
    return "(archived:" in (s or "")


def _msg_chars(m: dict) -> int:
    n = len(_cstr(m))
    for tc in (m.get("tool_calls") or []):
        n += len(str(tc.get("function", {}).get("arguments", "") or ""))
    return n


def _detect_desk_note(content: str) -> str | None:
    if "[desk:" in (content or ""):
        m = re.search(r"\[desk:[^\]]+\]", content)
        if m:
            return m.group(0)
    return None


def _classify_tool(content: str) -> tuple[str, str]:
    c = BID.sub("", content).strip()
    try:
        o = json.loads(c)
    except Exception:
        return ("text", (c.split("\n", 1)[0][:120] or "(empty)"))
    if not isinstance(o, dict):
        return ("json", str(o)[:120])
    if "total_lines" in o:
        return ("file-read",
                f"{o.get('path') or o.get('file') or '?'} — "
                f"{o.get('total_lines')} lines, {o.get('file_size','?')}B")
    if "output" in o:
        return ("terminal", str(o.get("output", ""))[:120].replace("\n", " "))
    if "results" in o:
        n = len(o["results"]) if isinstance(o.get("results"), list) else "?"
        return ("search", f"{n} results")
    if "result" in o:
        return ("tidy-result", str(o.get("result"))[:120])
    if "error" in o and o.get("error"):
        return ("error", str(o["error"])[:120])
    return ("json", c[:120])


def _h(s) -> str:
    return _html.escape(s if isinstance(s, str) else str(s), quote=False)


# --- dump loading + turn detection ----------------------------------------
def _load_dumps(sid: str, hermes_home: str) -> list[tuple[str, dict]]:
    pattern = f"{hermes_home}/sessions/request_dump_{sid}_*.json"
    paths = sorted(glob.glob(pattern), key=os.path.getmtime)
    dumps: list[tuple[str, dict]] = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            dumps.append((p, d))
        except Exception:
            continue
    return dumps


def _msgs(dump: dict) -> list[dict]:
    body = dump.get("request", {}).get("body", {}) if isinstance(dump, dict) else {}
    return body.get("messages", []) if isinstance(body, dict) else []


def _group_turns(dumps: list[tuple[str, dict]]) -> list[list[tuple[str, dict]]]:
    """Group dumps into turns by detecting new user messages.

    A turn boundary occurs when the user-message count in the messages list
    INCREASES between consecutive dumps. Returns list of turns; each turn is
    a list of (path, dump) tuples in chronological order.
    """
    turns: list[list[tuple[str, dict]]] = []
    current: list[tuple[str, dict]] = []
    prev_user_count: int | None = None
    for path, d in dumps:
        user_count = sum(1 for m in _msgs(d) if m.get("role") == "user")
        if prev_user_count is not None and user_count > prev_user_count and current:
            turns.append(current)
            current = []
        current.append((path, d))
        prev_user_count = user_count
    if current:
        turns.append(current)
    return turns


def _tally_tidy(msgs: list[dict]) -> dict:
    """Return cumulative tidy-call counts (deduped by tool_call_id) in `msgs`."""
    seen: dict[str, str] = {}
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function", {}).get("name", "")
            if fn in ("archive", "recall", "shred"):
                tcid = tc.get("id") or ""
                if tcid and tcid not in seen:
                    seen[tcid] = fn
    out = {"archive": 0, "recall": 0, "shred": 0}
    for v in seen.values():
        out[v] = out.get(v, 0) + 1
    return out


def _new_msg_indices(prev_msgs: list[dict],
                     cur_msgs: list[dict]) -> tuple[list[int], list[int]]:
    """Return (added_indices, mutated_indices) — positions in cur_msgs that are
    new or whose content differs from prev_msgs at the same index."""
    added = list(range(len(prev_msgs), len(cur_msgs)))
    mutated: list[int] = []
    for i in range(min(len(prev_msgs), len(cur_msgs))):
        if _cstr(prev_msgs[i]) != _cstr(cur_msgs[i]):
            mutated.append(i)
    return added, mutated


# --- HTML rendering -------------------------------------------------------
_CSS = """
:root { color-scheme: light dark; }
body { font-family: ui-sans-serif, system-ui, sans-serif;
       margin: 0; padding: 1rem; line-height: 1.4;
       background: #fafafa; color: #222; max-width: 1400px; }
@media (prefers-color-scheme: dark) {
  body { background: #1a1a1a; color: #ddd; }
  pre, code { background: #2a2a2a; }
  details { background: #232323; border-color: #3a3a3a; }
  .badge { background: #2a2a2a; border: 1px solid #444; }
  .note { background: #3a2f00; border-color: #6a5300; color: #ffe49a; }
  .arch { background: #1a3a2a; border-color: #2a5a3a; color: #b0e8c8; }
  .err  { background: #3a1a1a; border-color: #6a2a2a; color: #ffb0b0; }
  .turn { background: #1f2540; border-color: #303860; }
  .iter { background: #232323; border-left-color: #557; }
  a { color: #6aafff; }
}
h1 { font-size: 1.3rem; margin: 0 0 .5rem; }
.meta { font-size: .8rem; color: #888; margin-left: auto; }
.summary-row { display: flex; gap: 1rem; flex-wrap: wrap;
               padding: .6rem .8rem; background: #fff;
               border: 1px solid #ddd; border-radius: 6px; margin: .5rem 0; }
@media (prefers-color-scheme: dark) {
  .summary-row { background: #232323; border-color: #444; }
}
.summary-row .kv { display: flex; gap: .3rem; }
.summary-row .kv b { font-variant-numeric: tabular-nums; }
details { margin: .35rem 0; padding: .4rem .6rem;
          background: #fff; border: 1px solid #ddd; border-radius: 6px; }
details > summary { cursor: pointer; outline: none;
                    display: flex; align-items: center;
                    gap: .5rem; flex-wrap: wrap; }
details[open] > summary { margin-bottom: .4rem; }
details.turn { background: #f0f4ff; border-color: #c0d0f0; padding: .5rem .8rem;
               margin: .8rem 0; }
details.iter { background: #fafafa; border-left: 3px solid #aab; padding: .35rem .6rem;
               margin: .25rem 0 .25rem 1rem; }
/* per-talker left stripe so the speaker is visible at a glance */
details.msg.talker-system    { border-left: 4px solid #f0a000; }
details.msg.talker-user      { border-left: 4px solid #2080e0; }
details.msg.talker-agent     { border-left: 4px solid #20a040; }
details.msg.talker-tool      { border-left: 4px solid #c060d0; }
.badge { display: inline-block; padding: .05rem .4rem; border-radius: 3px;
         font: .8rem ui-monospace, SFMono-Regular, Menlo, monospace;
         background: #eee; border: 1px solid #ccc; color: inherit; }
.badge.talker { font-size: .85rem; font-weight: 600; padding: .1rem .5rem;
                letter-spacing: .03em; }
.badge.talker.system { background: #fff0e0; border-color: #f0c090; color: #8a4a00; }
.badge.talker.user   { background: #e0f0ff; border-color: #90c0f0; color: #003a8a; }
.badge.talker.agent  { background: #e0ffe0; border-color: #90f090; color: #006a00; }
.badge.talker.tool   { background: #ffe0ff; border-color: #f090f0; color: #6a006a; }
.badge.bid    { background: #2a2a3a; border: 1px solid #4a4a6a; color: #b0b0ff;
                font-weight: bold; }
.badge.lvl-calm   { background: #d0f0d0; color: #003a00; border-color: #a0d0a0; }
.badge.lvl-notice { background: #fff0a0; color: #5a4a00; border-color: #d0c060; }
.badge.lvl-urgent { background: #ffd090; color: #6a3a00; border-color: #d09060; }
.badge.lvl-forced { background: #ff9090; color: #6a0000; border-color: #d05050; }
.badge.turn-tag { background: #c0d0f0; color: #003a8a; font-weight: bold; }
.note  { background: #fff8d0; border: 1px solid #d0c060; color: #5a4a00;
         padding: .3rem .5rem; border-radius: 4px; font-size: .85rem; }
.arch  { background: #d0f0d8; border: 1px solid #a0d0b0; color: #003a18;
         padding: .3rem .5rem; border-radius: 4px; font-size: .85rem; }
.err   { background: #ffd0d0; border: 1px solid #d09090; color: #6a0000;
         padding: .3rem .5rem; border-radius: 4px; font-size: .85rem; }
pre { background: #f0f0f0; border: 1px solid #ddd; border-radius: 4px;
      padding: .6rem; overflow-x: auto; white-space: pre-wrap;
      word-break: break-word; max-height: 50vh;
      font: .82rem ui-monospace, SFMono-Regular, Menlo, monospace; }
.idx  { color: #888; font-variant-numeric: tabular-nums;
        min-width: 2.5rem; display: inline-block; }
.toolcall { margin-top: .4rem; padding: .3rem .5rem;
            background: #f8f0ff; border-left: 3px solid #c090e0;
            border-radius: 3px; font-size: .85rem; }
@media (prefers-color-scheme: dark) {
  pre { background: #2a2a2a; border-color: #444; }
  .toolcall { background: #2a1a3a; border-left-color: #8a6ac0; color: #d0b0ff; }
}
.mutated { border-left: 3px solid #f0a040; padding-left: .4rem; }
.section-foot { font-size: .8rem; color: #888; margin-top: .4rem; }
"""


def _render_message(idx: int, m: dict, *, mutated: bool = False) -> str:
    role = m.get("role", "?")
    # Map technical role → user-facing talker label. "assistant" is hermes's
    # internal name; "agent" reads more naturally in the preview.
    talker = {"assistant": "agent"}.get(role, role)
    content = _cstr(m)
    chars = _msg_chars(m)
    bid = _block_id(content) if role == "tool" else None
    archived = _is_archived(content) if bid else False
    badges = [f'<span class="badge talker {_h(talker)}">{_h(talker)}</span>']
    tail = ""
    open_attr = ""

    if bid:
        badges.append(f'<span class="badge bid">{_h(bid)}</span>')
        if archived:
            tail = f'<span class="arch">{_h(content)}</span>'
        else:
            kind, summ = _classify_tool(content)
            badges.append(f'<span class="badge">{_h(kind)}</span>')
            tail = f'<span class="meta">{_h(summ)}</span>'
    elif role == "user":
        note = _detect_desk_note(content)
        if note:
            mb = re.search(r"\[desk:\s*(\w+)", note)
            if mb:
                badges.append(f'<span class="badge lvl-{_h(mb.group(1).lower())}">'
                              f'{_h(mb.group(1).lower())}</span>')
            tail = f'<span class="note">{_h(note)}</span>'
            open_attr = " open"
        else:
            preview = content.replace("\n", " ").strip()[:140]
            tail = f'<span class="meta">{_h(preview)}</span>'
    elif role == "assistant":
        tcs = m.get("tool_calls") or []
        if tcs:
            names = ", ".join(tc.get("function", {}).get("name", "?") for tc in tcs)
            badges.append(f'<span class="badge">{_h(names)}</span>')
        preview = content.replace("\n", " ").strip()[:140]
        if preview:
            tail = f'<span class="meta">{_h(preview)}</span>'
    else:
        preview = content.replace("\n", " ").strip()[:140]
        tail = f'<span class="meta">{_h(preview)}</span>'

    size_b = f'<span class="meta">{chars:,} chars</span>'
    mut_b = '<span class="badge" title="mutated since previous iteration">~</span>' if mutated else ""

    body = ""
    if not (bid and archived):
        if content:
            body += f"<pre>{_h(content)}</pre>"
        for tc in (m.get("tool_calls") or []):
            name = tc.get("function", {}).get("name", "?")
            args = tc.get("function", {}).get("arguments", "")
            tcid = tc.get("id", "?")
            body += (f'<div class="toolcall"><b>tool_call</b> '
                     f'<code>{_h(name)}</code> '
                     f'<span class="meta">id={_h(tcid)}</span>'
                     f'<pre>{_h(args)}</pre></div>')

    summary = (f'<span class="idx">#{idx}</span> '
               + " ".join(badges) + " "
               + (mut_b + " " if mut_b else "")
               + tail + " " + size_b)
    classes = f"msg talker-{talker}"
    if mutated:
        classes += " mutated"
    if body:
        return (f'<details{open_attr} class="{classes}">'
                f'<summary>{summary}</summary>{body}</details>')
    return f'<details class="{classes}"><summary>{summary}</summary></details>'


def _render_iteration(turn_idx: int, iter_idx: int, *, prev_msgs: list[dict],
                      cur_msgs: list[dict]) -> str:
    chars = sum(_msg_chars(m) for m in cur_msgs)
    toks = chars // 4
    band = _band(toks)
    archived_count = sum(1 for m in cur_msgs
                         if m.get("role") == "tool" and _is_archived(_cstr(m)))
    live_blocks = sum(1 for m in cur_msgs
                      if m.get("role") == "tool" and _block_id(_cstr(m))
                      and not _is_archived(_cstr(m)))
    added, mutated = _new_msg_indices(prev_msgs, cur_msgs)

    badges = [f'<span class="badge lvl-{band}">band: {band}</span>',
              f'<span class="badge">~{toks:,} tok</span>',
              f'<span class="badge">msgs {len(cur_msgs)}</span>',
              f'<span class="badge">live blocks {live_blocks}</span>']
    if archived_count:
        badges.append(f'<span class="badge">archived {archived_count}</span>')
    if added:
        badges.append(f'<span class="badge">+{len(added)} new</span>')
    if mutated:
        badges.append(f'<span class="badge">~{len(mutated)} mutated</span>')

    label = f"<b>iter {turn_idx}.{iter_idx}</b>"
    summary = label + " " + " ".join(badges)

    body = ""
    # Render added messages (default-open one level)
    if added:
        body += '<div class="section-foot"><b>added</b></div>'
        for i in added:
            body += _render_message(i, cur_msgs[i])
    if mutated:
        body += '<div class="section-foot"><b>mutated since last iteration</b></div>'
        for i in mutated:
            body += _render_message(i, cur_msgs[i], mutated=True)
    if not added and not mutated:
        body += ('<div class="section-foot">'
                 '(no message-level changes — same prompt as prior iteration)'
                 '</div>')

    return f'<details class="iter"><summary>{summary}</summary>{body}</details>'


def _render_turn(turn_idx: int, dumps: list[tuple[str, dict]]) -> str:
    if not dumps:
        return ""
    first_msgs = _msgs(dumps[0][1])
    last_msgs = _msgs(dumps[-1][1])
    last_chars = sum(_msg_chars(m) for m in last_msgs)
    last_toks = last_chars // 4
    last_band = _band(last_toks)

    # find this turn's user prompt — the last role=user message in the first
    # dump (the new user is at the tail of msgs at iteration start)
    user_msg = None
    for m in reversed(first_msgs):
        if m.get("role") == "user":
            user_msg = m
            break
    user_text = _cstr(user_msg) if user_msg else ""
    # strip the desk-note from the preview if any
    user_preview = re.sub(r"\s*\[desk:[^\]]*\]\s*$", "", user_text).strip()
    user_preview = user_preview.replace("\n", " ")[:200]

    # tidy ops emitted during this turn (delta from start to end)
    start_tidy = _tally_tidy(first_msgs)
    end_tidy = _tally_tidy(last_msgs)
    turn_tidy = {k: end_tidy.get(k, 0) - start_tidy.get(k, 0)
                 for k in ("archive", "recall", "shred")}

    badges = [f'<span class="badge turn-tag">turn {turn_idx}</span>',
              f'<span class="badge">{len(dumps)} iter</span>',
              f'<span class="badge lvl-{last_band}">end: {last_band}</span>',
              f'<span class="badge">end ~{last_toks:,} tok</span>']
    if any(turn_tidy.values()):
        ops = "/".join(str(turn_tidy[k]) for k in ("archive", "recall", "shred"))
        badges.append(f'<span class="badge">tidy a/r/s = {ops}</span>')
    summary = (" ".join(badges) +
               f' <span class="meta">{_h(user_preview or "(no user msg)")}</span>')

    body = ""
    prev_msgs: list[dict] = []
    for k, (_path, d) in enumerate(dumps, start=1):
        cur_msgs = _msgs(d)
        body += _render_iteration(turn_idx, k,
                                  prev_msgs=prev_msgs, cur_msgs=cur_msgs)
        prev_msgs = cur_msgs

    return f'<details class="turn"><summary>{summary}</summary>{body}</details>'


def _render_session_summary(sid: str, dumps: list[tuple[str, dict]],
                            turns: list[list[tuple[str, dict]]]) -> str:
    if not dumps:
        return ""
    last_msgs = _msgs(dumps[-1][1])
    last_chars = sum(_msg_chars(m) for m in last_msgs)
    last_toks = last_chars // 4
    band = _band(last_toks)
    tidy = _tally_tidy(last_msgs)
    live_blocks = sum(1 for m in last_msgs
                      if m.get("role") == "tool" and _block_id(_cstr(m))
                      and not _is_archived(_cstr(m)))
    archived = sum(1 for m in last_msgs
                   if m.get("role") == "tool" and _is_archived(_cstr(m)))
    kvs = [
        ("turns", str(len(turns))),
        ("iterations", str(len(dumps))),
        ("end msgs", str(len(last_msgs))),
        ("end ~tokens", f"{last_toks:,}"),
        ("end band", band),
        ("tidy archive", str(tidy.get("archive", 0))),
        ("tidy recall", str(tidy.get("recall", 0))),
        ("tidy shred", str(tidy.get("shred", 0))),
        ("live blocks", str(live_blocks)),
        ("archived blocks", str(archived)),
    ]
    return ('<div class="summary-row">' +
            "".join(f'<span class="kv"><span>{_h(k)}:</span><b>{_h(v)}</b></span>'
                    for k, v in kvs) + "</div>")


# --- public API -----------------------------------------------------------
def render_session(sid: str, *,
                   hermes_home: str = "/wpu/services/hermes/state/.hermes",
                   out_path: str | None = None,
                   out_dir: str = "/wpu/dump") -> str:
    """Render a full session as foldable HTML. Returns the output path."""
    dumps = _load_dumps(sid, hermes_home)
    if not dumps:
        raise FileNotFoundError(f"no dumps for session {sid!r} under {hermes_home}")
    turns = _group_turns(dumps)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>desk preview — {_h(sid)}</title>"
            f"<style>{_CSS}</style></head><body>"
            f"<h1>desk preview — <code>{_h(sid)}</code></h1>"
            f"<div class='meta'>rendered {_h(ts)} · {len(dumps)} iterations · "
            f"{len(turns)} turns · EFFECTIVE_CTX={EFFECTIVE_CTX:,} tok</div>")
    summary = _render_session_summary(sid, dumps, turns)
    body = "".join(_render_turn(i + 1, t) for i, t in enumerate(turns))
    h = head + summary + body + "</body></html>"
    out = out_path or f"{out_dir}/desk-preview-{sid}.html"
    out_p = Path(out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(h, encoding="utf-8")
    # Also point the "latest" alias at this render — a stable URL the user
    # can bookmark and reload across sessions. Most-recently-rendered wins.
    # Use a regular file copy (not a symlink) so external file servers /
    # NFS clients don't trip on link semantics.
    latest = out_p.parent / "desk-preview-latest.html"
    try:
        latest.write_text(h, encoding="utf-8")
    except Exception:
        pass
    return out
