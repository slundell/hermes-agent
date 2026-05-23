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
        _tool_msg("p1", "small"),
        _tool_msg("p2", "X" * 5000, tool_call_id="tc2"),
    ]
    out = engine.compress(msgs)
    # returns the list unchanged — no synchronous reduction
    assert out is msgs
    assert len(out) == 3
    # loud diagnostic written, naming the largest paper
    log = tmp_path / "desk" / "overflow.log"
    assert log.exists()
    assert "p2" in log.read_text(encoding="utf-8")


def test_archive_then_recall_round_trips(engine):
    msgs = [_tool_msg("p1", "the original content")]
    r = json.loads(engine.handle_tool_call(
        "archive",
        {"target": "p1", "description": "test content for round-trip"},
        messages=msgs))
    assert r["result"] == "archived p1"
    assert "(archived:" in msgs[0]["content"]
    # the description appears in the placeholder so future iterations can
    # tell what the paper was without recalling it for inspection
    assert "test content for round-trip" in msgs[0]["content"]
    r = json.loads(engine.handle_tool_call("recall", {"target": "p1"}, messages=msgs))
    assert r["result"] == "recalled p1 onto the desk"
    assert msgs[0]["content"] == "[p1] the original content"


def test_archive_requires_description(engine):
    msgs = [_tool_msg("p1", "some content")]
    # no description → reject before writing the archive file
    r = json.loads(engine.handle_tool_call(
        "archive", {"target": "p1"}, messages=msgs))
    assert "description is required" in r.get("error", "")
    # empty/whitespace description also rejected
    r = json.loads(engine.handle_tool_call(
        "archive", {"target": "p1", "description": "   "}, messages=msgs))
    assert "description is required" in r.get("error", "")
    # paper was NOT archived (placeholder absent, content untouched)
    assert "(archived:" not in msgs[0]["content"]


def test_archive_placeholder_format(engine):
    msgs = [_tool_msg("p1", "X" * 2500)]
    r = json.loads(engine.handle_tool_call(
        "archive",
        {"target": "p1", "description": "run_agent.py:1-1000 — orientation"},
        messages=msgs))
    assert r["result"] == "archived p1"
    placeholder = msgs[0]["content"]
    # format: [p1] (archived: <desc> — <chars> chars off-desk; recall p1 to restore)
    assert placeholder.startswith("[p1] (archived: run_agent.py:1-1000 — orientation")
    # size is measured in tokens now (via desk_core.token_count — chars/4
    # fallback when no DESK_TOKENIZER_URL configured)
    assert "tokens off-desk" in placeholder
    assert "recall p1 to restore" in placeholder


def test_archive_description_is_capped(engine):
    msgs = [_tool_msg("p1", "content")]
    long_desc = "x" * 1000
    r = json.loads(engine.handle_tool_call(
        "archive", {"target": "p1", "description": long_desc}, messages=msgs))
    assert r["result"] == "archived p1"
    # the placeholder caps the description at 150 chars
    placeholder = msgs[0]["content"]
    assert "x" * 150 in placeholder
    assert "x" * 200 not in placeholder


def test_shred_removes_block_and_orphan_tool_call(engine):
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc1", "function": {"name": "x", "arguments": "{}"}}]},
        _tool_msg("p1", "junk", tool_call_id="tc1"),
    ]
    r = json.loads(engine.handle_tool_call("shred", {"target": "p1"}, messages=msgs))
    assert "shredded p1" in r["result"]
    assert msgs == []  # paper + its now-empty assistant turn both gone


def test_unknown_tool_rejected(engine):
    r = json.loads(engine.handle_tool_call("frobnicate", {}, messages=[]))
    assert "error" in r


def test_missing_target_gives_clear_error(engine):
    r = json.loads(engine.handle_tool_call("archive", {}, messages=[]))
    assert "target is required" in r["error"]


def test_recall_of_never_archived_block_errors(engine):
    msgs = [_tool_msg("p1", "live content")]
    r = json.loads(engine.handle_tool_call("recall", {"target": "p1"}, messages=msgs))
    assert "not in the archive" in r["error"]


def test_shred_block_without_paired_tool_call(engine):
    # an assistant turn that has no tool_calls must be left untouched by shred
    msgs = [
        {"role": "assistant", "content": "just text, no tool calls"},
        _tool_msg("p1", "junk", tool_call_id="tcX"),
    ]
    r = json.loads(engine.handle_tool_call("shred", {"target": "p1"}, messages=msgs))
    assert "shredded p1" in r["result"]
    assert msgs == [{"role": "assistant", "content": "just text, no tool calls"}]


def test_tool_descriptions_explain_system_side_overhead(engine):
    schemas = {s["name"]: s for s in engine.get_tool_schemas()}
    # archive should explain that only [bN] papers are archivable and that
    # system overhead is not on the desk
    arch_desc = schemas["archive"]["description"]
    assert "Only [bN]" in arch_desc
    assert "system prompt" in arch_desc.lower()
    assert "AGENTS.md" in arch_desc
    assert "tell the user" in arch_desc.lower()
    # archive must require a description parameter so the placeholder carries
    # an identity hint (no more recall-as-inspection)
    arch_params = schemas["archive"]["parameters"]
    assert "description" in arch_params["properties"]
    assert "description" in arch_params["required"]
    # shred should mention the band gate
    shred_desc = schemas["shred"]["description"]
    assert "urgent" in shred_desc.lower() and "forced" in shred_desc.lower()
    # recall should warn against using it for inspection
    rec_desc = schemas["recall"]["description"]
    assert "grow" in rec_desc.lower()


def test_compress_signals_hard_noop_to_avoid_session_rotation(engine):
    # compress_context() rotates the session unless the engine flags a hard
    # no-op via _last_compress_aborted. The desk must set it so an API
    # overflow aborts the turn WITHOUT forking the session lineage.
    msgs = [{"role": "user", "content": "hi"}]
    engine.compress(msgs)
    assert engine._last_compress_aborted is True
    assert engine._last_summary_error
