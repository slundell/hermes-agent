"""Tests for the desk ContextEngine after the desk cut.

The engine no longer wraps ContextCompressor: no synchronous compaction,
compress() is a fail-loud guard, the tidy tools still work.
"""

import json

import pytest


@pytest.fixture()
def engine(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib
    import desk_core
    importlib.reload(desk_core)
    from plugins.context_engine import load_context_engine
    eng = load_context_engine("desk")
    assert eng is not None and eng.name == "desk"
    return eng


def _tool_msg(bid, body, tool_call_id="tc1"):
    return {"role": "tool", "tool_call_id": tool_call_id, "content": f"[{bid}] {body}"}


def test_should_compress_always_false(engine):
    engine.update_model("test-model", 262144)
    assert engine.should_compress() is False
    assert engine.should_compress(999_999_999) is False
    assert engine.should_compress_preflight([]) is False
    assert engine.has_content_to_compress([]) is False


def test_update_model_pins_threshold_to_window(engine):
    engine.update_model("test-model", 262144)
    # threshold == window: the preflight check never trips short of a genuine
    # over-window prompt.
    assert engine.context_length == 262144
    assert engine.threshold_tokens == 262144


def test_compress_is_a_fail_loud_noop(engine, tmp_path):
    msgs = [
        {"role": "user", "content": "hi"},
        _tool_msg("b1", "small"),
        _tool_msg("b2", "X" * 5000, tool_call_id="tc2"),
    ]
    out = engine.compress(msgs)
    # returns the list unchanged — no synchronous reduction
    assert out is msgs
    assert len(out) == 3
    # loud diagnostic written, naming the largest block
    log = tmp_path / "desk" / "overflow.log"
    assert log.exists()
    assert "b2" in log.read_text(encoding="utf-8")


def test_archive_then_recall_round_trips(engine):
    msgs = [_tool_msg("b1", "the original content")]
    r = json.loads(engine.handle_tool_call("archive", {"target": "b1"}, messages=msgs))
    assert r["result"] == "archived b1"
    assert "(archived:" in msgs[0]["content"]
    r = json.loads(engine.handle_tool_call("recall", {"target": "b1"}, messages=msgs))
    assert r["result"] == "recalled b1 onto the desk"
    assert msgs[0]["content"] == "[b1] the original content"


def test_shred_removes_block_and_orphan_tool_call(engine):
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "function": {"name": "x", "arguments": "{}"}}]},
        _tool_msg("b1", "junk", tool_call_id="tc1"),
    ]
    r = json.loads(engine.handle_tool_call("shred", {"target": "b1"}, messages=msgs))
    assert "shredded b1" in r["result"]
    assert msgs == []  # block + its now-empty assistant turn both gone


def test_unknown_tool_rejected(engine):
    r = json.loads(engine.handle_tool_call("frobnicate", {}, messages=[]))
    assert "error" in r


def test_missing_target_gives_clear_error(engine):
    r = json.loads(engine.handle_tool_call("archive", {}, messages=[]))
    assert "target is required" in r["error"]


def test_recall_of_never_archived_block_errors(engine):
    msgs = [_tool_msg("b1", "live content")]
    r = json.loads(engine.handle_tool_call("recall", {"target": "b1"}, messages=msgs))
    assert "not in the archive" in r["error"]


def test_shred_block_without_paired_tool_call(engine):
    # an assistant turn that has no tool_calls must be left untouched by shred
    msgs = [
        {"role": "assistant", "content": "just text, no tool calls"},
        _tool_msg("b1", "junk", tool_call_id="tcX"),
    ]
    r = json.loads(engine.handle_tool_call("shred", {"target": "b1"}, messages=msgs))
    assert "shredded b1" in r["result"]
    assert msgs == [{"role": "assistant", "content": "just text, no tool calls"}]
