import json
import os

import pytest

from memgraph.savings import EVENTS_FILE, append_event, format_savings, measure_savings, read_events
from tests.test_usage import line


def test_append_and_read_events_tolerate_partial_lines(tmp_path):
    append_event(tmp_path, {"type": "guard_block", "session": "a", "context": 5})
    with open(tmp_path / EVENTS_FILE, "a", encoding="utf-8") as fh:
        fh.write('{"type": "fresh_st')  # a write cut off half way
    append_event(tmp_path, {"type": "fresh_start", "session": "b"})
    events = read_events(tmp_path)
    assert [e["type"] for e in events] == ["guard_block"]  # the cut line swallows the next, never raises
    assert all("t" in e for e in events)
    assert read_events(tmp_path / "missing") == []


def test_append_event_is_one_line_each(tmp_path):
    for i in range(3):
        append_event(tmp_path, {"type": "guard_block", "session": str(i), "context": i})
    text = (tmp_path / EVENTS_FILE).read_text(encoding="utf-8")
    assert text.count("\n") == 3 and len(read_events(tmp_path)) == 3


def _write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


W = {"input_tokens": 3, "cache_creation_input_tokens": 120000, "cache_read_input_tokens": 0, "output_tokens": 50,
     "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 120000}}
WARM = {"input_tokens": 3, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 119000, "output_tokens": 50}
FIRST_NEW = {"input_tokens": 4, "cache_creation_input_tokens": 6000, "cache_read_input_tokens": 14000, "output_tokens": 70}


def seed(tmp_path):
    t = tmp_path / "t"
    (t / "old" / "subagents").mkdir(parents=True)
    _write(t / "old.jsonl", [
        line("2026-11-01T09:00:00Z", "m1", "r1", W, session="old"),
        line("2026-11-01T09:01:00Z", "m2", "r2", WARM, session="old"),
        line("2026-11-01T11:01:00Z", "m3", "r3", W, session="old"),  # 2 hours idle: cold rebuild
        line("2026-11-01T11:01:00Z", "m3", "r3", W, session="old"),  # streaming duplicate
    ])
    _write(t / "old" / "subagents" / "agent-x.jsonl", [  # helper gaps never count
        line("2026-11-01T09:00:00Z", "h1", "hr1", W, session="old", sidechain=True),
        line("2026-11-01T12:00:00Z", "h2", "hr2", W, session="old", sidechain=True),
    ])
    _write(t / "new.jsonl", [
        line("2026-11-01T11:30:05Z", "n1", "nr1", FIRST_NEW, session="new"),
        line("2026-11-01T11:31:00Z", "n2", "nr2", WARM, session="new"),
    ])
    mem = tmp_path / "mem"
    mem.mkdir()
    append_event(mem, {"type": "fresh_start", "session": "new", "brief_chars": 4000,
                       "prev_session": "old", "prev_context": 120003})
    append_event(mem, {"type": "fresh_start", "session": "ghost", "brief_chars": 4000,
                       "prev_session": "old", "prev_context": None})
    append_event(mem, {"type": "guard_block", "session": "old", "context": 120003})
    return t, mem


def test_measure_savings_fresh_starts_are_measured(tmp_path):
    t, mem = seed(tmp_path)
    res = measure_savings(t, mem, ttl_minutes=60)
    (row,) = res["fresh_starts"]
    assert row["prev_context"] == 120003
    assert row["actual"] == 4 + 6000 + 14000
    assert row["saved"] == 120003 - 20004
    assert res["totals"]["measured_saved_tokens"] == 120003 - 20004
    # priced as a cache rebuild of the avoided tokens: 1.25 x $4 per million
    assert res["totals"]["measured_saved_usd"] == pytest.approx((120003 - 20004) * 5.0 / 1e6)
    assert res["totals"]["fresh_starts"] == 1 and res["totals"]["guard_blocks"] == 1


def test_measure_savings_cold_restarts_are_estimates(tmp_path):
    t, mem = seed(tmp_path)
    res = measure_savings(t, mem, ttl_minutes=60)
    (row,) = res["cold_restarts"]
    assert row["session"] == "old" and row["idle_minutes"] == 120
    assert row["rebuild_tokens"] == 120000
    assert row["with_plugin_estimate"] == 1000  # latest brief 4000 chars // 4
    assert row["could_have_saved_estimate"] == 119000
    assert res["totals"]["missed_estimate_tokens"] == 119000
    # rebuild was a 1h write: 2 x $4 per million; the estimate share of it
    assert row["rebuild_usd"] == pytest.approx(120000 * 8.0 / 1e6)
    assert res["totals"]["missed_estimate_usd"] == pytest.approx(119000 * 8.0 / 1e6)
    assert res["totals"]["cold_restarts"] == 1


def test_measure_savings_without_events_uses_500_token_estimate(tmp_path):
    t, mem = seed(tmp_path)
    os.remove(mem / EVENTS_FILE)
    res = measure_savings(t, mem, ttl_minutes=60)
    assert res["fresh_starts"] == [] and res["totals"]["measured_saved_tokens"] == 0
    assert res["cold_restarts"][0]["with_plugin_estimate"] == 500


def test_saved_never_below_zero(tmp_path):
    t, mem = seed(tmp_path)
    os.remove(mem / EVENTS_FILE)
    append_event(mem, {"type": "fresh_start", "session": "new", "brief_chars": 10, "prev_session": "old",
                       "prev_context": 100})
    assert measure_savings(t, mem, 60)["fresh_starts"][0]["saved"] == 0


def test_format_savings_keeps_measured_and_estimate_apart(tmp_path):
    t, mem = seed(tmp_path)
    text = format_savings(measure_savings(t, mem, 60))
    assert "Measured" in text and "estimate" in text
    assert "prices as of 2026-10-06" in text
    assert text.index("Measured") < text.index("estimate")
    json.dumps(measure_savings(t, mem, 60))  # JSON-able
