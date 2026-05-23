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

PID = re.compile(r"^\s*\[(p\d+)\]")

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
    return "clean"


# --- helpers --------------------------------------------------------------
def _cstr(m: dict) -> str:
    c = m.get("content", "")
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return c or ""


def _paper_id(s: str) -> str | None:
    m = PID.match(s or "")
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
    c = PID.sub("", content).strip()
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
:root {
  color-scheme: light dark;
  --ink: #222; --muted: #888; --rule: #e6e6e6;
  --bg: #fafafa; --card: #fff;
  --t-system: #c97a00; --t-user: #1e6fc9; --t-agent: #189a3e; --t-tool: #a040b8;
  --b-clean: #2e8b57; --b-notice: #b8860b; --b-urgent: #c75a00; --b-forced: #c83838;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #ddd; --muted: #8a8a8a; --rule: #333;
    --bg: #1a1a1a; --card: #222;
    --t-system: #f0a040; --t-user: #5aa9ff; --t-agent: #4cce6a; --t-tool: #c885d6;
    --b-clean: #4caf50; --b-notice: #d0a020; --b-urgent: #e89030; --b-forced: #e85050;
  }
}
* { box-sizing: border-box; }
body { font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
       margin: 0; padding: 1.5rem 1.25rem 4rem; line-height: 1.5;
       background: var(--bg); color: var(--ink);
       max-width: 1200px; margin-inline: auto;
       font-size: 14.5px; }
h1 { font-size: 1.1rem; margin: 0; font-weight: 600;
     color: var(--ink); letter-spacing: -.005em; }
h1 code { font: inherit; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
          color: var(--muted); font-weight: 500; }
.subtitle { color: var(--muted); font-size: .85rem; margin: .2rem 0 1.5rem; }
.session-stats { display: flex; gap: 1.5rem; flex-wrap: wrap;
                 padding: .8rem 0 1.2rem;
                 border-bottom: 1px solid var(--rule);
                 margin-bottom: 1.5rem; font-size: .9rem; }
.session-stats .kv { display: flex; gap: .35rem; align-items: baseline; }
.session-stats .kv .k { color: var(--muted); font-size: .8rem; }
.session-stats .kv .v { font-weight: 600; font-variant-numeric: tabular-nums; }
details { background: transparent; border: none; padding: 0; margin: 0; }
details > summary { cursor: pointer; outline: none; list-style: none;
                    display: flex; align-items: baseline;
                    gap: .6rem; flex-wrap: wrap; }
details > summary::-webkit-details-marker { display: none; }
details > summary::before { content: "▸"; color: var(--muted);
                            font-size: .7rem; transform: translateY(-1px); }
details[open] > summary::before { content: "▾"; }
/* Turn = the strong heading. */
details.turn { margin: 1.2rem 0 0; padding: .5rem 0 .3rem;
               border-top: 1px solid var(--rule); }
details.turn:first-of-type { border-top: none; margin-top: 0; }
details.turn > summary { font-size: 1rem; }
.turn-num { font-weight: 700; color: var(--ink); letter-spacing: -.005em; }
.turn-prompt { flex: 1 1 18rem; color: var(--ink); font-weight: 500;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.turn-meta { margin-left: auto; color: var(--muted); font-size: .8rem;
             font-variant-numeric: tabular-nums; }
/* Iteration = compact subheading. */
details.iter { margin: .35rem 0 .35rem 1.1rem; padding: .15rem 0;
               font-size: .9rem; }
details.iter > summary { color: var(--muted); }
.iter-num { font-weight: 600; color: var(--ink); font-variant-numeric: tabular-nums;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.iter-meta { color: var(--muted); font-size: .82rem;
             font-variant-numeric: tabular-nums; }
/* Message rows = subtle, with a left color stripe by talker. */
details.msg { margin: .2rem 0 .2rem 2rem; padding: .15rem .5rem;
              border-left: 3px solid var(--rule); }
details.msg.talker-system { border-left-color: var(--t-system); }
details.msg.talker-user   { border-left-color: var(--t-user); }
details.msg.talker-agent  { border-left-color: var(--t-agent); }
details.msg.talker-tool   { border-left-color: var(--t-tool); }
details.msg > summary { font-size: .88rem; }
.talker { font-weight: 600; font-size: .82rem;
          text-transform: lowercase; letter-spacing: .02em;
          font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.talker.system { color: var(--t-system); }
.talker.user   { color: var(--t-user); }
.talker.agent  { color: var(--t-agent); }
.talker.tool   { color: var(--t-tool); }
.bid { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
       color: var(--muted); font-size: .8rem; }
.kind { color: var(--muted); font-size: .8rem;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.preview { color: var(--muted); flex: 1 1 12rem;
           overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.size { color: var(--muted); font-size: .75rem; margin-left: auto;
        font-variant-numeric: tabular-nums; }
.idx { color: var(--muted); font-size: .75rem; min-width: 2.2rem;
       font-variant-numeric: tabular-nums;
       font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
/* Band pill — small coloured chip used in iter/turn headers. */
.band { display: inline-paper; padding: .05rem .45rem; border-radius: 10px;
        font-size: .72rem; font-weight: 600; text-transform: lowercase;
        letter-spacing: .03em; color: #fff; }
.band.clean   { background: var(--b-clean); }
.band.notice { background: var(--b-notice); }
.band.urgent { background: var(--b-urgent); }
.band.forced { background: var(--b-forced); }
/* Inline note (desk-note in user context) — subtle yellow tint. */
.note { color: var(--ink); background: rgba(200,150,0,.10);
        padding: .1rem .4rem; border-radius: 3px; font-size: .82rem;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.arch { color: var(--muted); font-size: .8rem;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.mut  { color: var(--b-urgent); font-size: .75rem; font-weight: 600; }
/* Expanded content. */
pre  { background: rgba(0,0,0,.04); border: none; border-radius: 4px;
       padding: .6rem .7rem; overflow-x: auto; white-space: pre-wrap;
       word-break: break-word; max-height: 55vh;
       font: 12.5px ui-monospace, SFMono-Regular, Menlo, monospace;
       margin: .5rem 0; line-height: 1.5; }
@media (prefers-color-scheme: dark) { pre { background: rgba(255,255,255,.04); } }
.toolcall { margin: .35rem 0 .15rem; padding: .25rem .55rem;
            border-left: 2px solid var(--t-agent);
            font-size: .82rem; color: var(--muted); }
.toolcall code { color: var(--ink);
                 font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.section-foot { font-size: .75rem; color: var(--muted); margin: .4rem 0 .2rem;
                text-transform: uppercase; letter-spacing: .08em; font-weight: 500; }
/* Verbatim request/response JSON boxes — one per iteration, foldable. */
details.json-box { margin: .35rem 0 .35rem 2rem; padding: .15rem .5rem;
                   border-left: 3px solid var(--rule); }
details.json-box > summary { font-size: .85rem; color: var(--muted); }
.json-label { font-weight: 600; color: var(--ink);
              font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
pre.json { font-size: 12px; max-height: 70vh; line-height: 1.45;
           background: rgba(0,0,0,.05); }
@media (prefers-color-scheme: dark) { pre.json { background: rgba(255,255,255,.05); } }
"""


def _render_message(idx: int, m: dict, *, mutated: bool = False) -> str:
    role = m.get("role", "?")
    talker = {"assistant": "agent"}.get(role, role)
    content = _cstr(m)
    chars = _msg_chars(m)
    bid = _paper_id(content) if role == "tool" else None
    archived = _is_archived(content) if bid else False
    open_attr = ""

    parts: list[str] = [f'<span class="idx">#{idx}</span>',
                        f'<span class="talker {_h(talker)}">{_h(talker)}</span>']
    if mutated:
        parts.append('<span class="mut" title="mutated since prior iteration">~</span>')

    if bid:
        parts.append(f'<span class="bid">{_h(bid)}</span>')
        if archived:
            parts.append(f'<span class="arch preview">{_h(content)}</span>')
        else:
            kind, summ = _classify_tool(content)
            parts.append(f'<span class="kind">{_h(kind)}</span>')
            parts.append(f'<span class="preview">{_h(summ)}</span>')
    elif role == "user":
        note = _detect_desk_note(content)
        if note:
            mb = re.search(r"\[desk:\s*(\w+)", note)
            if mb:
                parts.append(f'<span class="band {_h(mb.group(1).lower())}">'
                             f'{_h(mb.group(1).lower())}</span>')
            parts.append(f'<span class="note">{_h(note)}</span>')
            open_attr = " open"
        else:
            preview = content.replace("\n", " ").strip()[:200]
            parts.append(f'<span class="preview">{_h(preview)}</span>')
    elif role == "assistant":
        tcs = m.get("tool_calls") or []
        if tcs:
            names = ", ".join(tc.get("function", {}).get("name", "?") for tc in tcs)
            parts.append(f'<span class="kind">→ {_h(names)}</span>')
        preview = content.replace("\n", " ").strip()[:200]
        if preview:
            parts.append(f'<span class="preview">{_h(preview)}</span>')
    else:
        preview = content.replace("\n", " ").strip()[:200]
        parts.append(f'<span class="preview">{_h(preview)}</span>')

    parts.append(f'<span class="size">{chars:,}c</span>')

    body = ""
    if not (bid and archived):
        if content:
            body += f"<pre>{_h(content)}</pre>"
        for tc in (m.get("tool_calls") or []):
            name = tc.get("function", {}).get("name", "?")
            args = tc.get("function", {}).get("arguments", "")
            tcid = tc.get("id", "?")
            body += (f'<div class="toolcall"><code>{_h(name)}</code> '
                     f'<span class="size">id={_h(tcid)}</span>'
                     f'<pre>{_h(args)}</pre></div>')

    summary = " ".join(parts)
    cls = f"msg talker-{talker}"
    if body:
        return (f'<details{open_attr} class="{cls}">'
                f'<summary>{summary}</summary>{body}</details>')
    return f'<details class="{cls}"><summary>{summary}</summary></details>'


def _humanise_newlines(s: str) -> str:
    """Make verbatim JSON readable. Sacrifices round-trippability for
    legibility — these boxes are for reading prompts, not re-parsing.

    Cheats applied in order:
      1. \\n / \\t  → real newline / tab        (multi-line content)
      2. \\"        → "                          (internal-quote escapes)
      3. \\\\       → \\                         (collapse double backslashes)

    Reads cleanly through nested-escape disasters like
    `{"content": "{\\\"key\\\": \\\"value\\\\nmore\\\"}"}` — those are
    common when tool results return JSON-stringified payloads that the
    outer message-body wrapper escapes a second time. After this pass
    you see:
      {"content": "{"key": "value
      more"}"}
    """
    s = s.replace("\\n", "\n").replace("\\t", "\t")
    s = s.replace('\\"', '"')
    s = s.replace("\\\\", "\\")
    return s


def _request_body_json(dump_path: str) -> str:
    """Return the request body as prettified JSON. Reads the dump file and
    extracts the `body` payload (what hermes actually sent to the LLM)."""
    try:
        d = json.loads(Path(dump_path).read_text(encoding="utf-8"))
    except Exception as e:
        return f"(failed to read {dump_path}: {e})"
    body = d.get("request", {}).get("body", d) if isinstance(d, dict) else d
    try:
        return _humanise_newlines(
            json.dumps(body, indent=2, ensure_ascii=False, default=str))
    except Exception as e:
        return f"(failed to render JSON: {e})"


def _response_json(dump_path: str, next_dump: dict | None,
                   cur_msgs: list[dict]) -> tuple[str, bool]:
    """Return (prettified-JSON, is_synthesised).
    Prefers a paired response_dump on disk (written by the post_api_request
    hook); falls back to synthesising from the next iteration's added
    messages when no response_dump exists.
    """
    p = Path(dump_path)
    base = p.name
    if base.startswith("request_dump_"):
        resp_path = p.parent / ("response_dump_" + base[len("request_dump_"):])
        if resp_path.exists():
            try:
                d = json.loads(resp_path.read_text(encoding="utf-8"))
                return (_humanise_newlines(
                    json.dumps(d, indent=2, ensure_ascii=False,
                               default=str)), False)
            except Exception as e:
                return (f"(failed to read response_dump: {e})", False)
    if next_dump is None:
        synth = {
            "_synthesised": True,
            "_note": "no next iteration yet — this is the latest dump",
        }
        return (_humanise_newlines(
            json.dumps(synth, indent=2, ensure_ascii=False)), True)
    next_msgs = _msgs(next_dump)
    added, _ = _new_msg_indices(cur_msgs, next_msgs)
    synth = {
        "_synthesised": True,
        "_note": ("derived from the next iteration's added messages — "
                  "the real API response (id, usage, finish_reason) requires "
                  "the post_api_request hook to write a paired response_dump"),
        "added_messages": [next_msgs[i] for i in added],
    }
    return (_humanise_newlines(
        json.dumps(synth, indent=2, ensure_ascii=False, default=str)), True)


def _render_iteration(turn_idx: int, iter_idx: int, *, prev_msgs: list[dict],
                      cur_msgs: list[dict], dump_path: str = "",
                      next_dump: dict | None = None,
                      open_default: bool = False) -> str:
    chars = sum(_msg_chars(m) for m in cur_msgs)
    toks = chars // 4
    band = _band(toks)
    archived_count = sum(1 for m in cur_msgs
                         if m.get("role") == "tool" and _is_archived(_cstr(m)))
    live_papers = sum(1 for m in cur_msgs
                      if m.get("role") == "tool" and _paper_id(_cstr(m))
                      and not _is_archived(_cstr(m)))
    added, mutated = _new_msg_indices(prev_msgs, cur_msgs)

    # Compact one-line summary: «1.2  clean  14,818 tok · 4 msgs · +2 new»
    meta_bits = [f"{toks:,} tok", f"{len(cur_msgs)} msgs"]
    if live_papers:
        meta_bits.append(f"{live_papers} live")
    if archived_count:
        meta_bits.append(f"{archived_count} archived")
    if added:
        meta_bits.append(f"+{len(added)} new")
    if mutated:
        meta_bits.append(f"~{len(mutated)} mutated")
    summary = (f'<span class="iter-num">{turn_idx}.{iter_idx}</span> '
               f'<span class="band {band}">{band}</span> '
               f'<span class="iter-meta">{" · ".join(meta_bits)}</span>')

    body = ""
    if added:
        body += '<div class="section-foot">added</div>'
        for i in added:
            body += _render_message(i, cur_msgs[i])
    if mutated:
        body += '<div class="section-foot">mutated</div>'
        for i in mutated:
            body += _render_message(i, cur_msgs[i], mutated=True)
    if not added and not mutated:
        body += ('<div class="section-foot">'
                 '(no message-level changes — same prompt as prior iteration)'
                 '</div>')

    # Verbatim request + response JSON boxes — closed by default, foldable.
    if dump_path:
        req_json = _request_body_json(dump_path)
        resp_json, synth = _response_json(dump_path, next_dump, cur_msgs)
        body += ('<details class="json-box"><summary>'
                 f'<span class="json-label">request body (JSON)</span>'
                 f'<span class="iter-meta">{len(req_json):,} chars</span>'
                 f'</summary><pre class="json">{_h(req_json)}</pre></details>')
        resp_tag = ("synthesised — pending response_dump"
                    if synth else "from response_dump")
        body += ('<details class="json-box"><summary>'
                 f'<span class="json-label">response (JSON)</span>'
                 f'<span class="iter-meta">{resp_tag} · '
                 f'{len(resp_json):,} chars</span>'
                 f'</summary><pre class="json">{_h(resp_json)}</pre></details>')

    open_attr = " open" if open_default else ""
    return f'<details class="iter"{open_attr}><summary>{summary}</summary>{body}</details>'


def _render_turn(turn_idx: int, dumps: list[tuple[str, dict]],
                 *, initial_prev_msgs: list[dict] | None = None,
                 open_default: bool = False,
                 sentinel_next: dict | None = None) -> tuple[str, list[dict]]:
    if not dumps:
        return ""
    first_msgs = _msgs(dumps[0][1])
    last_msgs = _msgs(dumps[-1][1])
    last_chars = sum(_msg_chars(m) for m in last_msgs)
    last_toks = last_chars // 4
    last_band = _band(last_toks)

    # the new user message at the head of this turn — last role=user in the
    # first iteration's prompt (the new user msg is at the tail at iter start)
    user_msg = None
    for m in reversed(first_msgs):
        if m.get("role") == "user":
            user_msg = m
            break
    user_text = _cstr(user_msg) if user_msg else ""
    user_preview = re.sub(r"\s*\[desk:[^\]]*\]\s*$", "", user_text).strip()
    user_preview = user_preview.replace("\n", " ")[:200]

    start_tidy = _tally_tidy(first_msgs)
    end_tidy = _tally_tidy(last_msgs)
    turn_tidy = {k: end_tidy.get(k, 0) - start_tidy.get(k, 0)
                 for k in ("archive", "recall", "shred")}

    # one-line, low-density summary: «Turn N — prompt … · 3 iter · 21k tok · band · tidy a/r/s»
    meta_bits = [f"{len(dumps)} iter", f"{last_toks:,} tok"]
    if any(turn_tidy.values()):
        a, r, s = (turn_tidy[k] for k in ("archive", "recall", "shred"))
        meta_bits.append(f"a/r/s={a}/{r}/{s}")
    summary = (f'<span class="turn-num">Turn {turn_idx}</span>'
               f' <span class="turn-prompt">{_h(user_preview or "(no user msg)")}</span>'
               f' <span class="band {last_band}">{last_band}</span>'
               f' <span class="turn-meta">{" · ".join(meta_bits)}</span>')

    body = ""
    prev_msgs: list[dict] = list(initial_prev_msgs or [])
    n = len(dumps)
    for k, (p_path, d) in enumerate(dumps, start=1):
        cur_msgs = _msgs(d)
        if k < n:
            next_dump = dumps[k][1]
        else:
            # last iter of this turn — use next turn's first dump as the
            # synthesised-response source (so its added assistant message
            # appears in this iter's response box). None if last turn.
            next_dump = sentinel_next
        # Auto-open the LAST iteration of the LAST turn — it's the live state.
        iter_open = open_default and (k == n)
        body += _render_iteration(turn_idx, k,
                                  prev_msgs=prev_msgs, cur_msgs=cur_msgs,
                                  dump_path=p_path, next_dump=next_dump,
                                  open_default=iter_open)
        prev_msgs = cur_msgs

    open_attr = " open" if open_default else ""
    html_str = f'<details class="turn"{open_attr}><summary>{summary}</summary>{body}</details>'
    return html_str, prev_msgs


def _render_session_summary(sid: str, dumps: list[tuple[str, dict]],
                            turns: list[list[tuple[str, dict]]]) -> str:
    if not dumps:
        return ""
    last_msgs = _msgs(dumps[-1][1])
    last_chars = sum(_msg_chars(m) for m in last_msgs)
    last_toks = last_chars // 4
    band = _band(last_toks)
    tidy = _tally_tidy(last_msgs)
    a = tidy.get("archive", 0)
    r = tidy.get("recall", 0)
    s = tidy.get("shred", 0)
    live_blocks = sum(1 for m in last_msgs
                      if m.get("role") == "tool" and _paper_id(_cstr(m))
                      and not _is_archived(_cstr(m)))
    archived = sum(1 for m in last_msgs
                   if m.get("role") == "tool" and _is_archived(_cstr(m)))
    kvs = [
        ("turns", str(len(turns))),
        ("iterations", str(len(dumps))),
        ("end ~tok", f"{last_toks:,}"),
        ("end band", f'<span class="band {band}">{band}</span>'),
        ("tidy a/r/s", f"{a}/{r}/{s}"),
        ("papers", f"{live_blocks} live · {archived} archived"),
    ]
    return ('<div class="session-stats">' +
            "".join(f'<span class="kv"><span class="k">{_h(k)}</span>'
                    f'<span class="v">{v}</span></span>'
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
            f"<h1>desk preview <code>{_h(sid)}</code></h1>"
            f"<div class='subtitle'>rendered {_h(ts)} · "
            f"effective context {EFFECTIVE_CTX:,} tok</div>")
    summary = _render_session_summary(sid, dumps, turns)
    # Open the last turn by default — it's the live state. Older turns stay
    # folded; the user opens them on demand. Thread prev_msgs across turn
    # boundaries so each turn's first iter shows only the genuinely new
    # messages (the new user prompt), not the entire cumulative history.
    # Also stitch the LAST iteration of turn N to the FIRST dump of turn N+1
    # so its synthesised response can use the next-turn first iteration's
    # added messages (the assistant reply that closed turn N).
    n_turns = len(turns)
    body_parts: list[str] = []
    prev_msgs: list[dict] = []
    for i, t in enumerate(turns):
        is_last = (i + 1 == n_turns)
        # For the last iter of this turn, the synthesised response can be
        # pulled from the FIRST dump of the next turn (where the closing
        # assistant message landed). Last turn has no next → sentinel=None.
        sentinel = turns[i + 1][0][1] if (not is_last and turns[i + 1]) else None
        h_str, prev_msgs = _render_turn(
            i + 1, t, initial_prev_msgs=prev_msgs, open_default=is_last,
            sentinel_next=sentinel)
        body_parts.append(h_str)
    body = "".join(body_parts)
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
