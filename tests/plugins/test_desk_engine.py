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


def test_archive_foreign_id_succeeds_with_context_local_description(engine):
    # Cross-session scenario: a paper id p999 was created and archived in some
    # OTHER session — its content lives in the global desk-archive store. The
    # model in this session sees the [p999] reference (via session_search,
    # quoted text, etc.) and archives it. Paper ids are global and the archive
    # store is shared; the description is the CURRENT session's context-local
    # annotation — why does THIS paper matter in THIS work? — and lives in
    # the tool-result message (visible to future iterations of this session)
    # without touching the originating session's archived placeholder.
    import desk_core
    (desk_core.archive_dir() / "p999.txt").write_text(
        "[p999] content from another session", encoding="utf-8")
    msgs_before = [{"role": "user", "content": "saw [p999] mentioned in session_search"}]
    archive_file_before = (desk_core.archive_dir() / "p999.txt").read_text(encoding="utf-8")

    desc = "cross-ref from earlier styckjunkaren analysis"
    r = json.loads(engine.handle_tool_call(
        "archive",
        {"target": "p999", "description": desc},
        messages=msgs_before))

    assert "error" not in r, f"expected success, got error: {r}"
    # The success result carries the context-local description, so a future
    # iteration of this session reading the conversation history can see why
    # the paper was archived in THIS context.
    assert "acknowledged" in r["result"]
    assert desc in r["result"]
    # No file write on this path — originating session's content unchanged.
    assert (desk_core.archive_dir() / "p999.txt").read_text(encoding="utf-8") == \
        archive_file_before


def test_archive_foreign_id_requires_description(engine):
    # Even on the cross-session path the description is REQUIRED. It's
    # context-local — describes why this paper matters in the current
    # session's work — so omitting it isn't allowed any more than it would
    # be for a local archive. The error message explicitly explains the
    # context-local intent so the model knows what to provide.
    import desk_core
    (desk_core.archive_dir() / "p777.txt").write_text(
        "[p777] some prior session content", encoding="utf-8")
    msgs = [{"role": "user", "content": "saw [p777] in session_search"}]

    # Missing description
    r = json.loads(engine.handle_tool_call(
        "archive", {"target": "p777"}, messages=msgs))
    assert "error" in r
    assert "description is required" in r["error"]
    assert "current context" in r["error"]  # signals context-local intent

    # Empty/whitespace description
    r = json.loads(engine.handle_tool_call(
        "archive", {"target": "p777", "description": "   "}, messages=msgs))
    assert "error" in r
    assert "description is required" in r["error"]


def test_archive_foreign_id_with_no_archive_file_still_errors(engine):
    # If the id is neither on the local desk NOR in the global archive store,
    # the original "no paper on the desk" rejection still applies. Lets the
    # model distinguish "you typo'd / hallucinated an id" from "this id is
    # already globally archived".
    msgs = [{"role": "user", "content": "hi"}]
    r = json.loads(engine.handle_tool_call(
        "archive",
        {"target": "p1234", "description": "won't reach the description check"},
        messages=msgs))
    assert "error" in r
    assert "no paper p1234 on the desk" in r["error"]


def test_archive_foreign_id_requires_visibility_in_context(engine):
    # Even if the paper file exists in the global archive store, the model
    # is only allowed the no-op success when the id is actually visible in
    # the current context (a [pN] reference somewhere in the messages list).
    # This guards against archiving ids the model merely invented that
    # happen to exist in the global store.
    import desk_core
    (desk_core.archive_dir() / "p888.txt").write_text(
        "content from elsewhere", encoding="utf-8")
    # No [p888] reference anywhere in this session's messages.
    msgs = [{"role": "user", "content": "tell me about styckjunkaren"}]
    r = json.loads(engine.handle_tool_call(
        "archive",
        {"target": "p888", "description": "made-up id that happens to exist"},
        messages=msgs))
    assert "error" in r, f"expected rejection on invisible id, got: {r}"
    assert "no paper p888 on the desk" in r["error"]
    # Archive file is left intact.
    assert (desk_core.archive_dir() / "p888.txt").read_text(encoding="utf-8") == \
        "content from elsewhere"


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
