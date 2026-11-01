import memgraph.guard as guard_mod
from datetime import datetime, timedelta, timezone

from memgraph.guard import GuardConfig, decide, last_activity
from tests.helpers import FIXTURE

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def test_blocks_idle_and_large():
    r = decide(NOW, NOW - timedelta(minutes=90), 140000, "continue", {}, GuardConfig())
    assert r.startswith("Cache guard: this chat has been idle for 90 minutes") and "140,000 tokens" in r


def test_reason_text_is_exact():
    r = decide(NOW, NOW - timedelta(minutes=90, seconds=59), 140000, "continue", {}, GuardConfig())
    assert r == (
        "Cache guard: this chat has been idle for 90 minutes, so its prompt cache has likely expired. "
        "Sending now re-reads about 140,000 tokens at full price.\n"
        "Cheaper: type /clear. Your project memory loads automatically as a short brief.\n"
        "To send anyway, send the same message again within 10 minutes."
    )


def test_allows_short_idle():
    assert decide(NOW, NOW - timedelta(minutes=20), 140000, "go", {}, GuardConfig()) is None


def test_allows_exactly_at_idle_threshold():
    assert decide(NOW, NOW - timedelta(minutes=55), 140000, "go", {}, GuardConfig()) is None


def test_allows_small_chat():
    assert decide(NOW, NOW - timedelta(hours=5), 12000, "go", {}, GuardConfig()) is None


def test_allows_exactly_at_token_threshold():
    assert decide(NOW, NOW - timedelta(hours=5), 30000, "go", {}, GuardConfig()) is None


def test_allows_slash_commands():
    assert decide(NOW, NOW - timedelta(hours=5), 140000, "/clear", {}, GuardConfig()) is None


def test_same_message_again_goes_through():
    s = {}; cfg = GuardConfig(); old = NOW - timedelta(hours=3)
    assert decide(NOW, old, 140000, "continue", s, cfg) is not None
    assert decide(NOW + timedelta(minutes=2), old, 140000, " continue ", s, cfg) is None


def test_block_records_state_and_allowed_repeat_clears_it():
    s = {}; cfg = GuardConfig(); old = NOW - timedelta(hours=3)
    decide(NOW, old, 140000, "continue", s, cfg)
    assert set(s["guard"]) == {"hash", "at"} and s["guard"]["at"] == NOW.isoformat()
    decide(NOW + timedelta(minutes=2), old, 140000, "continue", s, cfg)
    assert "guard" not in s and decide(NOW + timedelta(minutes=3), old, 140000, "continue", s, cfg) is not None


def test_different_message_blocks_again():
    s = {}; cfg = GuardConfig(); old = NOW - timedelta(hours=3)
    decide(NOW, old, 140000, "continue", s, cfg)
    assert decide(NOW + timedelta(minutes=1), old, 140000, "something else", s, cfg) is not None


def test_repeat_after_window_blocks_again():
    s = {}; cfg = GuardConfig(); old = NOW - timedelta(hours=3)
    decide(NOW, old, 140000, "continue", s, cfg)
    assert decide(NOW + timedelta(minutes=11), old, 140000, "continue", s, cfg) is not None


def test_garbage_guard_state_is_treated_as_empty():
    old = NOW - timedelta(hours=3)
    for bad in ("x", {"hash": 1, "at": "nope"}, {}, None):
        assert decide(NOW, old, 140000, "continue", {"guard": bad}, GuardConfig()) is not None


def test_naive_now_is_treated_as_utc():
    naive = NOW.replace(tzinfo=None)
    r = decide(naive, NOW - timedelta(minutes=90), 140000, "continue", {}, GuardConfig())
    assert r is not None and "idle for 90 minutes" in r


def test_missing_data_allows():                               # Review Focus 3
    assert decide(NOW, None, 140000, "go", {}, GuardConfig()) is None
    assert decide(NOW, NOW - timedelta(hours=3), None, "go", {}, GuardConfig()) is None


def test_last_activity_from_fixture():
    ts, ctx = last_activity(FIXTURE)
    assert ts == datetime(2026, 11, 1, 10, 0, 30, tzinfo=timezone.utc) and ctx == 42010


def test_last_activity_missing_file(tmp_path):
    assert last_activity(tmp_path / "nope.jsonl") == (None, None)


def test_last_activity_skips_a_line_that_raises(tmp_path, monkeypatch):    # R5
    f = tmp_path / "t.jsonl"
    f.write_text(
        '{"type":"assistant","timestamp":"2026-11-01T10:00:00.000Z","message":{"usage":{"input_tokens":7}}}\n'
        '{"type":"assistant","timestamp":"2026-11-01T10:05:00.000Z","message":{"usage":{"input_tokens":9}}}\n',
        encoding="utf-8",
    )
    real = guard_mod.parse_line

    def flaky(line):
        if "10:05:00" in line:
            raise RuntimeError("boom")
        return real(line)

    monkeypatch.setattr(guard_mod, "parse_line", flaky)
    ts, ctx = last_activity(f)
    assert ts == datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc) and ctx == 7


def test_last_activity_reads_only_tail_and_drops_partial_line(tmp_path):
    f = tmp_path / "big.jsonl"
    old = '{"type":"assistant","timestamp":"2026-11-01T09:00:00.000Z","message":{"usage":{"input_tokens":5}}}\n'
    pad = '{"type":"system","content":"' + "x" * 1000 + '"}\n'
    new = '{"type":"assistant","timestamp":"2026-11-01T10:00:00.000Z","message":{"usage":{"input_tokens":8}}}\n'
    f.write_text(old + pad * 400 + new, encoding="utf-8")        # > 256 KB; old line is outside the tail
    ts, ctx = last_activity(f)
    assert ts == datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc) and ctx == 8


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("SESSION_MEMORY_IDLE_MIN", "30"); monkeypatch.setenv("SESSION_MEMORY_MIN_TOKENS", "abc")
    cfg = GuardConfig.from_env()
    assert (cfg.idle_minutes, cfg.min_tokens) == (30, 30000)
