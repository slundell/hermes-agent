"""desk — model-driven context-tidying ContextEngine.

The desk is the model's working context. The model tidies it — archive /
recall / shred, addressed by [bN] block id — instead of leaning on opaque
compaction. ("tidy" is the desk verb; Hermes's built-in `curator` curates
*skills*, a separate thing — desk = context, curator = skills.)

After the desk cut this engine no longer wraps the LLM-based
ContextCompressor. Context reduction is model-driven and asynchronous: the
model shrinks the desk on its next turn via tool calls, prompted by the
escalating watermark note (the `plugins/desk` hook-plugin). There is therefore
no synchronous compaction:

  should_compress() -> always False
  compress()        -> reached only when the API rejects a request as too
                       large before any tidy turn could run; it fails loud
                       and returns the message list unchanged. The caller's
                       existing "cannot compress further" path then aborts the
                       turn with the session preserved.

Shared primitives (block-id regex, state paths, watermark math) live in the
top-level `desk_core` module. Select with `context.engine: desk` in
config.yaml.
"""
from __future__ import annotations

import json

import desk_core
from agent.context_engine import ContextEngine

TIDY_TOOLS = {"archive", "recall", "shred"}
_ARCHIVED_MARK = "(archived:"

# Engine tool schemas are returned BARE ({name,description,parameters}) —
# hermes wraps each as {"type":"function","function": <schema>} itself.
_ARCHIVE_TOOL = {
    "name": "archive",
    "description": (
        "Move a spent block off the desk into your archive. The block is kept "
        "whole and safe and can be brought back any time with recall — "
        "archiving never loses anything, it only moves the block aside. A "
        "one-line placeholder carrying your `description` is left in its "
        "place, so you (or a future iteration) can see what's archived at a "
        "glance without recalling it. Use on bulky spent tool-result blocks. "
        "Only [bN]-stamped tool-result blocks are on the desk and can be "
        "archived. System overhead — system prompt, tool schemas, memory, "
        "project context (AGENTS.md, etc.) — is *not* on the desk: it adds "
        "to the fill but cannot be archived. When no [bN] blocks remain on "
        "the desk and the desk is still full, that fill is system-side and "
        "you should tell the user rather than keep archiving."),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "The id of the block to archive — bare (b37) "
                               "or bracketed ([b37]); both accepted.",
            },
            "description": {
                "type": "string",
                "description": (
                    "REQUIRED. A brief, specific one-line label of what this "
                    "block contains, e.g. 'run_agent.py:1-1000 — orientation', "
                    "'terminal: pip install output', or 'search: 17 results for "
                    "OAuth refresh'. Appears in the placeholder left on the "
                    "desk — without it future iterations have to recall the "
                    "block just to see what it was, growing the desk again. "
                    "Keep it short (<150 chars)."),
            },
        },
        "required": ["target", "description"],
    },
}
_RECALL_TOOL = {
    "name": "recall",
    "description": "Bring an archived block back onto the desk, verbatim, by its id. "
                   "Recall *grows* the desk — only use it when you need the original "
                   "content again, not to inspect the desk state.",
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
        "If unsure, archive instead. Only available at the urgent and forced "
        "watermark bands; denied at clean and notice (use archive)."),
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


class DeskEngine(ContextEngine):
    """Desk engine — model-driven tidying; no synchronous compaction."""

    def __init__(self) -> None:
        self._session_id = "default"
        # Read by compress_context() to detect a hard no-op — see compress().
        self._last_compress_aborted = False
        self._last_summary_error = None

    @property
    def name(self) -> str:
        return "desk"

    # -- compaction interface: there is no synchronous compaction -----------
    def update_model(self, model, context_length, base_url="", api_key="",
                     provider="") -> None:
        self.context_length = context_length or 0
        # No compaction threshold exists. Pin threshold_tokens to the window
        # itself so the preflight check (`tokens >= threshold_tokens`) only
        # trips when the prompt already exceeds the window — a genuine
        # overflow, which compress() then fails loud on.
        self.threshold_tokens = self.context_length

    def update_from_response(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        self.last_prompt_tokens = usage.get("prompt_tokens", 0) or 0
        self.last_completion_tokens = usage.get("completion_tokens", 0) or 0
        self.last_total_tokens = usage.get("total_tokens", 0) or 0

    def should_compress(self, prompt_tokens=None) -> bool:
        # Reduction is model-driven and asynchronous — never compact synchronously.
        return False

    def should_compress_preflight(self, messages) -> bool:
        return False

    def has_content_to_compress(self, messages) -> bool:
        # Manual /compress has nothing to do on the desk.
        return False

    def compress(self, messages, current_tokens=None, focus_topic=None, force=False):
        # current_tokens / focus_topic / force are accepted for caller-signature
        # compatibility and ignored — the desk does not compact synchronously.
        # Reaching compress() means the API rejected the request as too large
        # before any tidy turn could run. The desk cannot reduce context
        # synchronously — fail loud and return the list unchanged.
        desk_core.log_overflow(
            self._biggest_block(messages), len(messages) if messages else 0)
        # Signal a hard no-op via the attributes compress_context() checks.
        # This makes it take the abort short-circuit (clean user warning, NO
        # session rotation) instead of the normal post-compression session
        # split. Set on every call — a desk compress() is always an abort;
        # the desk has no successful-compaction path.
        self._last_compress_aborted = True
        self._last_summary_error = (
            "desk overflow — context cannot be reduced synchronously; "
            "the model must tidy its desk")
        return messages

    @staticmethod
    def _biggest_block(messages):
        biggest = None
        for m in messages or []:
            if not isinstance(m, dict) or m.get("role") != "tool":
                continue
            c = desk_core.content_str(m)
            if biggest is None or len(c) > biggest[1]:
                biggest = (desk_core.block_id(c) or "?", len(c))
        return biggest

    def on_session_start(self, session_id, **kwargs) -> None:
        self._session_id = session_id or "default"

    # -- tidy tools ---------------------------------------------------------
    def get_tool_schemas(self):
        return [_ARCHIVE_TOOL, _RECALL_TOOL, _SHRED_TOOL]

    def _find_block(self, messages, target):
        for m in messages or []:
            if not isinstance(m, dict) or m.get("role") != "tool":
                continue
            if desk_core.block_id(desk_core.content_str(m)) == target:
                return m
        return None

    def handle_tool_call(self, name, args, **kwargs) -> str:
        if name not in TIDY_TOOLS:
            return json.dumps({"error": f"unknown tool: {name}"})

        messages = kwargs.get("messages")
        if not isinstance(messages, list):
            return json.dumps({"error": "no live message list available"})
        args = args if isinstance(args, dict) else {}
        raw_target = args.get("target")
        if not str(raw_target or "").strip():
            return json.dumps({"error": "target is required — give the block id, e.g. b37"})
        target = _norm(raw_target)

        block = self._find_block(messages, target)
        if block is None:
            return json.dumps({"error": f"no block {target} on the desk"})
        content = desk_core.content_str(block)

        if name == "archive":
            if _ARCHIVED_MARK in content:
                return json.dumps({"result": f"{target} is already archived"})
            # description is REQUIRED — the placeholder carries it so future
            # iterations can see what's archived without recalling. Reject the
            # call if the model omits it (the model has to think and label).
            desc = str(args.get("description") or "").strip()
            if not desc:
                return json.dumps({"error":
                    "description is required — a brief one-line label of what "
                    "this block contains, e.g. 'run_agent.py:1-1000 — "
                    "orientation'. Without it the placeholder on the desk "
                    "carries no identity and a later iteration has to recall "
                    "the block just to see what it was."})
            desc = desc[:150]  # cap to keep placeholders compact
            try:
                (desk_core.archive_dir() / f"{target}.txt").write_text(
                    content, encoding="utf-8")
            except Exception as e:
                return json.dumps({"error": f"archive write failed: {e}"})
            tok = desk_core.token_count(content)
            block["content"] = (
                f"[{target}] {_ARCHIVED_MARK} {desc} — ~{tok:,} tokens "
                f"off-desk; recall {target} to restore)")
            return json.dumps({"result": f"archived {target}"})

        if name == "recall":
            f = desk_core.archive_dir() / f"{target}.txt"
            if not f.exists():
                return json.dumps({"error": f"{target} is not in the archive"})
            try:
                block["content"] = f.read_text(encoding="utf-8")
            except Exception as e:
                return json.dumps({"error": f"recall read failed: {e}"})
            return json.dumps({"result": f"recalled {target} onto the desk"})

        if name == "shred":
            # destroy the block and its paired assistant tool-call — orphan-safe.
            # If the block was previously archived, its desk-archive/bN.txt file
            # is left as a harmless orphan: block ids are globally monotonic and
            # never reused, so a stale archive file can never be mis-recalled.
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
