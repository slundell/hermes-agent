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
    assert dc.block_id("[b37] some tool result") == "b37"
    assert dc.block_id("  [b1] leading space ok") == "b1"
    assert dc.block_id("no id here") is None
    assert dc.block_id("[archived] not a block id") is None


def test_content_str_flattens_list_and_string(dc):
    assert dc.content_str({"content": "plain"}) == "plain"
    assert dc.content_str({"content": [{"text": "a"}, {"text": "b"}]}) == "a b"
    assert dc.content_str({"content": None}) == ""


def test_is_stamped(dc):
    assert dc.is_stamped("[b9] x") is True
    assert dc.is_stamped("x") is False


def test_block_ids_on_desk(dc):
    msgs = [
        {"role": "tool", "content": "[b3] r3"},
        {"role": "assistant", "content": "[b99] not a tool — ignored"},
        {"role": "tool", "content": "[b1] r1"},
        {"role": "tool", "content": "no id"},
    ]
    assert dc.block_ids_on_desk(msgs) == [1, 3]


def test_live_block_ids_on_desk_excludes_placeholders(dc):
    msgs = [
        {"role": "tool", "content": "[b5] live content"},
        {"role": "tool", "content": "[b6] (archived: 1234 chars moved off the desk — recall b6 to bring it back)"},
        {"role": "tool", "content": "[b7] another live block"},
        {"role": "tool", "content": "[b8] (archived: 99 chars moved off the desk — recall b8 to bring it back)"},
    ]
    # full view (used by the rollback diagnostic) sees everything
    assert dc.block_ids_on_desk(msgs) == [5, 6, 7, 8]
    # live view (used by the desk-note's "ids on the desk" listing) hides
    # placeholders so the model isn't misled into treating them as slots
    assert dc.live_block_ids_on_desk(msgs) == [5, 7]


def test_collapse_ranges(dc):
    assert dc.collapse_ranges([1, 2, 3, 5, 6]) == "b1–b3, b5, b6"
    assert dc.collapse_ranges([4]) == "b4"
    assert dc.collapse_ranges([]) == ""


def test_next_block_id_is_monotonic(dc):
    first = dc.next_block_id()
    assert dc.next_block_id() == first + 1
    assert dc.next_block_id() == first + 2


def test_effective_ctx_is_window_minus_headroom(dc):
    assert dc.EFFECTIVE_CTX == dc.DESK_MODEL_MAX_CTX - dc.FORCED_HEADROOM_TOKENS


def test_level_for_bands(dc):
    # fractions of EFFECTIVE_CTX: notice 0.80, urgent 0.90, forced 1.00
    assert dc.level_for(0.50) == "calm"
    assert dc.level_for(0.85) == "notice"
    assert dc.level_for(0.95) == "urgent"
    assert dc.level_for(1.05) == "forced"


def test_level_for_hysteresis_holds_band_on_exit(dc):
    # dropping just below urgent's entry (0.90) holds at urgent within HYSTERESIS
    assert dc.level_for(0.88, prev="urgent") == "urgent"
    # dropping well below it releases to notice
    assert dc.level_for(0.82, prev="urgent") == "notice"


def test_log_overflow_writes_loud_diagnostic(dc, tmp_path):
    dc.log_overflow(("b42", 123456), n_messages=80)
    log = tmp_path / "desk" / "overflow.log"
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert "DESK OVERFLOW" in text
    assert "b42" in text


def test_fill_fraction(dc):
    assert dc.fill_fraction(dc.EFFECTIVE_CTX) == pytest.approx(1.0)
    assert dc.fill_fraction(0) == 0.0


def test_log_overflow_handles_none_offending(dc, tmp_path):
    # a real overflow may have no identified block — must not crash
    dc.log_overflow(None, n_messages=12)
    log = tmp_path / "desk" / "overflow.log"
    assert log.exists()
    assert "DESK OVERFLOW" in log.read_text(encoding="utf-8")


def test_next_block_id_returns_value_even_if_persist_fails(dc, monkeypatch):
    first = dc.next_block_id()
    # simulate the counter file write failing
    from pathlib import Path
    monkeypatch.setattr(
        Path, "write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    # the incremented value is still returned (in-memory), write failure is logged
    assert dc.next_block_id() == first + 1
