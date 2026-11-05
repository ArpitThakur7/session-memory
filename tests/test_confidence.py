import json

from memgraph.brief import build_brief
from memgraph.graph import SCHEMA_VERSION, Graph, edge_conf
from memgraph.memory_ops import recall_lines
from memgraph.transcript import Event

TS = "2026-11-05T10:00:00Z"


def ev(kind, **data):
    return Event(kind, TS, "s1", data)


def build():
    g = Graph()
    g.apply(ev("prompt", text="make the login form work"))
    g.apply(ev("edit", path="src/login.py", id="t1"))
    g.apply(ev("command", cmd="pytest tests", id="t2"))
    g.apply(ev("error", text="Traceback: File src/login.py line 3, in form: NameError", tool_use_id="t2"))
    return g


def test_direct_edges_are_extracted_and_heuristic_mentions_inferred():
    g = build()
    rels = {e["rel"]: e for e in g.edges.values()}
    for rel in ("asked", "edited", "ran", "failed"):
        assert rels[rel]["conf"] == "extracted"
    assert rels["mentions"]["conf"] == "inferred"
    assert rels["mentions"]["src"].startswith("error:") and rels["mentions"]["dst"] == "file:src/login.py"


def test_extracted_wins_over_inferred_for_the_same_link():
    g = Graph()
    g.add_edge("a", "mentions", "b", TS)
    g.add_edge("a", "mentions", "b", TS, conf="extracted")
    assert g.edges["a|mentions|b"]["conf"] == "extracted"
    g.add_edge("a", "mentions", "b", TS, conf="inferred")
    assert g.edges["a|mentions|b"]["conf"] == "extracted"


def test_mentions_only_known_files_and_whole_names():
    g = Graph()
    g.apply(ev("prompt", text="x"))
    g.apply(ev("edit", path="src/app.py", id="t1"))
    g.apply(ev("command", cmd="run", id="t2"))
    g.apply(ev("error", text="failed in src/happy.py and other/thing.py", tool_use_id="t2"))
    assert not [e for e in g.edges.values() if e["rel"] == "mentions"]


def test_save_writes_schema_version_and_old_graphs_load(tmp_path):
    path = tmp_path / "graph.json"
    build().save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION == 2
    old = {"nodes": {"task:a": {"id": "task:a", "kind": "task", "label": "x"}},
           "edges": {"s|asked|task:a": {"src": "s", "rel": "asked", "dst": "task:a", "count": 1, "last": TS},
                     "e|mentions|f": {"src": "e", "rel": "mentions", "dst": "f", "count": 1, "last": TS, "conf": "bogus"},
                     "junk": "not an edge"},
           "meta": {}}
    path.write_text(json.dumps(old), encoding="utf-8")
    g = Graph.load(path)
    assert g.edges["s|asked|task:a"]["conf"] == "extracted"
    assert g.edges["e|mentions|f"]["conf"] == "inferred"
    assert edge_conf("junk") == "extracted"  # never crashes on a malformed entry
    g.save(path)
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2


def test_newer_schema_still_loads(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text(json.dumps({"schema_version": 99, "nodes": {}, "edges": {}, "meta": {"x": 1}}), encoding="utf-8")
    assert Graph.load(path).meta == {"x": 1}


def test_recall_shows_tag_and_inferred_only_ranks_lower():
    g = Graph()
    g.add_node("file", "a", "docs/alpha-notes.md", "2026-11-05T09:00:00Z")
    g.add_node("file", "b", "docs/beta-notes.md", "2026-11-01T09:00:00Z")
    g.add_edge("error:x", "mentions", "file:a", TS)       # only a guess links alpha
    g.add_edge("task:t", "edited", "file:b", TS)          # beta was edited
    lines = recall_lines(g, "notes")
    assert lines[0].startswith("[file] docs/beta-notes.md") and "extracted" in lines[0]
    assert "inferred" in lines[1]


def test_brief_error_line_names_linked_file_and_tag():
    b = build_brief(build())
    line = next(x for x in b.splitlines() if "NameError" in x)
    assert "src/login.py" in line and "inferred" in line
