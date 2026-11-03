"""Read guard: a PreToolUse(Read) hook that stops re-reading unchanged files within one chat."""
import io
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from memgraph import readguard
from memgraph.cli import main

T0 = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.delenv("SESSION_MEMORY_READ_GUARD", raising=False)
    p = tmp_path / "proj"
    p.mkdir()
    f = p / "app.py"
    f.write_text("print('hi')\n" * 10, encoding="utf-8")
    return p


def payload(proj, file=None, session="s1", **tool):
    tool_input = {"file_path": str(file or proj / "app.py"), **tool}
    return {"session_id": session, "transcript_path": str(proj / "t.jsonl"), "cwd": str(proj),
            "hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": tool_input}


def hook(data, now=T0, name="hook-pre-read"):
    out = io.StringIO()
    text = data if isinstance(data, str) else json.dumps(data)
    rc = main([name], io.StringIO(text), out, now=now)
    assert rc == 0
    return out.getvalue()


def denied(text):
    if not text:
        return False
    obj = json.loads(text)["hookSpecificOutput"]
    assert obj["hookEventName"] == "PreToolUse"
    assert obj["permissionDecision"] == "deny"
    return obj["permissionDecisionReason"]


def bump_mtime(path):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_first_read_allowed_then_unchanged_reread_denied(proj):
    assert hook(payload(proj)) == ""
    reason = denied(hook(payload(proj), T0 + timedelta(minutes=5)))
    assert reason.startswith("Not re-read: ")
    assert "app.py is unchanged since you read it at 09:30 in this chat" in reason
    assert "read it again within 2 minutes" in reason


def test_edited_file_allowed(proj):
    hook(payload(proj))
    (proj / "app.py").write_text("changed and longer\n" * 20, encoding="utf-8")
    assert hook(payload(proj), T0 + timedelta(minutes=1)) == ""


def test_mtime_only_change_allowed(proj):
    hook(payload(proj))
    bump_mtime(proj / "app.py")
    assert hook(payload(proj), T0 + timedelta(minutes=1)) == ""


def test_range_covered_denied_other_range_allowed(proj):
    hook(payload(proj, offset=1, limit=50))
    assert denied(hook(payload(proj, offset=10, limit=5), T0 + timedelta(minutes=1)))
    assert hook(payload(proj, offset=40, limit=50), T0 + timedelta(minutes=2)) == ""
    # 1..90 is now covered by the two recorded (merged) ranges
    assert denied(hook(payload(proj, offset=30, limit=40), T0 + timedelta(minutes=3)))


def test_whole_read_covers_partial_but_partial_does_not_cover_whole(proj):
    hook(payload(proj, offset=1, limit=5))
    assert hook(payload(proj), T0 + timedelta(minutes=1)) == ""
    assert denied(hook(payload(proj, offset=2, limit=3), T0 + timedelta(minutes=2)))


def test_after_compaction_allowed(proj):
    hook(payload(proj))
    compact = {"session_id": "s1", "transcript_path": str(proj / "t.jsonl"), "cwd": str(proj),
               "hook_event_name": "PreCompact", "trigger": "auto"}
    assert hook(compact, T0 + timedelta(minutes=1), name="hook-compact") == ""
    assert hook(payload(proj), T0 + timedelta(minutes=2)) == ""
    assert denied(hook(payload(proj), T0 + timedelta(minutes=3)))


def test_session_start_compact_bumps_epoch(proj):
    hook(payload(proj))
    start = {"session_id": "s1", "transcript_path": str(proj / "t.jsonl"), "cwd": str(proj),
             "hook_event_name": "SessionStart", "source": "compact"}
    hook(start, T0 + timedelta(minutes=1), name="hook-compact")
    assert hook(payload(proj), T0 + timedelta(minutes=2)) == ""


def test_retry_within_two_minutes_allowed_then_denied_again(proj):
    hook(payload(proj))
    assert denied(hook(payload(proj), T0 + timedelta(minutes=5)))
    assert hook(payload(proj), T0 + timedelta(minutes=6)) == ""  # 60 s later: escape hatch
    assert denied(hook(payload(proj), T0 + timedelta(minutes=10)))


def test_retry_after_two_minutes_still_denied(proj):
    hook(payload(proj))
    assert denied(hook(payload(proj), T0 + timedelta(minutes=5)))
    assert denied(hook(payload(proj), T0 + timedelta(minutes=8)))


def test_other_session_and_subagent_allowed(proj):
    hook(payload(proj))
    assert hook(payload(proj, session="s2"), T0 + timedelta(minutes=1)) == ""
    sub = payload(proj)
    sub["agent_id"] = "agent-1"
    assert hook(sub, T0 + timedelta(minutes=1)) == ""
    assert denied(hook(sub, T0 + timedelta(minutes=2)))


def test_env_off_allows(proj, monkeypatch):
    hook(payload(proj))
    monkeypatch.setenv("SESSION_MEMORY_READ_GUARD", "0")
    assert hook(payload(proj), T0 + timedelta(minutes=1)) == ""


@pytest.mark.parametrize("bad", ["", "not json", "[]", '{"cwd": 5}', '{"tool_input": "x"}'])
def test_bad_stdin_allows_silently(bad):
    assert hook(bad) == ""


def test_missing_file_and_huge_file_allowed(proj):
    missing = proj / "nope.py"
    assert hook(payload(proj, file=missing)) == ""
    assert hook(payload(proj, file=missing), T0 + timedelta(minutes=1)) == ""
    big = proj / "big.bin"
    with open(big, "wb") as fh:
        fh.truncate(readguard.MAX_BYTES + 1)
    assert hook(payload(proj, file=big)) == ""
    assert hook(payload(proj, file=big), T0 + timedelta(minutes=1)) == ""


def test_large_whole_read_not_recorded_but_ranges_are(proj):
    f = proj / "large.txt"
    f.write_text("x" * (readguard.WHOLE_READ_MAX_BYTES + 10), encoding="utf-8")
    hook(payload(proj, file=f))
    assert hook(payload(proj, file=f), T0 + timedelta(minutes=1)) == ""  # may have failed as too large
    hook(payload(proj, file=f, offset=1, limit=10), T0 + timedelta(minutes=2))
    assert denied(hook(payload(proj, file=f, offset=1, limit=10), T0 + timedelta(minutes=3)))


def test_pdf_pages_not_guarded(proj):
    hook(payload(proj, pages="1-2"))
    assert hook(payload(proj, pages="1-2"), T0 + timedelta(minutes=1)) == ""


def test_relative_and_differently_spelled_path_same_file(proj):
    hook(payload(proj))
    rel = payload(proj)
    rel["tool_input"]["file_path"] = "app.py"
    assert denied(hook(rel, T0 + timedelta(minutes=1)))


def test_norm_path_windows_case_and_slashes():
    assert readguard.norm_path(r"C:\a\b.py", nt=True) == readguard.norm_path("c:/a/b.py", nt=True)
    assert readguard.norm_path("/a/B.py", nt=False) != readguard.norm_path("/a/b.py", nt=False)
    assert readguard.norm_path("/a/./x/../b.py", nt=False) == "/a/b.py"


def test_state_kept_in_reads_json_and_pruned(proj):
    hook(payload(proj))
    data = json.loads((proj / ".claude" / "session-memory" / "reads.json").read_text(encoding="utf-8"))
    assert "s1" in data["scopes"]
    for i in range(readguard.MAX_SCOPES + 5):
        hook(payload(proj, session=f"x{i}"), T0 + timedelta(seconds=i + 1))
    data = json.loads((proj / ".claude" / "session-memory" / "reads.json").read_text(encoding="utf-8"))
    assert len(data["scopes"]) <= readguard.MAX_SCOPES
    assert "s1" not in data["scopes"]  # oldest dropped


def test_hooks_json_registers_read_guard_and_compaction():
    from pathlib import Path

    hooks = json.loads((Path(readguard.__file__).resolve().parent.parent / "hooks" / "hooks.json")
                       .read_text(encoding="utf-8"))["hooks"]
    pre = hooks["PreToolUse"]
    assert any(e.get("matcher") == "Read" and "hook-pre-read" in e["hooks"][0]["command"] for e in pre)
    assert any("hook-compact" in e["hooks"][0]["command"] for e in hooks["PreCompact"])
    assert any(e.get("matcher") == "compact" and "hook-compact" in e["hooks"][0]["command"]
               for e in hooks["SessionStart"])
    assert any(e.get("matcher") == "startup|clear" and "hook-session-start" in e["hooks"][0]["command"]
               for e in hooks["SessionStart"])
