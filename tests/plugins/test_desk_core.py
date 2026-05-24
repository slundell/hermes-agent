"""Unit tests for desk_core — the shared desk primitives module."""

import importlib

import pytest


@pytest.fixture()
def dc(monkeypatch, tmp_path):
    """Fresh desk_core with HERMES_HOME isolated to a tempdir."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import desk_core
    importlib.reload(desk_core)
    return desk_core


def test_block_id_parses_bracketed_prefix(dc):
    assert dc.paper_id("[p37] some tool result") == "p37"
    assert dc.paper_id("  [p1] leading space ok") == "p1"
    assert dc.paper_id("no id here") is None
    assert dc.paper_id("[archived] not a paper id") is None


def test_content_str_flattens_list_and_string(dc):
    assert dc.content_str({"content": "plain"}) == "plain"
    assert dc.content_str({"content": [{"text": "a"}, {"text": "b"}]}) == "a b"
    assert dc.content_str({"content": None}) == ""


def test_is_stamped(dc):
    assert dc.is_stamped("[p9] x") is True
    assert dc.is_stamped("x") is False


def test_block_ids_on_desk(dc):
    msgs = [
        {"role": "tool", "content": "[p3] r3"},
        {"role": "assistant", "content": "[p99] not a tool — ignored"},
        {"role": "tool", "content": "[p1] r1"},
        {"role": "tool", "content": "no id"},
    ]
    assert dc.paper_ids_on_desk(msgs) == [1, 3]


def test_live_block_ids_on_desk_excludes_placeholders(dc):
    msgs = [
        {"role": "tool", "content": "[p5] live content"},
        {"role": "tool", "content": "[p6] (archived: 1234 chars moved off the desk — recall p6 to bring it back)"},
        {"role": "tool", "content": "[p7] another live paper"},
        {"role": "tool", "content": "[p8] (archived: 99 chars moved off the desk — recall p8 to bring it back)"},
    ]
    # full view (used by the rollback diagnostic) sees everything
    assert dc.paper_ids_on_desk(msgs) == [5, 6, 7, 8]
    # live view (used by the desk-note's "ids on the desk" listing) hides
    # placeholders so the model isn't misled into treating them as slots
    assert dc.live_paper_ids_on_desk(msgs) == [5, 7]


def test_collapse_ranges(dc):
    assert dc.collapse_ranges([1, 2, 3, 5, 6]) == "p1–p3, p5, p6"
    assert dc.collapse_ranges([4]) == "p4"
    assert dc.collapse_ranges([]) == ""


def test_next_block_id_is_monotonic(dc):
    first = dc.next_paper_id()
    assert dc.next_paper_id() == first + 1
    assert dc.next_paper_id() == first + 2


def test_effective_ctx_is_hygiene_threshold(dc):
    # EFFECTIVE_CTX anchors the desk's working budget to the LLM-compressor's
    # trigger point (gateway hygiene's token threshold, default 0.85 of
    # model context). The desk bands are fractions of this — so forced
    # sits BELOW hygiene by design, giving the model a safety margin to
    # archive proactively before compression takes over.
    assert dc.HYGIENE_PCT == 0.85
    assert dc.HYGIENE_THRESHOLD == int(dc.DESK_MODEL_MAX_CTX * dc.HYGIENE_PCT)
    assert dc.EFFECTIVE_CTX == dc.HYGIENE_THRESHOLD


def test_forced_band_sits_below_hygiene(dc):
    # forced = 0.95 × EFFECTIVE_CTX means the desk's forced band fires at
    # 95% of the compressor's trigger — a 5% safety margin during which
    # the model can still archive before the LLM compressor steps in.
    assert dc.FORCED_PCT == 0.95
    # Compute the token-level thresholds and assert the ordering:
    # notice < urgent < forced < hygiene (compressor) < model_max
    notice_tok = int(dc.NOTICE_PCT * dc.EFFECTIVE_CTX)
    urgent_tok = int(dc.URGENT_PCT * dc.EFFECTIVE_CTX)
    forced_tok = int(dc.FORCED_PCT * dc.EFFECTIVE_CTX)
    hygiene_tok = dc.HYGIENE_THRESHOLD
    assert notice_tok < urgent_tok < forced_tok < hygiene_tok < dc.DESK_MODEL_MAX_CTX
    # Forced should leave a meaningful gap before hygiene (~5% of effective)
    assert hygiene_tok - forced_tok >= int(0.04 * dc.EFFECTIVE_CTX)


def test_level_for_bands(dc):
    # Band entries: notice 0.80, urgent 0.90, forced 0.95 (of EFFECTIVE_CTX).
    # Note: with the band-relative-to-hygiene refactor, fill values up to ~1.05
    # are still meaningful — they represent the model overshooting into the
    # hygiene safety margin before the compressor catches up.
    assert dc.level_for(0.50) == "clean"
    assert dc.level_for(0.85) == "notice"
    assert dc.level_for(0.92) == "urgent"
    assert dc.level_for(0.96) == "forced"
    assert dc.level_for(1.05) == "forced"


def test_level_for_hysteresis_holds_band_on_exit(dc):
    # dropping just below urgent's entry (0.90) holds at urgent within HYSTERESIS
    assert dc.level_for(0.88, prev="urgent") == "urgent"
    # dropping well below it releases to notice
    assert dc.level_for(0.82, prev="urgent") == "notice"


def test_notes_open_with_band_name(dc):
    """Every note must open with the band's literal name so the model has a
    positive signal of which band it's in. Clean gets a (short) note now too —
    v7 drive showed Aina acting on a stale 'forced' note in history after the
    band had dropped to clean; emitting a clean note every iteration keeps the
    most recent prompt state-current."""
    assert dc.NOTES["clean"] is not None
    assert "clean" in dc.NOTES["clean"].lower()
    assert "notice" in dc.NOTES["notice"].lower()
    assert "urgent" in dc.NOTES["urgent"].lower()
    assert "forced" in dc.NOTES["forced"].lower()
    # each non-clean band's note should also mention shred's availability
    assert "shred" in dc.NOTES["notice"].lower()
    assert "shred" in dc.NOTES["urgent"].lower()
    assert "shred" in dc.NOTES["forced"].lower()
    # clean note stays short — no per-turn token waste
    assert len(dc.NOTES["clean"]) <= 30


def test_token_count_fallback_when_no_url(dc, monkeypatch):
    monkeypatch.setattr(dc, "DESK_TOKENIZER_URL", "")
    assert dc.token_count("") == 0
    # chars/4 fallback with floor of 1 for non-empty input
    assert dc.token_count("hi") == 1
    s = "x" * 400
    assert dc.token_count(s) == 100


def test_token_count_calls_endpoint(dc, monkeypatch):
    """When DESK_TOKENIZER_URL is set, token_count POSTs to it and returns
    the length of the `tokens` list."""
    import json as _json
    import urllib.request

    class _FakeResp:
        def __init__(self, body):
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    captured = {}
    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["timeout"] = timeout
        # mimic llamacpp's {"tokens": [...]}
        body = _json.dumps({"tokens": list(range(73))}).encode("utf-8")
        return _FakeResp(body)

    monkeypatch.setattr(dc, "DESK_TOKENIZER_URL", "http://fake/tokenize")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    n = dc.token_count("some content")
    assert n == 73
    assert captured["url"] == "http://fake/tokenize"
    assert _json.loads(captured["body"].decode("utf-8")) == {"content": "some content"}


def test_token_count_falls_back_on_endpoint_failure(dc, monkeypatch):
    """Any tokenizer endpoint failure → chars/4 fallback, never raises."""
    import urllib.request
    def _boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(dc, "DESK_TOKENIZER_URL", "http://fake/tokenize")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert dc.token_count("x" * 200) == 50


def test_log_overflow_writes_loud_diagnostic(dc, tmp_path):
    dc.log_overflow(("p42", 123456), n_messages=80)
    log = tmp_path / "desk" / "overflow.log"
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert "DESK OVERFLOW" in text
    assert "p42" in text


def test_fill_fraction(dc):
    assert dc.fill_fraction(dc.EFFECTIVE_CTX) == pytest.approx(1.0)
    assert dc.fill_fraction(0) == 0.0


def test_log_overflow_handles_none_offending(dc, tmp_path):
    # a real overflow may have no identified paper — must not crash
    dc.log_overflow(None, n_messages=12)
    log = tmp_path / "desk" / "overflow.log"
    assert log.exists()
    assert "DESK OVERFLOW" in log.read_text(encoding="utf-8")


def test_next_block_id_returns_value_even_if_persist_fails(dc, monkeypatch):
    first = dc.next_paper_id()
    # simulate the counter file write failing
    from pathlib import Path
    monkeypatch.setattr(
        Path, "write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    # the incremented value is still returned (in-memory), write failure is logged
    assert dc.next_paper_id() == first + 1


def test_live_message_count_subtracts_archived_placeholders(dc):
    # Archived papers stay in the message list as one-line placeholders so
    # `recall` can find them, but they cost ~50 tokens each. Callers that
    # gate on history length (e.g. gateway hygiene's hard message limit)
    # should see the *logical* conversation count, not raw list length —
    # otherwise the desk's success at trimming tokens triggers an opaque
    # LLM compression because list entries accumulate while tokens stay low.
    msgs = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "content": "[p1] live tool result"},
        {"role": "tool", "content": "[p2] (archived: foo — ~1,234 tokens off-desk; recall p2 to restore)"},
        {"role": "tool", "content": "[p3] another live result"},
        {"role": "tool", "content": "[p4] (archived: bar — ~99 tokens off-desk; recall p4 to restore)"},
    ]
    # raw length is 7; two archived placeholders → 5 live messages
    assert len(msgs) == 7
    assert dc.live_message_count(msgs) == 5


def test_live_message_count_handles_empty_and_non_archive(dc):
    assert dc.live_message_count([]) == 0
    assert dc.live_message_count(None) == 0
    # tool message with the archive marker but no [pN] id is NOT one of ours —
    # be conservative and count it as live (don't false-positive on prose).
    msgs = [
        {"role": "tool", "content": "user said (archived: not a real placeholder)"},
        {"role": "assistant", "content": "(archived: just text)"},
        {"role": "user", "content": "(archived: still just text)"},
    ]
    assert dc.live_message_count(msgs) == 3
