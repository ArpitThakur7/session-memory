import pytest
import io
import json
import shutil
from datetime import datetime, timezone
from unittest import mock

from memgraph.cli import main
from tests.helpers import FIXTURE


def hook_input(tmp_path, prompt=None, transcripts=True, source="startup"):
    transcripts_dir = tmp_path / "transcripts"
    if transcripts:
        transcripts_dir.mkdir()
        shutil.copyfile(FIXTURE, transcripts_dir / FIXTURE.name)
    cwd = tmp_path / "proj"
    cwd.mkdir(exist_ok=True)
    data = {
        "session_id": "s1",
        "transcript_path": str(transcripts_dir / FIXTURE.name),
        "cwd": str(cwd),
        "hook_event_name": "Hook",
        "source": source,
    }
    if prompt is not None:
        data["prompt"] = prompt
    return json.dumps(data)


def run_rc(argv, stdin_text, now=None, min_tokens=None):
    out = io.StringIO()
    env = {"SESSION_MEMORY_MIN_TOKENS": str(min_tokens)} if min_tokens is not None else {}
    with mock.patch.dict("os.environ", env):
        rc = main(argv, io.StringIO(stdin_text), out, now=now)
    return rc, out.getvalue()


def run(argv, stdin_text, now=None, min_tokens=None):
    return run_rc(argv, stdin_text, now, min_tokens)[1]


def test_session_start_outputs_brief_json(tmp_path):
    out = run(["hook-session-start"], hook_input(tmp_path))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "Fix login test" in ctx and (tmp_path / "proj/.claude/session-memory/graph.json").exists()
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_session_start_empty_project_prints_nothing(tmp_path):
    assert run(["hook-session-start"], hook_input(tmp_path, transcripts=False)) == ""


def test_hook_stop_ingests_and_prints_nothing(tmp_path):
    assert run(["hook-stop"], hook_input(tmp_path)) == ""
    mem = tmp_path / "proj/.claude/session-memory"
    assert (mem / "graph.json").exists() and (mem / "state.json").exists()


def test_prompt_hook_blocks_cold_large_chat(tmp_path):
    out = run(["hook-prompt"], hook_input(tmp_path, prompt="continue"), now=datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc), min_tokens=1000)
    assert json.loads(out)["decision"] == "block"
    assert "reason" in json.loads(out)
    state = json.loads((tmp_path / "proj/.claude/session-memory/guard.json").read_text(encoding="utf-8"))
    assert "guard" in state


def test_prompt_hook_allows_warm_chat(tmp_path):
    assert run(["hook-prompt"], hook_input(tmp_path, prompt="go"), now=datetime(2026, 11, 1, 10, 5, tzinfo=timezone.utc)) == ""


def test_hook_never_raises(tmp_path):  # Review Focus 2
    rc, out = run_rc(["hook-stop"], "not json at all")
    assert rc == 0 and out == ""


def test_hook_error_is_logged_to_error_log(tmp_path):
    data = json.loads(hook_input(tmp_path))
    data["transcript_path"] = 5  # makes dirname() raise after the memory dir is known
    rc, out = run_rc(["hook-stop"], json.dumps(data))
    assert rc == 0 and out == ""
    log = (tmp_path / "proj/.claude/session-memory/error.log").read_text(encoding="utf-8")
    assert "Traceback" in log


def test_remember_then_recall(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run(["remember", "Use", "SQLite", "for", "users"], "") == "Saved to project memory.\n"
    assert "[decision] Use SQLite for users" in run(["recall", "sqlite"], "")


def test_remember_links_current_task_and_recall_none(tmp_path, monkeypatch):
    run(["hook-stop"], hook_input(tmp_path))
    monkeypatch.chdir(tmp_path / "proj")
    run(["remember", "Use", "SQLite"], "", now=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc))
    graph = json.loads((tmp_path / "proj/.claude/session-memory/graph.json").read_text(encoding="utf-8"))
    decided = [e for e in graph["edges"].values() if e["rel"] == "decided"]
    assert len(decided) == 1 and decided[0]["dst"].startswith("decision:")
    assert decided[0]["src"].startswith("task:")
    assert graph["nodes"][decided[0]["dst"]]["last"] == "2026-11-02T09:00:00+00:00"
    assert run(["recall", "zzz-nope"], "") == 'Nothing found for "zzz-nope".\n'


def test_stats(tmp_path, monkeypatch):
    run(["hook-stop"], hook_input(tmp_path))
    monkeypatch.chdir(tmp_path / "proj")
    out = run(["stats"], "")
    assert out.startswith("nodes=") and "edges=" in out and "sessions=1" in out and "brief_chars=" in out


def test_non_ascii_prompt_and_brief_survive(tmp_path):  # R8
    prompt = "लॉगिन ठीक करो"
    lines = [json.dumps({"type": "user", "sessionId": "s2", "timestamp": "2026-11-01T12:00:00.000Z",
                         "message": {"role": "user", "content": prompt}}, ensure_ascii=False)]
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    (tdir / "hindi.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cwd = tmp_path / "proj"
    cwd.mkdir()
    stdin = json.dumps({"session_id": "s2", "transcript_path": str(tdir / "hindi.jsonl"),
                        "cwd": str(cwd), "prompt": prompt}, ensure_ascii=False)
    out = run(["hook-session-start"], stdin)
    out.encode("ascii")  # ensure_ascii=True
    assert prompt in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_prompt_hook_keeps_guard_state_in_guard_json_only(tmp_path):   # M2
    import hashlib
    now = datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc)
    mem = tmp_path / "proj/.claude/session-memory"
    mem.mkdir(parents=True)
    # A matching guard entry in state.json must be ignored: the hook reads only guard.json.
    stale = {"offsets": {"x.jsonl": 3},
             "guard": {"hash": hashlib.sha1(b"continue").hexdigest(), "at": now.isoformat()}}
    (mem / "state.json").write_text(json.dumps(stale), encoding="utf-8")
    out = run(["hook-prompt"], hook_input(tmp_path, prompt="continue"), now=now, min_tokens=1000)
    assert json.loads(out)["decision"] == "block"
    assert json.loads((mem / "state.json").read_text(encoding="utf-8")) == stale
    assert "guard" in json.loads((mem / "guard.json").read_text(encoding="utf-8"))



def test_session_start_brief_lists_files_relative_to_cwd(tmp_path):   # I3
    cwd = tmp_path / "proj"
    cwd.mkdir()
    lines = [{"type": "user", "sessionId": "s3", "timestamp": "2026-11-01T12:00:00.000Z",
              "message": {"role": "user", "content": "edit things"}}]
    for i, path in enumerate([cwd / "src" / "app.py", tmp_path / "other" / "x.py"]):
        lines.append({"type": "assistant", "sessionId": "s3", "timestamp": f"2026-11-01T12:00:0{i + 1}.000Z",
                      "message": {"content": [{"type": "tool_use", "id": f"e{i}", "name": "Edit",
                                               "input": {"file_path": str(path)}}]}})
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    (tdir / "t.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    stdin = json.dumps({"session_id": "s3", "transcript_path": str(tdir / "t.jsonl"), "cwd": str(cwd)})
    ctx = json.loads(run(["hook-session-start"], stdin))["hookSpecificOutput"]["additionalContext"]
    assert "- src/app.py" in ctx and "x.py" not in ctx


def test_map_no_open_prints_path_and_does_not_open(tmp_path, monkeypatch):
    run(["hook-stop"], hook_input(tmp_path))
    monkeypatch.chdir(tmp_path / "proj")
    opened = []
    monkeypatch.setattr("memgraph.cli.webbrowser.open", lambda url: opened.append(url))
    out = run(["map", "--no-open"], "")
    path = tmp_path / "proj/.claude/session-memory/memory-map.html"
    assert out == f"Memory map: {path}\n"
    assert path.exists() and opened == []


def test_map_opens_browser_with_file_uri(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    opened = []
    monkeypatch.setattr("memgraph.cli.webbrowser.open", lambda url: opened.append(url))
    out = run(["map"], "")
    path = tmp_path / ".claude/session-memory/memory-map.html"
    assert out == f"Memory map: {path}\n"
    assert opened == [path.as_uri()] and opened[0].startswith("file:")


def test_pack_writes_text_and_image(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    run(["hook-stop"], hook_input(tmp_path))
    monkeypatch.chdir(tmp_path / "proj")
    out = run(["pack", "--size", "tiny"], "")
    mem = tmp_path / "proj/.claude/session-memory"
    assert out == f"Resume pack: {mem / 'resume-pack.md'}\nVisual pack: {mem / 'memory.png'}\n"
    assert (mem / "resume-pack.md").exists() and (mem / "memory.png").exists()


def test_pack_no_visual_and_bad_size(tmp_path, monkeypatch):
    run(["hook-stop"], hook_input(tmp_path))
    monkeypatch.chdir(tmp_path / "proj")
    out = run(["pack", "--no-visual"], "")
    assert out.splitlines() == [f"Resume pack: {tmp_path / 'proj/.claude/session-memory/resume-pack.md'}"]
    rc, text = run_rc(["pack", "--size", "huge"], "")
    assert rc == 2 and text.startswith("Usage: pack")


def test_pack_reports_missing_pillow(tmp_path, monkeypatch):
    run(["hook-stop"], hook_input(tmp_path))
    monkeypatch.chdir(tmp_path / "proj")
    monkeypatch.setattr("memgraph.cli.render_visual_pack", lambda *a, **k: None)
    assert run(["pack"], "").splitlines()[1] == "Visual pack skipped: install Pillow"


def test_pack_empty_graph_says_nothing_to_draw_and_removes_stale_png(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mem = tmp_path / ".claude/session-memory"
    mem.mkdir(parents=True)
    (mem / "memory.png").write_bytes(b"stale")
    out = run(["pack"], "")
    assert out.splitlines()[1] == "Visual pack skipped: nothing to draw yet"
    assert not (mem / "memory.png").exists()


def _hold_graph_lock(mem):
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "graph.lock").write_text("held", encoding="utf-8")


def test_remember_reports_busy_graph_instead_of_losing_data(tmp_path, monkeypatch):   # I3
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("memgraph.graph.LOCK_TIMEOUT", 0.2)
    _hold_graph_lock(tmp_path / ".claude/session-memory")
    rc, out = run_rc(["remember", "Use", "SQLite"], "")
    assert rc == 1 and "busy" in out.lower()
    assert not (tmp_path / ".claude/session-memory/graph.json").exists()


def test_hooks_skip_ingest_when_graph_is_busy_and_still_arm(tmp_path, monkeypatch):   # I3
    from memgraph import watch

    armed = []
    monkeypatch.setattr(watch, "arm", lambda *a, **k: armed.append(a))
    monkeypatch.setattr("memgraph.graph.LOCK_TIMEOUT", 0.2)
    mem = tmp_path / "proj/.claude/session-memory"
    _hold_graph_lock(mem)
    payload = hook_input(tmp_path)
    assert run(["hook-stop"], payload) == ""
    assert not (mem / "graph.json").exists() and not (mem / "state.json").exists()
    assert len(armed) == 1 and not (mem / "error.log").exists()
    (mem / "graph.lock").unlink()
    assert run(["hook-stop"], payload) == ""  # next hook catches up
    assert (mem / "graph.json").exists() and not (mem / "graph.lock").exists()


def test_remember_text_capped_at_1000_chars(tmp_path, monkeypatch):   # M5
    monkeypatch.chdir(tmp_path)
    rc, out = run_rc(["remember", "x" * 1001], "")
    assert rc == 2 and "1000" in out.replace(",", "")
    assert not (tmp_path / ".claude/session-memory/graph.json").exists()
    assert run(["remember", "y" * 1000], "") == "Saved to project memory.\n"


DAY2_PROMPT = (
    '{"type":"user","sessionId":"s2","timestamp":"2026-11-02T09:00:00.000Z",'
    '"message":{"role":"user","content":"Day two: add the audit log"}}'
)


def test_session_start_refreshes_the_pack_it_points_at(tmp_path, monkeypatch):   # I1
    payload = hook_input(tmp_path)
    run(["hook-stop"], payload)
    monkeypatch.chdir(tmp_path / "proj")
    run(["pack", "--no-visual"], "")  # day 1 pack
    mem = tmp_path / "proj/.claude/session-memory"
    assert "Day two" not in (mem / "resume-pack.md").read_text(encoding="utf-8")
    with open(tmp_path / "transcripts" / FIXTURE.name, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n" + DAY2_PROMPT + "\n")  # the fixture ends in a half-written line
    run(["hook-stop"], payload)
    out = run(["hook-session-start"], payload)
    brief = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "Full resume pack: .claude/session-memory/resume-pack.md" in brief
    text = (mem / "resume-pack.md").read_text(encoding="utf-8")
    assert "Day two: add the audit log" in text and text.splitlines()[2].startswith("Built ")


def test_session_start_points_at_picture_only_when_fresh(tmp_path):   # I1
    import os
    payload = hook_input(tmp_path)
    mem = tmp_path / "proj/.claude/session-memory"
    mem.mkdir(parents=True)
    (mem / "memory.png").write_bytes(b"old picture")
    os.utime(mem / "memory.png", (1_000_000_000, 1_000_000_000))
    brief = json.loads(run(["hook-session-start"], payload))["hookSpecificOutput"]["additionalContext"]
    assert "Full resume pack" in brief and "Visual summary" not in brief


def test_session_start_removes_pack_when_nothing_to_pack(tmp_path):   # I1
    payload = hook_input(tmp_path, transcripts=False)
    mem = tmp_path / "proj/.claude/session-memory"
    mem.mkdir(parents=True)
    (mem / "resume-pack.md").write_text("stale", encoding="utf-8")
    assert run(["hook-session-start"], payload) == ""
    assert not (mem / "resume-pack.md").exists()


def test_pack_cli_never_writes_an_empty_pack(tmp_path, monkeypatch):   # I1
    monkeypatch.chdir(tmp_path)
    out = run(["pack", "--no-visual"], "")
    assert out == "Resume pack skipped: nothing to pack yet\n"
    assert not (tmp_path / ".claude/session-memory/resume-pack.md").exists()


def test_session_start_rebuilds_pack_at_last_size_built(tmp_path, monkeypatch):
    payload = hook_input(tmp_path)
    run(["hook-stop"], payload)
    monkeypatch.chdir(tmp_path / "proj")
    run(["pack", "--size", "deep", "--no-visual"], "")
    sizes = []
    real = __import__("memgraph.cli", fromlist=["write_pack"]).write_pack
    monkeypatch.setattr("memgraph.cli.write_pack", lambda g, m, s, *a, **k: sizes.append(s) or real(g, m, s, *a, **k))
    run(["hook-session-start"], payload)
    assert sizes == ["deep"]


# ---------------------------------------------------------------- token counter: events and transcripts dir

def _events(tmp_path):
    from memgraph.savings import read_events
    return read_events(tmp_path / "proj/.claude/session-memory")


def test_guard_block_logs_event_and_transcripts_dir(tmp_path):
    out = run(["hook-prompt"], hook_input(tmp_path, prompt="continue"),
              now=datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc), min_tokens=1000)
    assert json.loads(out)["decision"] == "block"
    (ev,) = _events(tmp_path)
    assert ev["type"] == "guard_block" and ev["session"] == "s1" and ev["context"] == 42010 and ev["t"]
    guard = json.loads((tmp_path / "proj/.claude/session-memory/guard.json").read_text(encoding="utf-8"))
    assert guard["transcripts_dir"] == str(tmp_path / "transcripts")


def test_allowed_prompt_logs_no_event(tmp_path):
    run(["hook-prompt"], hook_input(tmp_path, prompt="go"), now=datetime(2026, 11, 1, 10, 5, tzinfo=timezone.utc))
    assert _events(tmp_path) == []


def test_session_start_logs_fresh_start_with_previous_session(tmp_path):
    data = json.loads(hook_input(tmp_path, source="clear"))
    data["session_id"] = "s-new"
    out = run(["hook-session-start"], json.dumps(data))
    brief = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    (ev,) = _events(tmp_path)
    assert ev["type"] == "fresh_start" and ev["session"] == "s-new"
    assert ev["brief_chars"] == len(brief)
    assert ev["prev_session"] == "s1" and ev["prev_context"] == 42010
    state = json.loads((tmp_path / "proj/.claude/session-memory/state.json").read_text(encoding="utf-8"))
    assert state["transcripts_dir"] == str(tmp_path / "transcripts")


def test_session_start_same_session_has_no_previous(tmp_path):
    run(["hook-session-start"], hook_input(tmp_path))
    (ev,) = _events(tmp_path)
    assert ev["prev_session"] is None and ev["prev_context"] is None


def test_session_start_resume_logs_nothing(tmp_path):
    run(["hook-session-start"], hook_input(tmp_path, source="resume"))
    assert _events(tmp_path) == []


def test_session_start_without_brief_logs_nothing(tmp_path):
    assert run(["hook-session-start"], hook_input(tmp_path, transcripts=False)) == ""
    assert _events(tmp_path) == []


def test_hook_stop_records_transcripts_dir(tmp_path):
    run(["hook-stop"], hook_input(tmp_path))
    state = json.loads((tmp_path / "proj/.claude/session-memory/state.json").read_text(encoding="utf-8"))
    assert state["transcripts_dir"] == str(tmp_path / "transcripts")


def test_event_log_failure_never_breaks_the_brief(tmp_path, monkeypatch):
    import memgraph.savings as savings

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(savings, "append_event", boom)
    out = run(["hook-session-start"], hook_input(tmp_path, source="clear"))
    assert "Fix login test" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "disk full" in (tmp_path / "proj/.claude/session-memory/error.log").read_text(encoding="utf-8")
