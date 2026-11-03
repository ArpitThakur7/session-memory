"""`setup-mcp`: JSON merges in temp dirs only; `claude mcp add` through a fake runner."""
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

import memgraph.setup_mcp as setup
from memgraph.cli import main

REPO = Path(setup.__file__).resolve().parent.parent.as_posix()


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    state = {"which": {"claude": "/fake/bin/claude", "py": "C:/Windows/py.exe"}, "result": (0, "Added", ""),
             "calls": []}

    def fake_which(name):
        return state["which"].get(name)

    def fake_run(cmd, **kw):
        state["calls"].append((cmd, kw))
        rc, out, err = state["result"]
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    monkeypatch.setattr(setup.shutil, "which", fake_which)
    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    monkeypatch.setattr(setup, "_is_windows", lambda: True)  # Windows form by default; POSIX tested explicitly
    return {"home": home, "project": project, **state, "state": state}


def run(*argv):
    out = io.StringIO()
    rc = main(["setup-mcp", *argv], io.StringIO(""), out)
    return rc, out.getvalue()


def vscode_file(project):
    return project / ".vscode" / "mcp.json"


def ag_file(home):
    return home / ".gemini" / "antigravity" / "mcp_config.json"


def test_vscode_creates_file_windows_py(env):
    rc, text = run("vscode", "--project", str(env["project"]))
    assert rc == 0
    data = json.loads(vscode_file(env["project"]).read_text(encoding="utf-8"))
    assert data == {"servers": {"session-memory": {
        "type": "stdio", "command": "py",
        "args": ["-3", "-m", "memgraph.cli", "mcp", "--project", "${workspaceFolder}"],
        "env": {"PYTHONPATH": REPO}}}}
    assert "\\" not in REPO
    assert str(vscode_file(env["project"])) in text


def test_vscode_windows_without_py_uses_executable(env):
    env["state"]["which"].pop("py")
    run("vscode", "--project", str(env["project"]))
    entry = json.loads(vscode_file(env["project"]).read_text(encoding="utf-8"))["servers"]["session-memory"]
    assert entry["command"] == Path(sys.executable).as_posix()
    assert entry["args"][0] == "-m"


def test_vscode_posix(env, monkeypatch):
    monkeypatch.setattr(setup, "_is_windows", lambda: False)
    run("vscode", "--project", str(env["project"]))
    entry = json.loads(vscode_file(env["project"]).read_text(encoding="utf-8"))["servers"]["session-memory"]
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "memgraph.cli", "mcp", "--project", "${workspaceFolder}"]


def test_vscode_merge_keeps_others_and_backs_up(env):
    f = vscode_file(env["project"])
    f.parent.mkdir()
    original = {"inputs": [1], "servers": {"other": {"command": "x"}}}
    f.write_text(json.dumps(original), encoding="utf-8")
    rc, _ = run("vscode", "--project", str(env["project"]))
    assert rc == 0
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["inputs"] == [1] and data["servers"]["other"] == {"command": "x"}
    assert "session-memory" in data["servers"]
    baks = list(f.parent.glob("mcp.json.*.bak"))
    assert len(baks) == 1 and json.loads(baks[0].read_text(encoding="utf-8")) == original
    assert not list(f.parent.glob("*.tmp"))


def test_idempotent(env):
    run("vscode", "--project", str(env["project"]))
    before = vscode_file(env["project"]).read_text(encoding="utf-8")
    rc, text = run("vscode", "--project", str(env["project"]))
    assert rc == 0 and "already set up" in text.lower()
    assert vscode_file(env["project"]).read_text(encoding="utf-8") == before
    assert not list(vscode_file(env["project"]).parent.glob("*.bak"))


def test_updates_stale_entry(env):
    f = vscode_file(env["project"])
    f.parent.mkdir()
    f.write_text(json.dumps({"servers": {"session-memory": {"command": "old"}}}), encoding="utf-8")
    rc, text = run("vscode", "--project", str(env["project"]))
    assert rc == 0 and "update" in text.lower()
    assert json.loads(f.read_text(encoding="utf-8"))["servers"]["session-memory"]["command"] == "py"


@pytest.mark.parametrize("content,word", [("{not json", "not valid json"),
                                          ('{\n  // a comment\n  "servers": {}\n}', "comments"),
                                          ("[1, 2]", "not valid"),
                                          ('{"servers": []}', "servers")])
def test_refuses_bad_files(env, content, word):
    f = vscode_file(env["project"])
    f.parent.mkdir()
    f.write_text(content, encoding="utf-8")
    rc, text = run("vscode", "--project", str(env["project"]))
    assert rc == 2 and word in text.lower()
    assert f.read_text(encoding="utf-8") == content
    assert not list(f.parent.glob("*.bak"))


def test_dry_run_changes_nothing(env):
    rc, text = run("vscode", "--project", str(env["project"]), "--dry-run")
    assert rc == 0 and "dry run" in text.lower() and "${workspaceFolder}" in text
    assert not (env["project"] / ".vscode").exists()
    rc, text = run("antigravity", "--project", str(env["project"]), "--dry-run")
    assert rc == 0 and not ag_file(env["home"]).exists()


def test_antigravity_merges_under_mcp_servers(env):
    f = ag_file(env["home"])
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"mcpServers": {"github": {"command": "gh"}}, "x": 1}), encoding="utf-8")
    rc, text = run("antigravity", "--project", str(env["project"]))
    assert rc == 0
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["x"] == 1 and data["mcpServers"]["github"] == {"command": "gh"}
    entry = data["mcpServers"]["session-memory"]
    assert entry["command"] == "py"
    assert entry["args"] == ["-3", "-m", "memgraph.cli", "mcp", "--project", Path(env["project"]).resolve().as_posix()]
    assert entry["env"] == {"PYTHONPATH": REPO}
    assert len(list(f.parent.glob("mcp_config.json.*.bak"))) == 1


def test_antigravity_creates_missing_file(env):
    rc, _ = run("antigravity", "--project", str(env["project"]))
    assert rc == 0 and "session-memory" in json.loads(ag_file(env["home"]).read_text(encoding="utf-8"))["mcpServers"]


def test_claude_dry_run_prints_command(env):
    rc, text = run("claude", "--project", str(env["project"]), "--dry-run")
    assert rc == 0 and env["calls"] == []
    assert "claude mcp add -s user session-memory -e PYTHONPATH=" in text
    assert "-- py -3 -m memgraph.cli mcp --project" in text


def test_claude_runs_list_args(env):
    rc, text = run("claude", "--project", str(env["project"]), "--scope", "project")
    assert rc == 0
    (cmd, kw), = env["calls"]
    project = str(Path(env["project"]).resolve())
    assert cmd == ["/fake/bin/claude", "mcp", "add", "-s", "project", "session-memory", "-e", f"PYTHONPATH={REPO}",
                   "--", "py", "-3", "-m", "memgraph.cli", "mcp", "--project", project]
    assert not kw.get("shell") and kw.get("cwd") == project
    assert "added" in text.lower()


def test_claude_already_exists_is_ok(env):
    env["state"]["result"] = (1, "", "MCP server session-memory already exists in user config")
    rc, text = run("claude", "--project", str(env["project"]))
    assert rc == 0 and "already set up" in text.lower()


def test_claude_failure_and_missing_cli(env):
    env["state"]["result"] = (1, "", "something broke")
    rc, text = run("claude", "--project", str(env["project"]))
    assert rc == 1 and "something broke" in text
    env["state"]["which"].pop("claude")
    rc, text = run("claude", "--project", str(env["project"]))
    assert rc == 1 and "not found" in text.lower()


@pytest.mark.parametrize("argv", [[], ["emacs"], ["claude", "--scope", "galaxy"], ["vscode", "--project"]])
def test_usage_errors(env, argv):
    rc, text = run(*argv)
    assert rc == 2 and "usage" in text.lower()
