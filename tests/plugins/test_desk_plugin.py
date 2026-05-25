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
    assert out.startswith("[p1] ")
    # already-stamped results are left alone
    assert desk._stamp(result="[p9] already", session_id="s1") is None
    # no session id -> no stamp
    assert desk._stamp(result="x", session_id="") is None


def test_pre_llm_call_emits_note_at_notice_level(desk):
    import desk_core
    desk._on_post_api_request(
        usage={"prompt_tokens": int(desk_core.EFFECTIVE_CTX * 0.85)},
        session_id="s1")
    msgs = [{"role": "tool", "content": "[p1] r1"}]
    out = desk._on_pre_llm_call(session_id="s1", conversation_history=msgs)
    assert out is not None
    assert "filling up" in out["context"]
    assert "papers on the desk: p1" in out["context"]


def test_pre_llm_call_calm_returns_short_state_note(desk):
    """Clean now emits a short '[desk: clean]' note (was None pre-v8) so the
    model's most recent prompt always carries the current band — fixing the
    sync issue where a stale 'forced' note in history led to misuse."""
    import desk_core
    desk._on_post_api_request(
        usage={"prompt_tokens": int(desk_core.EFFECTIVE_CTX * 0.10)},
        session_id="s2")
    out = desk._on_pre_llm_call(session_id="s2", conversation_history=[])
    assert out is not None
    assert "clean" in out["context"].lower()
    # clean note must NOT carry the id-listing appendix (desk is fine)
    assert "papers on the desk" not in out["context"]
    assert "no archivable" not in out["context"]


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
    small = [{"role": "tool", "content": "[p1] r"}]
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
    # chars/4 estimate (clean for an empty history), not a stale 'forced'.
    # Post-v8: clean emits a short note so the state is positively signalled
    # to the model on every iteration; assert it's the clean note (not forced).
    out = desk._on_pre_llm_call(session_id="s5", conversation_history=[])
    assert out is not None
    assert "clean" in out["context"].lower()
    assert "forced" not in out["context"].lower()


def _set_level(tmp_path, sid, lvl):
    import desk_core
    (desk_core.state_dir() / f"{sid}.level").write_text(lvl, encoding="utf-8")


def test_pre_tool_call_blocks_shred_at_calm(desk, tmp_path):
    _set_level(tmp_path, "s6", "clean")
    r = desk._on_pre_tool_call(tool_name="shred", session_id="s6")
    assert isinstance(r, dict) and r.get("action") == "paper"
    assert "shred is unavailable" in r["message"]
    assert "clean" in r["message"]
    # the denial must explain the band may have dropped since the model saw
    # a higher-band note (the sync issue) — so the model doesn't read the
    # paper as a contradiction
    assert "dropped" in r["message"].lower() or "since" in r["message"].lower()


def test_pre_tool_call_blocks_shred_at_notice(desk, tmp_path):
    _set_level(tmp_path, "s7", "notice")
    r = desk._on_pre_tool_call(tool_name="shred", session_id="s7")
    assert isinstance(r, dict) and r.get("action") == "paper"
    assert "notice" in r["message"]


def test_pre_tool_call_allows_shred_at_urgent(desk, tmp_path):
    _set_level(tmp_path, "s8", "urgent")
    assert desk._on_pre_tool_call(tool_name="shred", session_id="s8") is None


def test_pre_tool_call_allows_shred_at_forced(desk, tmp_path):
    _set_level(tmp_path, "s9", "forced")
    assert desk._on_pre_tool_call(tool_name="shred", session_id="s9") is None


def test_pre_tool_call_ignores_non_shred_tools(desk, tmp_path):
    _set_level(tmp_path, "s10", "clean")
    assert desk._on_pre_tool_call(tool_name="archive", session_id="s10") is None
    assert desk._on_pre_tool_call(tool_name="recall", session_id="s10") is None
    assert desk._on_pre_tool_call(tool_name="read_file", session_id="s10") is None


def test_pre_tool_call_defaults_to_calm_when_no_level_file(desk):
    # fresh session with no level file → treat as clean → shred blocked
    r = desk._on_pre_tool_call(tool_name="shred", session_id="never-seen")
    assert isinstance(r, dict) and r.get("action") == "paper"


def test_register_now_includes_pre_tool_call(desk):
    ctx = _FakeCtx()
    desk.register(ctx)
    assert "pre_tool_call" in ctx.hooks


# --- note lists LIVE ids only ---------------------------------------------
def test_pre_llm_call_note_lists_live_ids_only(desk):
    import desk_core
    # push the session to notice so a note is emitted
    desk._on_post_api_request(
        usage={"prompt_tokens": int(desk_core.EFFECTIVE_CTX * 0.85)},
        session_id="ids-live")
    msgs = [
        {"role": "tool", "content": "[p10] live data"},
        {"role": "tool", "content": "[p11] (archived: 9000 chars moved off the desk — recall p11 to bring it back)"},
        {"role": "tool", "content": "[p12] another live"},
    ]
    out = desk._on_pre_llm_call(session_id="ids-live", conversation_history=msgs)
    assert out is not None
    # p11 is an archived placeholder and must NOT appear in the listing
    assert "papers on the desk: p10, p12" in out["context"]
    assert "p11" not in out["context"]


# --- calibration softens the level lag ------------------------------------
def test_calibration_baseline_set_by_post_api_request(desk):
    # Simulate a real turn: pre_llm_call sees a conversation, post_api_request
    # gets the real prompt_tokens. The baseline gets recorded.
    msgs = [{"role": "user", "content": "x" * 4000}]   # 1000 chars/4 estimate
    desk._on_pre_llm_call(session_id="cal", conversation_history=msgs)
    # response reports 25,000 real tokens — system+tools accounted for
    desk._on_post_api_request(
        usage={"prompt_tokens": 25_000}, session_id="cal")
    assert desk._TOK_BASELINE.get("cal") == (25_000, 1000)
    # the in-flight chars4 was consumed by the post-handler
    assert "cal" not in desk._PENDING_CHARS4


def test_pre_llm_call_uses_calibrated_estimate_for_delta(desk):
    import desk_core
    # Calibration: at chars/4 = 1000, real = 25k.
    desk._TOK_BASELINE["cal2"] = (25_000, 1000)
    # Next call sees a bigger conversation — chars/4 grows to 5000
    # (a +4000 chars/4 delta). The estimate the watermark uses should be
    # 25k + 4k = 29k tokens, NOT the stale 25k from last response.
    bigger = [{"role": "user", "content": "y" * 20_000}]    # ~5000 chars/4
    desk._on_pre_llm_call(session_id="cal2", conversation_history=bigger)
    # The level file is what the hook actually wrote — verify the level
    # corresponds to 29k tokens (which is well below 49k notice — clean).
    import desk_core
    lf = desk_core.state_dir() / "cal2.level"
    assert lf.read_text(encoding="utf-8") == "clean"
    # Now simulate a much larger conversation that would cross notice:
    # at the calibrated rate, real = 25k + (huge_chars4 - 1000).
    huge_chars4 = int(desk_core.EFFECTIVE_CTX * 0.85 - 25_000 + 1000)
    desk._TOK_BASELINE["cal2"] = (25_000, 1000)   # reset baseline
    desk._level_file("cal2").write_text("clean", encoding="utf-8")  # reset
    huge = [{"role": "user", "content": "z" * (huge_chars4 * 4)}]
    out = desk._on_pre_llm_call(session_id="cal2", conversation_history=huge)
    assert out is not None
    assert "filling up" in out["context"]    # notice-band note


def test_note_when_no_live_blocks_says_fill_is_system_side(desk):
    """When the watermark fires but nothing is archivable, the note must say
    so explicitly — so the model tells the user instead of grasping at
    non-existent ids (the v3 confusion observed at forced)."""
    import desk_core
    # push to forced; conversation has only placeholders + non-paper messages
    desk._on_post_api_request(
        usage={"prompt_tokens": int(desk_core.EFFECTIVE_CTX * 1.10)},
        session_id="empty")
    msgs = [
        {"role": "user", "content": "x"},
        {"role": "tool", "content": "[p20] (archived: 9000 chars moved off the desk — recall p20 to bring it back)"},
        {"role": "tool", "content": "[p21] (archived: 4500 chars moved off the desk — recall p21 to bring it back)"},
    ]
    out = desk._on_pre_llm_call(session_id="empty", conversation_history=msgs)
    assert out is not None
    assert "no archivable papers remain" in out["context"]
    assert "system-side" in out["context"]
    assert "tell the user" in out["context"]


def test_calibration_cleared_on_session_reset(desk):
    desk._TOK_BASELINE["cal3"] = (50_000, 10_000)
    desk._PENDING_CHARS4["cal3"] = 12_000
    desk._LAST_PROMPT_TOKENS["cal3"] = 50_000
    desk._on_session_reset(session_id="cal3")
    assert "cal3" not in desk._TOK_BASELINE
    assert "cal3" not in desk._PENDING_CHARS4
    assert "cal3" not in desk._LAST_PROMPT_TOKENS


# --- reviewer-fork detection (skip the forced-tool whitelist) -------------


def test_is_reviewer_call_detects_curator_prompt(desk):
    msgs = [
        {"role": "user", "content": "find styckjunkaren"},
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "content": "[p1] result"},
        {"role": "user", "content":
            "Review the conversation above and update the skill library. "
            "Be ACTIVE — most sessions produce..."},
    ]
    assert desk._is_reviewer_call(msgs) is True


def test_is_reviewer_call_false_for_main_agent_history(desk):
    msgs = [
        {"role": "user", "content": "find styckjunkaren"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "thanks, do x"},
    ]
    assert desk._is_reviewer_call(msgs) is False


def test_is_reviewer_call_handles_list_content(desk):
    """Anthropic-style multipart content blocks must also match."""
    msgs = [
        {"role": "user", "content": [
            {"type": "text", "text":
                "Review the conversation above and consider saving to "
                "memory if appropriate.\n\nFocus on:..."},
        ]},
    ]
    assert desk._is_reviewer_call(msgs) is True


def test_reviewer_call_skips_forced_whitelist(desk, monkeypatch):
    """At a forced-band fill, a reviewer-fork pre_llm_call must NOT set
    the tidy-tools whitelist — the reviewer has its own job (skill-
    library update) and should run unrestricted."""
    import desk_core
    set_calls = []
    clear_calls = []
    monkeypatch.setattr(desk, "set_thread_tool_whitelist",
                        lambda tools, **k: set_calls.append(tools))
    monkeypatch.setattr(desk, "clear_thread_tool_whitelist",
                        lambda: clear_calls.append(True))
    # Put the desk at forced first (main-agent path).
    desk._on_post_api_request(
        usage={"prompt_tokens": int(desk_core.EFFECTIVE_CTX * 1.10)},
        session_id="r1")
    # Reviewer-fork call (same session_id, curator prompt in history).
    out = desk._on_pre_llm_call(session_id="r1", conversation_history=[
        {"role": "user", "content": "earlier user msg"},
        {"role": "assistant", "content": "earlier reply"},
        {"role": "user", "content":
            "Review the conversation above and update the skill library."},
    ])
    # No state-note injected, no whitelist set, whitelist cleared
    # defensively.
    assert out is None
    assert set_calls == []
    assert clear_calls == [True]


def test_reviewer_detection_bounded_to_recent_tail(desk):
    """The scan walks only the recent tail so long main-agent histories
    that happen to contain an old (now-stale) sentinel string aren't
    misclassified."""
    msgs = [{"role": "user", "content":
             "Review the conversation above and ..."}]
    msgs += [{"role": "tool", "content": f"[p{i}] r"} for i in range(60)]
    msgs.append({"role": "user", "content": "normal request"})
    assert desk._is_reviewer_call(msgs) is False


# --- write_file as a forced-band escape hatch -----------------------------


def test_forced_tidy_tools_has_tidy_and_persistence_ops():
    """The forced whitelist mixes two roles: tidy ops (archive/shred) to
    make room, plus persistence escape hatches (write_file, memory) so
    the model can commit a synthesis before the turn ends instead of
    being trapped in a pure archive/shred loop. fact_store is
    intentionally NOT included — its search/probe actions grow context
    and name-only whitelisting can't separate them from action=add."""
    import desk_core
    assert "archive" in desk_core.FORCED_TIDY_TOOLS
    assert "shred" in desk_core.FORCED_TIDY_TOOLS
    assert "write_file" in desk_core.FORCED_TIDY_TOOLS
    assert "memory" in desk_core.FORCED_TIDY_TOOLS
    assert "fact_store" not in desk_core.FORCED_TIDY_TOOLS
    assert "read_file" not in desk_core.FORCED_TIDY_TOOLS
    assert "recall" not in desk_core.FORCED_TIDY_TOOLS


def test_forced_note_enumerates_persistence_channels():
    """The forced-band note must tell the model what escape hatches are
    available, otherwise it won't discover them on its own."""
    import desk_core
    note = desk_core.NOTES["forced"]
    assert "write_file" in note
    assert "memory" in note
