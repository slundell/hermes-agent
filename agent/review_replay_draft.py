"""DRAFT — byte-identical prefix replay for the background-review fork.

Problem (measured): the review fork re-runs the full assembly pipeline
(run_conversation -> LCM on_session_start/compress) on its message snapshot,
producing a DIFFERENT context (e.g. 131k) than the foreground's last-sent
payload (141k). Only the ~26k system+tools front matches -> the fork
cold-prefills ~104k per call and (on the single std slot) starves live turns.

Fix: capture-and-replay. Hermes already guarantees a byte-stable prefix for the
foreground (cached system prompt; plugin/prefetch content quarantined to the
current-turn user message; bit-perfect normalization — conversation_loop.py
~1034/988/1081). So byte-identity for the fork reduces to: hand it the
foreground's EXACT last-sent payload and append only the review prompt — never
re-assemble. That is immune to plugins / LCM / timestamps by construction,
because nothing in the pipeline re-runs.

Two pieces:
  1. CAPTURE — store the foreground's last assembled payload (cheap, shallow).
  2. REPLAY — a slim review loop that freezes that payload as the prefix and
     appends the review prompt + tool branch, so the warm std KV transfers.

Status: DRAFT. Not wired in. The capture is 2 lines; the replay reuses the
agent's existing call + tool machinery (the two spots marked INTEGRATION need a
final read of the tool-executor entry before this is runnable).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_REVIEW_ITERS = 4   # cap: a short review, not the 49-tool-turn deep dive


# ── 1. CAPTURE ────────────────────────────────────────────────────────────────
# Add to AIAgent._build_api_kwargs (run_agent.py:4617), at the top:
#
#     def _build_api_kwargs(self, api_messages):
#         # [byte-identical replay] snapshot the exact assembled payload so the
#         # background-review fork can replay it verbatim (foreground captures on
#         # itself; the fork reads the PARENT's snapshot at spawn — separate
#         # objects, no clobber). api_messages is freshly built per call
#         # (conversation_loop:979) and not mutated after, so a shallow list()
#         # copy is a stable, cheap reference (no deep copy of 141k of content).
#         self._last_sent_payload = {"messages": list(api_messages), "tools": self.tools}
#         return build_api_kwargs(self, api_messages)
#
# And in AIAgent._spawn_background_review (run_agent.py:1404), pass the parent's
# snapshot through to the fork runner:
#
#     parent_payload = getattr(self, "_last_sent_payload", None)
#     ... _run_review_in_thread(self, messages_snapshot, prompt, parent_payload=parent_payload)


# ── 2. REPLAY ────────────────────────────────────────────────────────────────
def run_review_replay(
    agent: Any,                 # parent (foreground) agent — for tool ctx/whitelist origin
    review_agent: Any,          # the fork (inherits _cached_system_prompt + tools)
    review_prompt: str,
    parent_payload: Optional[Dict[str, Any]],
    *,
    max_iters: int = MAX_REVIEW_ITERS,
) -> List[Dict[str, Any]]:
    """Run the review with the foreground's payload frozen as the prefix.

    The conversation is:  [parent's exact last api_messages] + [review prompt]
    + [the review's own assistant/tool branch].  The frozen 141k prefix is
    reused from the warm slot every iteration; only the short review branch is
    re-prefilled. No LCM, no re-assembly — byte-identical by construction.
    """
    from agent.chat_completion_helpers import (
        interruptible_streaming_api_call,
        build_assistant_message,
    )

    if not parent_payload or not parent_payload.get("messages"):
        # No captured payload (e.g. first turn) -> caller should fall back to the
        # existing run_conversation path. Signal with empty result.
        logger.debug("review replay: no parent payload; fall back to run_conversation")
        return []

    # Freeze the foreground's exact assembled payload. messages[0] is already the
    # cached system prompt (conversation_loop:1043), and the full injected
    # conversation+tool history follows — i.e. exactly what is warm in the slot.
    frozen_prefix: List[Dict[str, Any]] = list(parent_payload["messages"])
    conv: List[Dict[str, Any]] = frozen_prefix + [
        {"role": "user", "content": review_prompt}
    ]
    # Force byte-identical tools[] too (parent's exact list, not a re-derive).
    review_agent.tools = parent_payload.get("tools") or review_agent.tools

    review_messages: List[Dict[str, Any]] = []
    for _ in range(max_iters):
        # Build kwargs from the FROZEN conv — no _compress_context, no plugin
        # pre_llm_call re-injection into history, no LCM. The prefix bytes equal
        # the slot's, so KV transfers; only the review branch is new.
        api_kwargs = review_agent._build_api_kwargs(conv)
        response = interruptible_streaming_api_call(review_agent, api_kwargs)

        assistant_msg = build_assistant_message(review_agent, response, finish_reason="stop")
        conv.append(assistant_msg)
        review_messages.append(assistant_msg)

        tool_calls = assistant_msg.get("tool_calls")
        if not tool_calls:
            break  # review decided / nothing more to do

        # INTEGRATION POINT A: execute tool_calls via the agent's existing
        # executor, which already honors the thread tool-whitelist set up in
        # background_review (set_thread_tool_whitelist -> memory/skill only).
        # Exact entry TBD on a read of tools/tool_executor.py — shape is:
        #   results = review_agent._execute_tool_calls(tool_calls)   # -> [{tool_call_id, content}, ...]
        results = _execute_review_tool_calls(review_agent, tool_calls)  # see INTEGRATION below
        for r in results:
            tool_msg = {"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"]}
            conv.append(tool_msg)
            review_messages.append(tool_msg)
        # NOTE: frozen_prefix stays byte-identical across iterations; only the
        # appended assistant/tool branch grows (and re-prefills) — bounded by
        # max_iters, so the warm 141k is reused every call.

    return review_messages


def _execute_review_tool_calls(review_agent: Any, tool_calls: List[Any]) -> List[Dict[str, Any]]:
    """INTEGRATION POINT B — reuse the agent's tool-execution path.

    This must route through the SAME executor run_conversation uses (so the
    thread whitelist + memory/skill write-origin tagging apply). Placeholder
    until the exact executor signature is read; do NOT ship as-is.
    """
    raise NotImplementedError(
        "Wire to the existing tool executor (respects set_thread_tool_whitelist). "
        "Read tools/tool_executor.py for the entry that run_conversation uses."
    )


# ── 3. PASSTHROUGH ALTERNATIVE (less code, idempotency caveat) ────────────────
# Instead of the slim loop above, keep run_conversation (free tool machinery)
# but stop the fork from re-assembling:
#   - give the fork a passthrough ContextEngine: should_compress*/compress are
#     no-ops, on_session_start does not re-derive from the DAG, so the provided
#     conversation_history is used verbatim;
#   - pass parent_payload["messages"][1:] (history minus system) as
#     conversation_history; the fork re-adds the inherited system (byte-equal).
# Risk: run_conversation's per-message transforms (conversation_loop:1001-1079)
# re-run on already-assembled messages. They're *designed* to be idempotent
# (strip fields / sanitize orphans), but that must be verified byte-for-byte
# before trusting it — otherwise the prefix drifts again. The slim loop above
# avoids that risk entirely by never re-entering the assembly block.
