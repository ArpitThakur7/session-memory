import os
from pathlib import Path

from memgraph.graph import Graph
from memgraph.transcript import Event
from tests.helpers import graph_from_fixture


def test_fixture_builds_expected_nodes(tmp_path):
    g = graph_from_fixture()
    kinds = sorted(n["kind"] for n in g.nodes.values())
    assert kinds == ["command", "error", "file", "session", "task"]


def test_edit_links_task_to_file():
    g = graph_from_fixture()
    assert any(e["rel"] == "edited" and e["dst"] == "file:src/login.py" and e["src"].startswith("task:") for e in g.edges.values())


def test_error_links_to_failing_command():
    g = graph_from_fixture()
    assert any(e["rel"] == "failed" and e["src"].startswith("command:") for e in g.edges.values())


def test_last_todos_and_context():
    g = graph_from_fixture()
    assert g.meta["last_todos"]["items"][0]["content"] == "Fix login test"
    assert g.meta["sessions"]["s1"]["context"] == 42010


def test_repeat_node_counts(tmp_path):
    g = Graph(); g.add_node("file", "a.py", "a.py", "t1"); g.add_node("file", "a.py", "a.py", "t2")
    assert g.nodes["file:a.py"]["count"] == 2 and g.nodes["file:a.py"]["last"] == "t2"


def test_save_load_roundtrip_non_ascii(tmp_path):
    g = Graph(); g.add_node("task", "k", "लॉगिन पेज जोड़ें", "t"); g.add_node("file", "C:/Users/अ/x.py", "C:/Users/अ/x.py", "t")
    g.save(tmp_path / "graph.json")
    assert Graph.load(tmp_path / "graph.json").nodes == g.nodes


def test_save_is_atomic(tmp_path, monkeypatch):
    p = tmp_path / "graph.json"; Graph().save(p)
    calls = []; real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append((a, b)), real(a, b)))
    Graph().save(p)
    assert calls and Path(calls[0][1]) == p and not p.with_suffix(".tmp").exists()


def test_corrupt_file_loads_empty(tmp_path):
    p = tmp_path / "graph.json"; p.write_text("{not json", encoding="utf-8")
    assert Graph.load(p).nodes == {}


def test_missing_file_loads_empty(tmp_path):
    g = Graph.load(tmp_path / "nope.json")
    assert g.nodes == {} and g.edges == {} and g.meta == {}


def test_search_case_insensitive():
    assert graph_from_fixture().search("LOGIN")[0]["kind"] in {"file", "task", "command", "error"}


def test_search_newest_first_and_limit():
    g = Graph()
    g.add_node("file", "old_x.py", "old_x.py", "2026-01-01")
    g.add_node("file", "new_x.py", "new_x.py", "2026-03-01")
    g.add_node("file", "mid_x.py", "mid_x.py", "2026-02-01")
    assert [n["label"] for n in g.search("X.PY")] == ["new_x.py", "mid_x.py", "old_x.py"]
    assert len(g.search("x.py", limit=2)) == 2


def test_add_edge_without_nodes_and_repeat_counts():
    g = Graph()
    g.add_edge("a:1", "rel", "b:2", "t1"); g.add_edge("a:1", "rel", "b:2", "t2")
    e = g.edges["a:1|rel|b:2"]
    assert e == {"src": "a:1", "rel": "rel", "dst": "b:2", "count": 2, "last": "t2"}


def test_windows_path_normalised_and_read_edge():
    g = Graph()
    g.apply(Event("prompt", "t1", "s", {"text": "do it"}))
    g.apply(Event("read", "t2", "s", {"path": "src" + chr(92) + "a.py", "id": "r1"}))
    assert "file:src/a.py" in g.nodes
    assert g.meta["tool_nodes"]["r1"] == "file:src/a.py"
    assert any(e["rel"] == "read" and e["src"].startswith("task:") for e in g.edges.values())


def test_edit_before_any_task_links_session():
    g = Graph()
    g.apply(Event("edit", "t1", "s", {"path": "a.py", "id": "e1"}))
    assert "session:s|edited|file:a.py" in g.edges


def test_prompt_meta_and_session_edge():
    g = Graph()
    g.apply(Event("prompt", "t1", "s", {"text": "hello"}))
    tid = g.meta["current_task"]["s"]
    assert tid.startswith("task:") and f"session:s|asked|{tid}" in g.edges
    assert g.meta["last_prompt"] == {"text": "hello", "ts": "t1", "session": "s"}


def test_error_without_known_tool_links_session():
    g = Graph()
    g.apply(Event("error", "t1", "s", {"text": "boom", "tool_use_id": "zzz"}))
    assert any(e["rel"] == "failed" and e["src"] == "session:s" for e in g.edges.values())


def test_tool_nodes_capped_at_500_newest():
    g = Graph()
    for i in range(600):
        g.apply(Event("read", "t", "s", {"path": f"f{i}.py", "id": f"id{i}"}))
    tn = g.meta["tool_nodes"]
    assert len(tn) == 500 and "id599" in tn and "id100" in tn and "id99" not in tn


def test_none_ts_and_session_do_not_crash():
    g = Graph()
    g.apply(Event("prompt", None, None, {"text": "x"}))
    g.apply(Event("usage", None, None, {"context": 5}))
    assert g.meta["sessions"]["unknown"]["context"] == 5


def test_write_json_atomic_cleans_up_on_failure(tmp_path):
    import json
    import pytest
    from memgraph.graph import write_json_atomic
    p = tmp_path / "sub" / "out.json"
    with pytest.raises(TypeError):
        write_json_atomic(p, {"bad": object()})
    assert not p.exists() and not list(p.parent.glob("*.tmp"))
    write_json_atomic(p, {"ok": 1})
    assert json.loads(p.read_text(encoding="utf-8")) == {"ok": 1}
