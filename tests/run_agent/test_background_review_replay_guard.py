"""Headroom + overflow guards for the bg-review byte-identical replay.

Incident 2026-06-11 20:23: the foreground's last payload was 123.7k real
tokens against a 131,072-token slot. The replay appended the review prompt,
the server rejected it (500 "request exceeds the available context size"),
and the caller fell back to full re-assembly — whose re-derived ~112k
context cold-prefilled the single std slot until the 600s stale-killer
fired (743s total, plus evicting the foreground's warm KV).

The fix is two guards in ``_attempt_review_replay``:

  1. Headroom precheck — when the foreground payload + review prompt +
     response margin don't fit the context window, SKIP the review
     entirely (no replay attempt, no fallback).
  2. Overflow on replay — when the replay request itself comes back as a
     context overflow, also skip (no fallback). Re-assembly is strictly
     worse than no review: same oversized content, cold prefill.

Other structural failures keep the existing fallback to run_conversation.
"""

from unittest.mock import patch

from agent.background_review import (
    REVIEW_REPLAY_MIN_HEADROOM,
    _attempt_review_replay,
    replay_headroom_ok,
)


class _CompressorStub:
    def __init__(self, context_length=131072, last_prompt_tokens=0):
        self.context_length = context_length
        self.last_prompt_tokens = last_prompt_tokens


class _AgentStub:
    def __init__(self, context_length=131072, last_prompt_tokens=0):
        self.context_compressor = _CompressorStub(
            context_length=context_length,
            last_prompt_tokens=last_prompt_tokens,
        )
        self.provider = "custom"
        self.model = "std"
        self.session_id = "sess-test"


class _ReviewAgentStub:
    _interrupt_requested = False


class _MockAPIError(Exception):
    """Shape of an OpenAI SDK APIStatusError."""

    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


def _payload(n_messages=3):
    return {
        "messages": [
            {"role": "user", "content": f"m{i}"} for i in range(n_messages)
        ],
        "tools": None,
    }


REVIEW_MSG = "Review the conversation above and update the skill library."


# ── replay_headroom_ok ──────────────────────────────────────────────────

class TestReplayHeadroomOk:
    def test_no_headroom_near_ceiling(self):
        """123.7k real prompt against a 131,072 window leaves < margin."""
        agent = _AgentStub(context_length=131072, last_prompt_tokens=123_698)
        assert replay_headroom_ok(agent, _payload(), REVIEW_MSG) is False

    def test_plenty_of_headroom(self):
        agent = _AgentStub(context_length=131072, last_prompt_tokens=50_000)
        assert replay_headroom_ok(agent, _payload(), REVIEW_MSG) is True

    def test_unknown_context_length_does_not_gate(self):
        agent = _AgentStub(context_length=0, last_prompt_tokens=123_698)
        assert replay_headroom_ok(agent, _payload(), REVIEW_MSG) is True

    def test_post_compression_sentinel_falls_back_to_estimate(self):
        """last_prompt_tokens=-1 (no real usage yet) must not be trusted as
        a size — a small payload should still pass on the rough estimate."""
        agent = _AgentStub(context_length=131072, last_prompt_tokens=-1)
        assert replay_headroom_ok(agent, _payload(n_messages=3), REVIEW_MSG) is True


# ── _attempt_review_replay ──────────────────────────────────────────────

class TestAttemptReviewReplay:
    def test_no_headroom_skips_review_without_replay_or_fallback(self):
        agent = _AgentStub(context_length=131072, last_prompt_tokens=123_698)

        def _must_not_run(*a, **k):
            raise AssertionError("run_review_replay must not be called without headroom")

        with patch("agent.background_review.run_review_replay", _must_not_run):
            result = _attempt_review_replay(
                agent, _ReviewAgentStub(), REVIEW_MSG, _payload()
            )
        # [] = review skipped AND fallback suppressed (None would fall back).
        assert result == []

    def test_replay_context_overflow_skips_fallback(self):
        """llama.cpp returns overflow as 500; falling back to re-assembly
        would cold-prefill the slot — skip the review instead."""
        agent = _AgentStub(context_length=131072, last_prompt_tokens=100_000)
        err = _MockAPIError(
            "Error code: 500 - the request exceeds the available context "
            "size, try increasing it",
            status_code=500,
        )

        def _raise_overflow(*a, **k):
            raise err

        with patch("agent.background_review.run_review_replay", _raise_overflow):
            result = _attempt_review_replay(
                agent, _ReviewAgentStub(), REVIEW_MSG, _payload()
            )
        assert result == []

    def test_replay_structural_failure_still_falls_back(self):
        agent = _AgentStub(context_length=131072, last_prompt_tokens=50_000)

        def _raise_structural(*a, **k):
            raise RuntimeError("missing attribute on response object")

        with patch("agent.background_review.run_review_replay", _raise_structural):
            result = _attempt_review_replay(
                agent, _ReviewAgentStub(), REVIEW_MSG, _payload()
            )
        assert result is None  # caller falls back to run_conversation

    def test_replay_preemption_skips_fallback(self):
        """Existing behavior preserved: interrupt during replay ends the
        review without fallback."""
        agent = _AgentStub(context_length=131072, last_prompt_tokens=50_000)
        review_agent = _ReviewAgentStub()

        def _raise_after_interrupt(*a, **k):
            review_agent._interrupt_requested = True
            raise RuntimeError("stream aborted")

        with patch("agent.background_review.run_review_replay", _raise_after_interrupt):
            result = _attempt_review_replay(agent, review_agent, REVIEW_MSG, _payload())
        assert result == []

    def test_replay_success_returns_messages(self):
        agent = _AgentStub(context_length=131072, last_prompt_tokens=50_000)
        sentinel = [{"role": "assistant", "content": "review done"}]

        with patch("agent.background_review.run_review_replay", lambda *a, **k: sentinel):
            result = _attempt_review_replay(
                agent, _ReviewAgentStub(), REVIEW_MSG, _payload()
            )
        assert result == sentinel

    def test_missing_payload_falls_back(self):
        agent = _AgentStub(context_length=131072, last_prompt_tokens=50_000)
        assert _attempt_review_replay(agent, _ReviewAgentStub(), REVIEW_MSG, None) is None
        assert (
            _attempt_review_replay(
                agent, _ReviewAgentStub(), REVIEW_MSG, {"messages": []}
            )
            is None
        )

    def test_headroom_margin_is_meaningful(self):
        """The margin must at least cover a review turn (prompt + response
        + a few tool iterations) — guard against someone shrinking it to
        a token-level epsilon."""
        assert REVIEW_REPLAY_MIN_HEADROOM >= 4096
