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


def test_usage_without_context_keeps_stored_context():                # C1
    g = Graph()
    g.apply(Event("usage", "t1", "s", {"context": 5000}))
    g.apply(Event("usage", "t2", "s", {"context": None}))
    g.apply(Event("usage", "t3", "s", {}))
    assert g.meta["sessions"]["s"]["context"] == 5000



def test_last_prompt_replaced_only_by_newer_or_equal_ts():             # I1
    g = Graph()
    g.apply(Event("prompt", "2026-11-01T12:00:00.000Z", "new", {"text": "newer"}))
    g.apply(Event("prompt", "2026-11-01T10:00:00.000Z", "old", {"text": "older"}))
    assert g.meta["last_prompt"]["text"] == "newer"
    g.apply(Event("prompt", None, "x", {"text": "no ts"}))
    g.apply(Event("prompt", "garbage", "x", {"text": "bad ts"}))
    assert g.meta["last_prompt"]["text"] == "newer"
    g.apply(Event("prompt", "2026-11-01T12:00:00Z", "new", {"text": "same time"}))
    assert g.meta["last_prompt"]["text"] == "same time"


def test_last_todos_replaced_only_by_newer_ts_but_per_session_todos_always():   # I1
    g = Graph()
    newer = [{"content": "n", "status": "pending"}]
    older = [{"content": "o", "status": "pending"}]
    g.apply(Event("todo", "2026-11-01T12:00:00Z", "new", {"items": newer}))
    g.apply(Event("todo", "2026-11-01T10:00:00Z", "old", {"items": older}))
    assert g.meta["last_todos"]["session"] == "new" and g.meta["last_todos"]["items"] == newer
    assert g.meta["todos"]["old"] == older


def _todo_graph(statuses, session="s"):
    g = Graph()
    items = [{"content": f"t{i + 1}", "status": st} for i, st in enumerate(statuses)]
    g.apply(Event("todo", "2026-11-01T10:00:00Z", session, {"items": items}))
    return g


def _delta(advance=False, done=(), start=None, complete_all=False):
    return {"advance": advance, "done": list(done), "start": start, "complete_all": complete_all}


def _statuses(items):
    return [i["status"] for i in items]


def test_todo_delta_advance_moves_one_step_in_both_lists():          # I2
    g = _todo_graph(["completed", "in_progress", "pending", "pending"])
    g.apply(Event("todo_delta", "2026-11-01T10:05:00Z", "s", _delta(advance=True)))
    want = ["completed", "completed", "in_progress", "pending"]
    assert _statuses(g.meta["todos"]["s"]) == want and _statuses(g.meta["last_todos"]["items"]) == want
    assert g.meta["last_todos"]["ts"] == "2026-11-01T10:05:00Z"


def test_todo_delta_done_start_and_complete_all():                   # I2
    g = _todo_graph(["pending", "pending", "pending", "pending"])
    g.apply(Event("todo_delta", "2026-11-01T10:05:00Z", "s", _delta(done=[1, 2, 9], start=4)))
    assert _statuses(g.meta["last_todos"]["items"]) == ["completed", "completed", "pending", "in_progress"]
    g.apply(Event("todo_delta", "2026-11-01T10:06:00Z", "s", _delta(complete_all=True)))
    assert set(_statuses(g.meta["last_todos"]["items"])) == {"completed"}
    assert set(_statuses(g.meta["todos"]["s"])) == {"completed"}


def test_todo_delta_of_other_session_leaves_last_todos_alone():      # I2
    g = _todo_graph(["in_progress", "pending"], session="a")
    g.apply(Event("todo", "2026-11-01T11:00:00Z", "b", {"items": [{"content": "b1", "status": "in_progress"}]}))
    g.apply(Event("todo_delta", "2026-11-01T11:05:00Z", "a", _delta(advance=True)))
    assert _statuses(g.meta["todos"]["a"]) == ["completed", "in_progress"]
    assert g.meta["last_todos"]["session"] == "b" and _statuses(g.meta["last_todos"]["items"]) == ["in_progress"]
    g.apply(Event("todo_delta", "2026-11-01T11:06:00Z", "nobody", _delta(advance=True)))  # no todos: no crash
