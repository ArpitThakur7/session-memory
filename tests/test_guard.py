import json

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


def _assistant(ts, tokens):
    return (
        '{"type":"assistant","timestamp":"%s","message":{"usage":{"input_tokens":%d}}}'
        % (ts, tokens)
    )


def test_last_activity_reads_only_the_last_256kb(tmp_path):
    f = tmp_path / "big.jsonl"
    head = _assistant("2026-11-01T11:00:00.000Z", 99)        # newer, but > 256 KB before the end
    pad = '{"type":"system","content":"' + "x" * 1000 + '"}\n'
    tail = _assistant("2026-11-01T10:00:00.000Z", 8)
    f.write_text(head + "\n" + pad * 400 + tail + "\n", encoding="utf-8")
    assert f.stat().st_size > 256 * 1024
    ts, ctx = last_activity(f)
    assert ts == datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc) and ctx == 8


def test_last_activity_drops_the_partial_first_line_of_the_tail(tmp_path):
    f = tmp_path / "cut.jsonl"
    newer = _assistant("2026-11-01T11:00:00.000Z", 99)
    older = _assistant("2026-11-01T10:00:00.000Z", 8)
    json.loads(newer)                                         # the fragment below is a valid line on its own
    junk = "x" * 5000
    # Size the pad so the 256 KB cut lands exactly on the '{' of `newer`, which sits mid-line after `junk`.
    pad_len = 256 * 1024 - len(newer) - len(older) - 3
    shell = '{"type":"system","content":""}'
    pad = '{"type":"system","content":"' + "y" * (pad_len - len(shell)) + '"}'
    data = junk + newer + "\n" + pad + "\n" + older + "\n"
    f.write_bytes(data.encode("utf-8"))
    assert len(data) - 256 * 1024 == len(junk)
    ts, ctx = last_activity(f)
    assert ts == datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc) and ctx == 8


def test_last_activity_survives_unicode_line_separators_in_content(tmp_path):
    f = tmp_path / "u2028.jsonl"
    newest = json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-11-01T11:00:00.000Z",
            "message": {"content": [{"type": "text", "text": "a b\x85c\x0cd"}], "usage": {"input_tokens": 77}},
        },
        ensure_ascii=False,
    )
    older = _assistant("2026-11-01T10:00:00.000Z", 8)
    f.write_bytes((newest + "\n" + older + "\n").encode("utf-8"))
    ts, ctx = last_activity(f)
    assert ts == datetime(2026, 11, 1, 11, 0, tzinfo=timezone.utc) and ctx == 8


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("SESSION_MEMORY_IDLE_MIN", "30"); monkeypatch.setenv("SESSION_MEMORY_MIN_TOKENS", "abc")
    cfg = GuardConfig.from_env()
    assert (cfg.idle_minutes, cfg.min_tokens) == (30, 30000)


def _line(**obj):
    return json.dumps(obj)


REAL = _line(type="assistant", timestamp="2026-11-01T10:00:00.000Z",
             message={"model": "claude-opus", "usage": {"input_tokens": 3, "cache_read_input_tokens": 140000}})
SYNTHETIC_LINES = [
    _line(type="assistant", timestamp="2026-11-01T11:50:00.000Z",
          message={"model": "<synthetic>", "content": [{"type": "text", "text": "x"}],
                   "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0}}),
    _line(type="assistant", timestamp="2026-11-01T11:50:00.000Z", isApiErrorMessage=True,
          message={"model": "claude-opus", "usage": {"input_tokens": 9}}),
    _line(type="assistant", timestamp="2026-11-01T11:50:00.000Z",
          message={"model": "claude-opus", "usage": {"input_tokens": 0, "output_tokens": 4}}),
]


def test_last_activity_ignores_synthetic_lines(tmp_path):             # C1
    for synthetic in SYNTHETIC_LINES:
        f = tmp_path / "t.jsonl"
        f.write_text(REAL + "\n" + synthetic + "\n", encoding="utf-8")
        assert last_activity(f) == (datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc), 140003)


def test_guard_fires_after_a_usage_limit_message(tmp_path):            # C1
    f = tmp_path / "t.jsonl"
    f.write_text(REAL + "\n" + SYNTHETIC_LINES[0] + "\n", encoding="utf-8")
    last_ts, ctx = last_activity(f)
    assert decide(NOW, last_ts, ctx, "continue", {}, GuardConfig()) is not None


def test_allows_prompts_starting_with_angle_bracket():                 # I4
    old = NOW - timedelta(hours=5)
    for p in ("<task-notification>done</task-notification>", "  \n<system-reminder>x"):
        assert decide(NOW, old, 140000, p, {}, GuardConfig()) is None


COMPACT = _line(type="system", subtype="compact_boundary", timestamp="2026-11-01T10:30:00.000Z", content="c")


def test_compact_boundary_resets_context_and_counts_as_activity(tmp_path):   # I5
    f = tmp_path / "t.jsonl"
    f.write_text(REAL + "\n" + COMPACT + "\n", encoding="utf-8")
    last_ts, ctx = last_activity(f)
    assert (last_ts, ctx) == (datetime(2026, 11, 1, 10, 30, tzinfo=timezone.utc), None)
    assert decide(NOW, last_ts, ctx, "continue", {}, GuardConfig()) is None


def test_reply_after_compact_boundary_sets_context_again(tmp_path):    # I5
    after = _line(type="assistant", timestamp="2026-11-01T10:31:00.000Z", message={"usage": {"input_tokens": 20000}})
    f = tmp_path / "t.jsonl"
    f.write_text(REAL + "\n" + COMPACT + "\n" + after + "\n", encoding="utf-8")
    assert last_activity(f) == (datetime(2026, 11, 1, 10, 31, tzinfo=timezone.utc), 20000)
