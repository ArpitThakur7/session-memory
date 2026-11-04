import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memgraph import usage
from memgraph.usage import PRICES_AS_OF, collect_usage, price_for, reply_cost, summarize, totals


def line(ts, mid, rid, usage_obj, session="m1", model="claude-opus-5-5", sidechain=False, **extra):
    obj = {"type": "assistant", "sessionId": session, "timestamp": ts, "requestId": rid,
           "message": {"id": mid, "role": "assistant", "model": model,
                       "content": [{"type": "text", "text": "ok"}], "usage": usage_obj}}
    if sidechain:
        obj["isSidechain"] = True
    obj.update(extra)
    return json.dumps(obj)


U1 = {"input_tokens": 10, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 0,
      "output_tokens": 100, "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 2000}}
U2 = {"input_tokens": 5, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 3000, "output_tokens": 40}
U_SUB = {"input_tokens": 7, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 900, "output_tokens": 9}


def write_dir(tmp_path):
    d = tmp_path / "t"
    (d / "m1" / "subagents").mkdir(parents=True)
    main = [
        json.dumps({"type": "user", "sessionId": "m1", "timestamp": "2026-11-01T10:00:00Z",
                    "message": {"role": "user", "content": "hi"}}),
        line("2026-11-01T10:00:05Z", "msg_1", "req_1", U1),
        line("2026-11-01T10:00:06Z", "msg_1", "req_1", U1),  # streaming duplicate
        line("2026-11-01T10:00:07Z", "msg_1", "req_1", U1),  # streaming duplicate
        # synthetic notice: never counted
        line("2026-11-01T10:00:08Z", "msg_s", "req_s",
             {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0},
             model="<synthetic>"),
        line("2026-11-01T12:00:10Z", "msg_2", "req_2", U2),  # 2 hours later
        "{not json",
        json.dumps({"type": "assistant", "sessionId": "m1", "message": {"usage": "odd"}}),
        line("2026-11-01T12:00:20Z", "msg_3", "req_3", U2, model="claude-mystery-9"),
    ]
    (d / "m1.jsonl").write_text("\n".join(main) + "\n", encoding="utf-8")
    sub = [line("2026-11-01T10:00:30Z", "msg_a", "req_a", U_SUB, sidechain=True, model="claude-haiku-4-5-20251001")]
    (d / "m1" / "subagents" / "agent-a1.jsonl").write_text("\n".join(sub) + "\n", encoding="utf-8")
    return d


def test_collect_usage_dedupes_skips_synthetic_and_reads_subagents(tmp_path):
    res = collect_usage(write_dir(tmp_path))
    assert res.duplicates == 2
    assert len(res.replies) == 4
    first = res.replies[0]
    assert (first.input, first.cache_write_5m, first.cache_write_1h, first.cache_write_unknown,
            first.cache_read, first.output) == (10, 1000, 2000, 0, 0, 100)
    assert first.ts == datetime(2026, 11, 1, 10, 0, 5, tzinfo=timezone.utc)
    assert first.session == "m1" and first.model == "claude-opus-5-5" and first.helper is False
    second = [r for r in res.replies if r.ts.hour == 12][0]
    assert second.cache_write_unknown == 200 and second.cache_write_5m == 0
    helpers = [r for r in res.replies if r.helper]
    assert len(helpers) == 1 and helpers[0].cache_read == 900


def test_collect_usage_missing_dir_is_empty(tmp_path):
    res = collect_usage(tmp_path / "nope")
    assert res.replies == [] and res.duplicates == 0


def test_price_lookup_by_prefix_and_unknown():
    assert price_for("claude-opus-5-5")["input"] == 4.00
    assert price_for("claude-haiku-4-5-20251001")["output"] == 5.00
    assert price_for("claude-haiku-5-5")["cache_read"] == pytest.approx(0.01)
    assert price_for("claude-opus-4-6")["cache_read"] == pytest.approx(0.5)
    assert price_for("claude-mystery-9") is None
    assert price_for("") is None
    assert PRICES_AS_OF == "2026-10-06"


def test_reply_cost_uses_cache_write_multipliers(tmp_path):
    first = collect_usage(write_dir(tmp_path)).replies[0]
    # 10*4 + 1000*5 + 2000*8 + 0 + 100*20 = 40 + 5000 + 16000 + 2000 = 23040 per million
    assert reply_cost(first) == pytest.approx(23040 / 1e6)


def test_totals_and_cache_hit_rate(tmp_path):
    t = totals(collect_usage(write_dir(tmp_path)).replies)
    assert t["replies"] == 4
    assert t["input"] == 10 + 5 + 5 + 7
    assert t["cache_read"] == 3000 + 3000 + 900
    assert t["cache_write"] == 3000 + 200 + 200
    assert t["output"] == 100 + 40 + 40 + 9
    assert t["cache_hit_rate"] == pytest.approx(6900 / (27 + 3400 + 6900))
    assert t["cost_partial"] is True and "claude-mystery-9" in t["unknown_models"]
    assert t["cost"] > 0


def test_summarize_by_day_model_who_session_and_since(tmp_path):
    replies = collect_usage(write_dir(tmp_path)).replies
    by_day = summarize(replies, "day")
    assert [r["key"] for r in by_day] == ["2026-11-01"]
    assert by_day[0]["replies"] == 4
    by_model = {r["key"]: r for r in summarize(replies, "model")}
    assert by_model["claude-mystery-9"]["cost"] is None
    assert by_model["claude-opus-5-5"]["cost_partial"] is False
    who = {r["key"]: r["replies"] for r in summarize(replies, "who")}
    assert who == {"main": 3, "helper": 1}
    assert [r["key"] for r in summarize(replies, "session")] == ["m1"]
    later = summarize(replies, "day", since=datetime(2026, 11, 1, 11, 0, tzinfo=timezone.utc))
    assert later[0]["replies"] == 2


def test_summarize_rejects_unknown_grouping(tmp_path):
    with pytest.raises(ValueError):
        summarize([], "planet")


def test_transcripts_dir_from_state_or_slug(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    mem.mkdir()
    monkeypatch.setattr(usage.Path, "home", lambda: tmp_path / "home")
    project = tmp_path / "My Proj.x"
    got = usage.transcripts_dir_for(project, mem)
    slug = "".join(c if c.isalnum() else "-" for c in str(project.resolve()))
    assert got == tmp_path / "home" / ".claude" / "projects" / slug
    (tmp_path / "t").mkdir()
    (mem / "state.json").write_text(json.dumps({"transcripts_dir": str(tmp_path / "t")}), encoding="utf-8")
    assert usage.transcripts_dir_for(project, mem) == tmp_path / "t"


def test_transcripts_dir_ignores_stale_recorded_dirs(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    mem.mkdir()
    monkeypatch.setattr(usage.Path, "home", lambda: tmp_path / "home")
    project = tmp_path / "proj"
    slug = usage.project_slug(project)
    (mem / "state.json").write_text(json.dumps({"transcripts_dir": str(tmp_path / "gone")}), encoding="utf-8")
    (mem / "guard.json").write_text(json.dumps({"transcripts_dir": str(tmp_path / "gone2")}), encoding="utf-8")
    assert usage.transcripts_dir_for(project, mem) == tmp_path / "home" / ".claude" / "projects" / slug
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert usage.transcripts_dir_for(project, mem) == tmp_path / "cfg" / "projects" / slug
    (tmp_path / "live").mkdir()
    (mem / "guard.json").write_text(json.dumps({"transcripts_dir": str(tmp_path / "live")}), encoding="utf-8")
    assert usage.transcripts_dir_for(project, mem) == tmp_path / "live"  # state.json is stale, guard.json is not


@pytest.mark.skipif(os.name != "nt", reason="a drive-letter path is only absolute on Windows")
def test_project_slug_is_ascii_only():
    from pathlib import PureWindowsPath

    class FakePath(PureWindowsPath):
        def resolve(self):
            return self

    assert usage.project_slug(FakePath(r"C:\Users\José\proj_é")) == "C--Users-Jos--proj--"


@pytest.mark.skipif(os.name == "nt", reason="POSIX path form")
def test_project_slug_is_ascii_only_posix():
    assert usage.project_slug(Path("/home/José/proj_é")) == "-home-Jos--proj--"


def test_helper_descriptions_are_redacted(tmp_path):
    secret = "ghp_" + "a1B2c3D4e5" * 4
    sub = tmp_path / "sess" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x.meta.json").write_text(json.dumps({"description": f"Deploy with token {secret} now"}),
                                           encoding="utf-8")
    got = usage.helper_descriptions(tmp_path)["agent-x"]
    assert secret not in got and got.startswith("Deploy with token ") and got.endswith(" now")
