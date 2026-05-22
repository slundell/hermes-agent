"""desk — model-driven desk-tidying ContextEngine for hermes.

The desk is the model's working context. The model tidies it — files spent
blocks away and pulls them back — instead of leaning on opaque compaction.
("tidy" is the desk verb; Hermes's built-in `curator` is a separate thing —
it curates skills, not context. Desk = context, curator = skills.)

Wraps the built-in ContextCompressor (kept as the fallback summariser, with a
raised threshold so it is a genuine last resort) and adds the tidy tools the
model drives itself:

  archive(target)  — move a spent block's content to the plain-text archive on
                     the PVC, leaving a one-line placeholder on the desk.
                     Reversible.
  recall(target)   — bring an archived block's content back onto the desk.

Block ids ([bN]) are stamped on tool results by the `desk-ids` plugin. The
model decides what to tidy; this engine only renders the means and executes
the model's calls. Select with `context.engine: desk` in config.yaml.

The wrapped compressor is built lazily in update_model() (which carries the
model). Tidying (archive/recall) works regardless — it only needs the live
message list.

Stage 3 of the desk implementation. `shred` is added at Stage 5.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from agent.context_engine import ContextEngine
from agent.context_compressor import ContextCompressor

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover
    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        v = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(v).resolve() if v else (Path.home() / ".hermes").resolve()

_BID = re.compile(r"^\s*\[(b\d+)\]")
_ARCHIVED_MARK = "(archived:"
TIDY_TOOLS = {"archive", "recall", "shred"}
# Fraction of the model context window at which the wrapped ContextCompressor
# compacts. The desk feature raises this above the hermes default (config
# `compression.threshold`, 0.8) so the compressor stays a genuine last resort
# — model-driven tidying does the normal work.
#
# DUPLICATED: plugins/desk-note defines DESK_COMPACTION_THRESHOLD under the
# same name and value (the two desk plugins share no module). Keep the two
# equal. Replace both with a dynamic calc when feasible.
DESK_COMPACTION_THRESHOLD = 0.92

# Engine tool schemas are returned BARE ({name,description,parameters}) —
# hermes wraps each as {"type":"function","function": <schema>} itself.
_ARCHIVE_TOOL = {
    "name": "archive",
    "description": (
        "Move a spent block off the desk into your archive. The block is kept "
        "whole and safe and can be brought back any time with recall — "
        "archiving never loses anything, it only moves the block aside. A "
        "one-line placeholder is left in its place. Use on bulky spent "
        "tool-result blocks."),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "The id of the block to archive — bare (b37) "
                               "or bracketed ([b37]); both accepted.",
            },
        },
        "required": ["target"],
    },
}
_RECALL_TOOL = {
    "name": "recall",
    "description": "Bring an archived block back onto the desk, verbatim, by its id.",
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "The id of the archived block to bring back.",
            },
        },
        "required": ["target"],
    },
}
_SHRED_TOOL = {
    "name": "shred",
    "description": (
        "Destroy a block for good — no archive, no way back. Rare and "
        "irreversible: use only for a block that is plainly worthless (an empty "
        "result, a failed or timed-out command, a search that found nothing). "
        "If unsure, archive instead."),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "The id of the block to destroy — bare (b37) or "
                               "bracketed ([b37]); both accepted.",
            },
        },
        "required": ["target"],
    },
}


def _norm(target) -> str:
    t = str(target or "").strip().strip("[]").strip()
    return t if t.startswith("b") else f"b{t}"


def _content_str(m) -> str:
    c = m.get("content", "")
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return c or ""


class DeskEngine(ContextEngine):
    """Desk engine — wraps the compressor (lazy), adds archive/recall."""

    def __init__(self) -> None:
        self._inner = None          # built lazily in update_model()
        self._session_id = "default"

    @property
    def name(self) -> str:
        return "desk"

    def _sync(self) -> None:
        if self._inner is None:
            return
        for a in ("last_prompt_tokens", "last_completion_tokens",
                  "last_total_tokens", "threshold_tokens", "context_length",
                  "compression_count"):
            try:
                setattr(self, a, getattr(self._inner, a))
            except Exception:
                pass

    # -- compaction: delegate to the wrapped compressor, guarded -------------
    def update_model(self, model, context_length, base_url="", api_key="", provider=""):
        try:
            self._inner = ContextCompressor(
                model=model,
                threshold_percent=DESK_COMPACTION_THRESHOLD,
                base_url=base_url or "",
                api_key=api_key or "",
                provider=provider or "",
                config_context_length=context_length,
            )
            self._inner.update_model(model, context_length, base_url, api_key, provider)
            self._inner.threshold_percent = DESK_COMPACTION_THRESHOLD
            if context_length:
                self._inner.threshold_tokens = int(context_length * DESK_COMPACTION_THRESHOLD)
            # observation/tuning override — pin the compaction trigger directly
            _override = int(os.environ.get("DESK_THRESHOLD_TOKENS", "0") or 0)
            if _override > 0:
                self._inner.threshold_tokens = _override
        except Exception:
            self._inner = None
        self._sync()

    def update_from_response(self, usage):
        if self._inner is not None:
            self._inner.update_from_response(usage)
            self._sync()

    def should_compress(self, prompt_tokens=None):
        if self._inner is None:
            return False
        r = self._inner.should_compress(prompt_tokens)
        self._sync()
        return r

    def should_compress_preflight(self, messages):
        return self._inner.should_compress_preflight(messages) if self._inner else False

    def has_content_to_compress(self, messages):
        return self._inner.has_content_to_compress(messages) if self._inner else False

    def compress(self, messages, current_tokens=None, focus_topic=None):
        if self._inner is None:
            return messages
        r = self._inner.compress(messages, current_tokens, focus_topic)
        self._sync()
        return r

    def on_session_start(self, session_id, **kwargs):
        self._session_id = session_id or "default"
        if self._inner is not None:
            try:
                self._inner.on_session_start(session_id, **kwargs)
            except Exception:
                pass

    def on_session_end(self, session_id, messages):
        if self._inner is not None:
            try:
                self._inner.on_session_end(session_id, messages)
            except Exception:
                pass

    def on_session_reset(self):
        if self._inner is not None:
            try:
                self._inner.on_session_reset()
            except Exception:
                pass
        self._sync()

    def get_status(self):
        if self._inner is not None:
            return self._inner.get_status()
        return super().get_status()

    # -- tidy tools ---------------------------------------------------------
    def get_tool_schemas(self):
        return [_ARCHIVE_TOOL, _RECALL_TOOL, _SHRED_TOOL]

    def _archive_dir(self) -> Path:
        # Flat, session-independent store. Block ids are globally unique
        # (desk-ids stamps from one monotonic counter), so no per-session
        # subdir is needed — and archive/recall no longer depend on the
        # engine's session id, which goes stale under interleaved sessions.
        d = get_hermes_home() / "desk-archive"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _find_block(self, messages, target):
        for m in messages or []:
            if m.get("role") != "tool":
                continue
            mm = _BID.match(_content_str(m))
            if mm and mm.group(1) == target:
                return m
        return None

    def handle_tool_call(self, name, args, **kwargs):
        if name not in TIDY_TOOLS:
            if self._inner is not None:
                try:
                    return self._inner.handle_tool_call(name, args, **kwargs)
                except Exception:
                    pass
            return json.dumps({"error": f"unknown tool: {name}"})

        messages = kwargs.get("messages")
        if not isinstance(messages, list):
            return json.dumps({"error": "no live message list available"})
        args = args if isinstance(args, dict) else {}
        target = _norm(args.get("target"))

        block = self._find_block(messages, target)
        if block is None:
            return json.dumps({"error": f"no block {target} on the desk"})
        content = _content_str(block)

        if name == "archive":
            if _ARCHIVED_MARK in content:
                return json.dumps({"result": f"{target} is already archived"})
            try:
                (self._archive_dir() / f"{target}.txt").write_text(
                    content, encoding="utf-8")
            except Exception as e:
                return json.dumps({"error": f"archive write failed: {e}"})
            block["content"] = (
                f"[{target}] {_ARCHIVED_MARK} {len(content)} chars moved off the "
                f"desk — recall {target} to bring it back)")
            return json.dumps({"result": f"archived {target}"})

        if name == "recall":
            f = self._archive_dir() / f"{target}.txt"
            if not f.exists():
                return json.dumps({"error": f"{target} is not in the archive"})
            try:
                block["content"] = f.read_text(encoding="utf-8")
            except Exception as e:
                return json.dumps({"error": f"recall read failed: {e}"})
            return json.dumps({"result": f"recalled {target} onto the desk"})

        if name == "shred":
            # destroy the block and its paired assistant tool-call — orphan-safe.
            tcid = block.get("tool_call_id")
            try:
                messages.remove(block)
            except ValueError:
                pass
            for m in list(messages):
                if m.get("role") != "assistant" or not m.get("tool_calls"):
                    continue
                kept = [tc for tc in m["tool_calls"] if tc.get("id") != tcid]
                if len(kept) == len(m["tool_calls"]):
                    continue
                if kept:
                    m["tool_calls"] = kept
                elif (m.get("content") or "").strip():
                    m.pop("tool_calls", None)
                else:
                    try:
                        messages.remove(m)
                    except ValueError:
                        pass
            return json.dumps({"result": f"shredded {target} — gone for good"})

        return json.dumps({"error": f"unhandled tidy tool: {name}"})
