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
    state = json.loads((tmp_path / "proj/.claude/session-memory/state.json").read_text(encoding="utf-8"))
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
