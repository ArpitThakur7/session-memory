import io

from memgraph.cli import main
from memgraph.explore import explain_lines, path_lines
from memgraph.graph import Graph
from memgraph.ingest import memory_dir
from memgraph.transcript import Event

TS = "2026-11-05T10:00:00Z"


def ev(kind, sid="s1", **data):
    return Event(kind, TS, sid, data)


def build():
    g = Graph()
    g.apply(ev("prompt", text="make the login form work"))
    g.apply(ev("edit", path="src/login.py", id="t1"))
    g.apply(ev("command", cmd="pytest tests/test_login.py", id="t2"))
    g.apply(ev("error", text="NameError in src/login.py line 3", tool_use_id="t2"))
    g.apply(ev("prompt", text="write the billing report"))
    g.apply(ev("edit", path="src/billing.py", id="t3"))
    return g


def test_explain_shows_node_neighbours_and_tags():
    lines = explain_lines(build(), "login form")
    assert lines[0].startswith("[task] make the login form work")
    body = "\n".join(lines[1:])
    assert "edited → [file] src/login.py (extracted)" in body
    assert "ran → [command] pytest tests/test_login.py (extracted)" in body
    assert "asked by ← [session]" in body


def test_explain_error_lists_inferred_mention():
    body = "\n".join(explain_lines(build(), "NameError"))
    assert "mentions → [file] src/login.py (inferred)" in body
    assert "failed ← [command] pytest tests/test_login.py (extracted)" in body


def test_explain_respects_budget_and_misses():
    g = build()
    for i in range(30):
        g.apply(ev("read", path=f"docs/page_{chr(65 + i)}.md", id=f"r{i}"))
    lines = explain_lines(g, "billing report", budget=60)
    assert len("\n".join(lines)) <= 240 and lines[-1].endswith("more — narrow the word")
    assert explain_lines(g, "zzzz") == []


def test_path_avoids_the_session_hub():
    lines = path_lines(build(), "src/login.py", "NameError")
    text = "\n".join(lines)
    assert lines[0].startswith("Path (1 hop)")
    assert "[session]" not in text and "mentions" in text


def test_path_through_task_and_command():
    text = "\n".join(path_lines(build(), "login form", "NameError"))
    assert text.startswith("Path (2 hops)")
    assert "[task] make the login form work" in text and "[error] NameError" in text


def test_path_falls_back_to_session_hub_and_reports_no_path():
    g = build()
    text = "\n".join(path_lines(g, "login.py", "billing.py"))
    assert "via a session" in text and "[file] src/billing.py" in text
    g.add_node("decision", "lonely", "a lonely decision", TS)
    assert path_lines(g, "lonely", "billing.py") == ['No link between "a lonely decision" and "src/billing.py".']
    assert path_lines(g, "nothinghere", "billing.py") == ['Nothing found for "nothinghere".']


def run(argv, project):
    out = io.StringIO()
    rc = main(argv, io.StringIO(""), out)
    return rc, out.getvalue()


def test_cli_explain_and_path(tmp_path, monkeypatch):
    build().save(memory_dir(tmp_path) / "graph.json")
    monkeypatch.chdir(tmp_path)
    rc, out = run(["explain", "NameError"], tmp_path)
    assert rc == 0 and out.startswith("[error] NameError")
    rc, out = run(["path", "login form", "NameError"], tmp_path)
    assert rc == 0 and out.startswith("Path (2 hops)")
    rc, out = run(["path", "onlyone"], tmp_path)
    assert rc == 2 and out.startswith("Usage: path <a> <b>")
    rc, out = run(["explain", "zzzz"], tmp_path)
    assert rc == 0 and out == 'Nothing found for "zzzz".\n'


def test_mcp_explain_and_path(tmp_path):
    from tests.test_mcp import call, drive
    proj = tmp_path / "proj"
    build().save(memory_dir(proj) / "graph.json")
    ex, pa, bad = drive(proj, call(1, "memory_explain", thing="NameError", budget=200),
                        call(2, "memory_path", a="login form", b="NameError"),
                        call(3, "memory_path", a="x"))
    assert ex["result"]["content"][0]["text"].startswith("[error] NameError")
    assert pa["result"]["content"][0]["text"].startswith("Path (2 hops)")
    assert bad["result"]["isError"] is True
