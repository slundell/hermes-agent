"""Tests for the unified desk hook-plugin (plugins/desk/).

One plugin registers every desk hook: id stamping, the watermark note, the
fill-token capture, and the context-rollback diagnostic.
"""

import importlib
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def desk(monkeypatch, tmp_path):
    """Load plugins/desk/__init__.py fresh, HERMES_HOME isolated."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import desk_core
    importlib.reload(desk_core)
    spec = importlib.util.spec_from_file_location(
        "desk_plugin_under_test", REPO / "plugins" / "desk" / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeCtx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, cb):
        self.hooks.setdefault(name, []).append(cb)


def test_register_wires_every_desk_hook(desk):
    ctx = _FakeCtx()
    desk.register(ctx)
    for hook in ("transform_tool_result", "pre_llm_call", "post_api_request",
                 "pre_api_request", "on_session_reset"):
        assert hook in ctx.hooks, f"{hook} not registered"


def test_stamp_adds_block_id(desk):
    out = desk._stamp(result="a tool result", session_id="s1")
    assert out.startswith("[b1] ")
    # already-stamped results are left alone
    assert desk._stamp(result="[b9] already", session_id="s1") is None
    # no session id -> no stamp
    assert desk._stamp(result="x", session_id="") is None


def test_pre_llm_call_emits_note_at_notice_level(desk):
    import desk_core
    desk._on_post_api_request(
        usage={"prompt_tokens": int(desk_core.EFFECTIVE_CTX * 0.85)},
        session_id="s1")
    msgs = [{"role": "tool", "content": "[b1] r1"}]
    out = desk._on_pre_llm_call(session_id="s1", conversation_history=msgs)
    assert out is not None
    assert "filling up" in out["context"]
    assert "ids on the desk: b1" in out["context"]


def test_pre_llm_call_calm_returns_no_note(desk):
    import desk_core
    desk._on_post_api_request(
        usage={"prompt_tokens": int(desk_core.EFFECTIVE_CTX * 0.10)},
        session_id="s2")
    out = desk._on_pre_llm_call(session_id="s2", conversation_history=[])
    assert out is None


def test_forced_level_restricts_tools(desk, monkeypatch):
    import desk_core
    calls = {}
    monkeypatch.setattr(desk, "set_thread_tool_whitelist",
                        lambda tools, **k: calls.setdefault("set", tools))
    desk._on_post_api_request(
        usage={"prompt_tokens": int(desk_core.EFFECTIVE_CTX * 1.10)},
        session_id="s3")
    desk._on_pre_llm_call(session_id="s3", conversation_history=[])
    assert calls.get("set") == desk_core.FORCED_TIDY_TOOLS


def test_rollback_diagnostic_logs_on_large_drop(desk, tmp_path):
    big = [{"role": "tool", "content": f"[b{i}] r"} for i in range(20)]
    desk._on_pre_api_request(session_id="s4", request_messages=big)
    small = [{"role": "tool", "content": "[b1] r"}]
    desk._on_pre_api_request(session_id="s4", request_messages=small)
    log = tmp_path / "context-trace" / "rollbacks.log"
    assert log.exists()
    assert "CONTEXT ROLLBACK DETECTED" in log.read_text(encoding="utf-8")


def test_session_reset_clears_cached_tokens(desk):
    import desk_core
    # a high token count would put the desk at the forced level
    desk._on_post_api_request(
        usage={"prompt_tokens": int(desk_core.EFFECTIVE_CTX * 1.10)},
        session_id="s5")
    desk._on_session_reset(session_id="s5")
    # after reset the cached count is gone — the next turn falls back to the
    # chars/4 estimate (calm for an empty history), not a stale 'forced'.
    out = desk._on_pre_llm_call(session_id="s5", conversation_history=[])
    assert out is None
