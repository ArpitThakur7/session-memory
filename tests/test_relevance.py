import tempfile
from pathlib import Path

from memgraph.graph import Graph
from memgraph.relevance import is_project_file, is_scratch_command, project_errors

ROOT = Path("C:/Users/me/proj")
TMP = tempfile.gettempdir()
SCRATCH = r"C:\Users\me\AppData\Local\Temp\claude\x\scratchpad\edit.py"


def test_project_file_kept():
    assert is_project_file("memgraph/cli.py", ROOT)
    assert is_project_file("C:/Users/me/proj/memgraph/cli.py", ROOT)
    assert is_project_file(r"C:\Users\me\proj\memgraph\cli.py", ROOT)
    assert is_project_file("memgraph/cli.py", None)


def test_outside_root_dropped():
    assert not is_project_file("C:/Users/other/x.py", ROOT)
    assert not is_project_file("/home/u/x.py", ROOT)


def test_ignored_segments():
    assert not is_project_file(".superpowers/sdd/plan/task-1-brief.md", ROOT)
    assert not is_project_file("a/node_modules/b.js", None)
    assert not is_project_file("C:/Users/me/proj/.claude/x.json", ROOT)
    assert not is_project_file("pkg/__pycache__/m.pyc", None)
    assert is_project_file(".gitignore", ROOT)


def test_temp_folder():
    fwd = TMP.replace("\\", "/")
    assert not is_project_file(fwd + "/scratch.py", None)
    assert not is_project_file(fwd.upper() + "/s.py", None)
    assert not is_project_file(fwd.replace("/", "\\") + "\\s.py", ROOT)
    assert not is_project_file(SCRATCH, None)


def test_scratch_command():
    assert is_scratch_command(f"python {SCRATCH}", ROOT)
    assert is_scratch_command(f"python {TMP}/x.py", ROOT)
    assert is_scratch_command("cat .superpowers/sdd/plan/task-1-brief.md", ROOT)
    assert is_scratch_command(f"cd {TMP} && ls", ROOT)
    assert is_scratch_command("cd .claude/foo && ls", None)
    assert not is_scratch_command("python -m pytest -q memgraph/cli.py", ROOT)
    assert not is_scratch_command("git add .gitignore", ROOT)


def make_graph():
    g = Graph()
    t = "2026-01-01T00:00:00Z"
    c_bad = g.add_node("command", "1", f"python {SCRATCH}", t)
    c_ok = g.add_node("command", "2", "pytest memgraph/cli.py", t)
    f_bad = g.add_node("file", "a", ".superpowers/sdd/x.md", t)
    f_ok = g.add_node("file", "b", "memgraph/cli.py", t)
    errs = {k: g.add_node("error", k, k, t) for k in ("e1", "e2", "e3", "e4", "e5")}
    g.nodes[errs["e5"]]["label"] = f"No such file {TMP}/zzz"
    g.add_edge(c_bad, "failed", errs["e1"], t)
    g.add_edge(c_ok, "failed", errs["e2"], t)
    g.add_edge(f_bad, "failed", errs["e3"], t)
    g.add_edge(f_ok, "failed", errs["e4"], t)
    g.add_edge(c_ok, "failed", errs["e5"], t)
    return g


def test_project_errors():
    g = make_graph()
    kept = project_errors(g, [n for n in g.nodes.values() if n["kind"] == "error"], ROOT)
    assert sorted(n["label"] for n in kept) == ["e2", "e4"]


def test_brief_pack_hide_noise():
    from memgraph.brief import build_brief
    from memgraph.pack import build_pack

    g = make_graph()
    t = "2026-01-01T00:00:00Z"
    sess = g.add_node("session", "s", "s", t)
    for key, label in (("x", ".superpowers/sdd/x.md"), ("y", "memgraph/cli.py"), ("z", "C:/Users/other/o.py")):
        g.add_edge(sess, "edited", g.add_node("file", key, label, t), t)
    for out in (build_brief(g, project_root=ROOT), build_pack(g, project_root=ROOT)):
        assert "memgraph/cli.py" in out and ".superpowers" not in out and "o.py" not in out
        assert "- e2" in out and "- e4" in out
        for gone in ("- e1", "- e3", "- e5", "No such file"):
            assert gone not in out


def test_visual_hides_noise():
    from memgraph import visual

    class F:
        def getlength(self, s):
            return 7 * len(s)

    g = make_graph()
    t = "2026-01-01T00:00:00Z"
    sess = g.add_node("session", "s", "s", t)
    task = g.add_node("task", "t", "do it", t)
    g.add_edge(sess, "asked", task, t)
    for key, label in (("x", ".superpowers/sdd/x.md"), ("y", "memgraph/cli.py")):
        g.add_edge(task, "edited", g.add_node("file", key, label, t), t)
    for _title, tasks, _se in visual._story(g, ROOT):
        for _t, n_files, errors in tasks:
            assert n_files == 1
    text = str(visual.compose(g, F(), 400, 60, ROOT)["flow"])
    assert "cli.py" in text and "x.md" not in text and "edit.py" not in text and "zzz" not in text
