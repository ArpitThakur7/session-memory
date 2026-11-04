"""`doctor`: every check isolated, never touches real configs (temp HOME, fake `claude`)."""
import io
import json
import subprocess
from pathlib import Path

import pytest

import memgraph.doctor as doctor
from memgraph.cli import main
from memgraph.usage import project_slug

PLUGIN_JSON = json.dumps([{"id": "session-memory@session-memory", "enabled": True}])


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp HOME and project; a fake `claude` on PATH whose answers the test can set."""
    home, project = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    answers = {"plugin": (0, PLUGIN_JSON), "mcp": (0, "session-memory: py -3 -m memgraph.cli mcp - ✓ Connected\n")}
    calls = []

    def fake_which(name):
        return "/fake/bin/claude" if name == "claude" and answers.get("claude_on_path", True) else None

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        key = "plugin" if cmd[1:3] == ["plugin", "list"] else "mcp"
        rc, out = answers[key]
        if isinstance(out, BaseException):
            raise out
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    monkeypatch.setattr(doctor.shutil, "which", fake_which)
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    return {"home": home, "project": project, "answers": answers, "calls": calls}


def run_doctor(project, *extra):
    out = io.StringIO()
    rc = main(["doctor", "--project", str(project), *extra], io.StringIO(""), out)
    return rc, out.getvalue()


def checks(project):
    rc, text = run_doctor(project, "--json")
    data = json.loads(text)
    return rc, {c["name"]: c for c in data["checks"]}, data


def test_healthy_setup_reports_ok_and_exit_0(env):
    rc, by, data = checks(env["project"])
    assert rc == 0 and data["ok"] is True
    for name in ("python", "plugin_files", "claude_cli", "plugin_installed", "mcp_claude"):
        assert by[name]["status"] == "ok", by[name]
    assert "3." in by["python"]["detail"]


def test_text_output_has_marks_and_fixes(env):
    env["answers"]["claude_on_path"] = False
    rc, text = run_doctor(env["project"])
    assert rc == 0
    assert "✓ Python" in text
    assert "✗" in text and "Claude Code" in text
    assert "–" in text  # unknown/optional checks
    assert "fix:" in text.lower()


def test_claude_missing_is_not_required(env):
    env["answers"]["claude_on_path"] = False
    rc, by, _ = checks(env["project"])
    assert rc == 0
    assert by["claude_cli"]["status"] == "fail" and by["claude_cli"]["fix"]
    assert by["plugin_installed"]["status"] == "skip"
    assert by["mcp_claude"]["status"] == "skip"
    assert env["calls"] == []


def test_plugin_not_installed_or_disabled(env):
    env["answers"]["plugin"] = (0, json.dumps([{"id": "other@x", "enabled": True}]))
    _, by, _ = checks(env["project"])
    assert by["plugin_installed"]["status"] == "fail"
    assert "plugin install session-memory@session-memory" in by["plugin_installed"]["fix"]
    env["answers"]["plugin"] = (0, json.dumps([{"id": "session-memory@session-memory", "enabled": False}]))
    _, by, _ = checks(env["project"])
    assert by["plugin_installed"]["status"] == "fail" and "enable" in by["plugin_installed"]["fix"]


@pytest.mark.parametrize("answer", [(1, "boom"), (0, "not json"),
                                    (0, subprocess.TimeoutExpired(["claude"], 15)), (0, OSError("nope"))])
def test_plugin_list_unknown_is_skip(env, answer):
    env["answers"]["plugin"] = answer
    env["answers"]["mcp"] = answer
    rc, by, _ = checks(env["project"])
    assert rc == 0
    assert by["plugin_installed"]["status"] == "skip"
    assert by["mcp_claude"]["status"] == "skip"


def test_claude_calls_use_timeout_and_list_args(env):
    checks(env["project"])
    assert env["calls"]
    for cmd, kw in env["calls"]:
        assert isinstance(cmd, list) and kw.get("timeout") == 15 and not kw.get("shell")
    assert any(cmd[1:] == ["plugin", "list", "--json"] for cmd, _ in env["calls"])


def test_mcp_claude_missing_entry(env):
    env["answers"]["mcp"] = (0, "context7: https://x - ✓ Connected\n")
    _, by, _ = checks(env["project"])
    assert by["mcp_claude"]["status"] == "skip" and "setup-mcp claude" in by["mcp_claude"]["fix"]


def test_transcripts_and_memory(env):
    project = env["project"]
    _, by, _ = checks(project)
    assert by["transcripts"]["status"] == "skip"
    assert by["memory"]["status"] == "skip"
    assert not (project / ".claude").exists()  # doctor never creates the memory folder
    tdir = env["home"] / ".claude" / "projects" / project_slug(project)
    tdir.mkdir(parents=True)
    (tdir / "a.jsonl").write_text("", encoding="utf-8")
    mem = project / ".claude" / "session-memory"
    mem.mkdir(parents=True)
    (mem / "graph.json").write_text(json.dumps({"nodes": {"a": {}, "b": {}}, "edges": {}, "meta": {}}),
                                    encoding="utf-8")
    _, by, _ = checks(project)
    assert by["transcripts"]["status"] == "ok" and "1" in by["transcripts"]["detail"]
    assert by["memory"]["status"] == "ok" and "2 items" in by["memory"]["detail"]
    # A recorded folder with no transcripts (e.g. a probe's temp dir) must not hide the real one.
    empty = env["home"] / "empty"
    empty.mkdir()
    (mem / "state.json").write_text(json.dumps({"transcripts_dir": str(empty)}), encoding="utf-8")
    _, by, _ = checks(project)
    assert str(tdir) in by["transcripts"]["detail"]


def test_watcher_state(env):
    mem = env["project"] / ".claude" / "session-memory"
    mem.mkdir(parents=True)
    _, by, _ = checks(env["project"])
    assert by["watcher"]["status"] == "skip"
    (mem / "watch.json").write_text(json.dumps({"deadline": "2026-11-03T10:00:00+00:00"}), encoding="utf-8")
    _, by, _ = checks(env["project"])
    assert "2026-11-03" in by["watcher"]["detail"]


def test_vscode_and_antigravity(env):
    _, by, _ = checks(env["project"])
    assert by["mcp_vscode"]["status"] == "skip" and "setup-mcp vscode" in by["mcp_vscode"]["fix"]
    assert by["mcp_antigravity"]["status"] == "skip" and "setup-mcp antigravity" in by["mcp_antigravity"]["fix"]
    vs = env["project"] / ".vscode"
    vs.mkdir()
    (vs / "mcp.json").write_text(json.dumps({"servers": {"session-memory": {"command": "py"}}}), encoding="utf-8")
    ag = env["home"] / ".gemini" / "antigravity"
    ag.mkdir(parents=True)
    (ag / "mcp_config.json").write_text(json.dumps({"mcpServers": {"session-memory": {}}}), encoding="utf-8")
    _, by, _ = checks(env["project"])
    assert by["mcp_vscode"]["status"] == "ok"
    assert by["mcp_antigravity"]["status"] == "ok"
    (vs / "mcp.json").write_text("{broken", encoding="utf-8")
    rc, by, _ = checks(env["project"])
    assert rc == 0 and by["mcp_vscode"]["status"] == "fail"


def test_pillow_optional(env, monkeypatch):
    monkeypatch.setattr(doctor, "_has_pillow", lambda: False)
    rc, by, _ = checks(env["project"])
    assert rc == 0 and by["pillow"]["status"] == "skip" and by["pillow"]["fix"] == "pip install pillow"


def test_missing_plugin_files_fail_exit_1(env, monkeypatch, tmp_path):
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    monkeypatch.setattr(doctor, "REPO", fake_repo)
    rc, by, data = checks(env["project"])
    assert rc == 1 and data["ok"] is False
    assert by["plugin_files"]["status"] == "fail" and "hooks" in by["plugin_files"]["detail"]


def test_old_python_fails(env, monkeypatch):
    monkeypatch.setattr(doctor, "_version", lambda: (3, 9, 1))
    rc, by, _ = checks(env["project"])
    assert rc == 1 and by["python"]["status"] == "fail"


def test_a_crashing_check_is_isolated(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(doctor, "_check_watcher", boom)
    rc, by, _ = checks(env["project"])
    assert rc == 0 and by["watcher"]["status"] == "skip" and "kaboom" in by["watcher"]["detail"]
    assert by["python"]["status"] == "ok"


def test_default_project_is_cwd(env, monkeypatch):
    monkeypatch.chdir(env["project"])
    out = io.StringIO()
    rc = main(["doctor", "--json"], io.StringIO(""), out)
    assert rc == 0 and json.loads(out.getvalue())["project"] == str(Path(env["project"]).resolve())
