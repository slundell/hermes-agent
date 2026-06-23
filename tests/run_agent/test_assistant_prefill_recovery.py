"""Assistant-prefill recovery for thinking-template providers.

Incident 2026-06-12 17:54: a mid-turn forced-overflow compaction (30
messages → 1 fused node) rebuilt the payload so it ended on the partial
assistant message that was in flight when compression triggered. llama.cpp
serving Qwen3.6 with ``--chat-template-kwargs {"enable_thinking":true}``
cannot continue a partial assistant message and returns a deterministic
HTTP 500: "Assistant response prefill is incompatible with
enable_thinking." The retry loop resent the identical payload 3x and the
turn died after 2.5 hours of work.

Recovery (mirrors the multimodal-tool-content strip, #27344): classify the
error specifically, strip the trailing assistant message in place, record
the (provider, model) as prefill-incapable for the session, and retry once
— the model regenerates the partial from the compacted context.
"""

from __future__ import annotations

from agent.error_classifier import FailoverReason, classify_api_error


class _FakeApiError(Exception):
    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


def _make_agent(provider: str = "custom", model: str = "std"):
    from run_agent import AIAgent
    agent = object.__new__(AIAgent)
    agent.provider = provider
    agent.model = model
    return agent


PREFILL_ERROR_MSG = "Assistant response prefill is incompatible with enable_thinking."


# ── Classifier ──────────────────────────────────────────────────────────

class TestClassifier:
    def test_500_prefill_incompatible_is_specific_reason(self):
        e = _FakeApiError(
            PREFILL_ERROR_MSG,
            status_code=500,
            body={"error": {"code": 500, "message": PREFILL_ERROR_MSG,
                            "type": "server_error"}},
        )
        result = classify_api_error(e)
        assert result.reason == FailoverReason.assistant_prefill_unsupported
        assert result.retryable is True
        assert result.should_compress is False

    def test_prefill_incompatible_without_status_is_specific_reason(self):
        """Mid-stream surface: plain APIError, no status code."""
        e = _FakeApiError(PREFILL_ERROR_MSG)
        result = classify_api_error(e)
        assert result.reason == FailoverReason.assistant_prefill_unsupported
        assert result.retryable is True


# ── Strip helper ────────────────────────────────────────────────────────

class TestStripTrailingAssistantPrefill:
    def test_empty_returns_false(self):
        agent = _make_agent()
        assert agent._try_strip_trailing_assistant_prefill([]) is False
        assert agent._try_strip_trailing_assistant_prefill(None) is False

    def test_trailing_user_message_returns_false(self):
        agent = _make_agent()
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "u"}]
        assert agent._try_strip_trailing_assistant_prefill(msgs) is False
        assert len(msgs) == 2

    def test_trailing_tool_message_returns_false(self):
        agent = _make_agent()
        msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
                {"role": "tool", "tool_call_id": "1", "content": "r"}]
        assert agent._try_strip_trailing_assistant_prefill(msgs) is False
        assert len(msgs) == 2

    def test_trailing_assistant_with_tool_calls_returns_false(self):
        """Popping a tool-call message would orphan its tool results."""
        agent = _make_agent()
        msgs = [{"role": "user", "content": "u"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}]
        assert agent._try_strip_trailing_assistant_prefill(msgs) is False
        assert len(msgs) == 2

    def test_trailing_assistant_text_is_stripped(self):
        agent = _make_agent()
        msgs = [{"role": "user", "content": "u"},
                {"role": "assistant", "content": "partial respo"}]
        assert agent._try_strip_trailing_assistant_prefill(msgs) is True
        assert len(msgs) == 1
        assert msgs[-1]["role"] == "user"

    def test_records_provider_model_for_session(self):
        agent = _make_agent(provider="custom", model="std")
        msgs = [{"role": "user", "content": "u"},
                {"role": "assistant", "content": "partial"}]
        agent._try_strip_trailing_assistant_prefill(msgs)
        assert ("custom", "std") in agent._assistant_prefill_unsupported_models
