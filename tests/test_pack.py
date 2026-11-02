import os
from pathlib import Path

import pytest

from memgraph import pack
from memgraph.brief import DATA_NOTE, where_things_stand
from memgraph.graph import Graph
from tests.helpers import graph_from_fixture


def big_graph(n=2000) -> Graph:
    g = Graph()
    g.meta["sessions"] = {"s1": {"title": "Big " + "t" * 50}}
    for i in range(n // 4):
        ts = f"2026-11-01T10:{i % 60:02d}:00Z"
        t = g.add_node("task", f"t{i}", f"request number {i} " + "word " * 30, ts)
        g.add_edge("session:s1", "asked", t, ts)
        f = g.add_node("file", f"f{i}", f"src/file{i}.py", ts)
        g.add_edge(t, "edited", f, ts)
        c = g.add_node("command", f"c{i}", f"pytest -k case{i} " + "x" * 80, ts)
        g.add_edge(t, "ran", c, ts)
        e = g.add_node("error", f"e{i}", f"error {i} " + "y" * 100, ts)
        g.add_edge(t, "failed", e, ts)
        g.add_node("decision", f"d{i}", f"decided {i} " + "z" * 100, ts)
    g.meta["last_prompt"] = {"text": "last " + "q" * 3000, "ts": "2026-11-01T11:00:00Z", "session": "s1"}
    g.meta["last_reply"] = {"text": "reply " + "r" * 3000, "ts": "2026-11-01T11:01:00Z", "session": "s1"}
    g.meta["last_todos"] = {"session": "s1", "ts": "2026-11-01T11:00:00Z",
                            "items": [{"content": f"todo {i}", "status": "pending"} for i in range(30)]}
    return g


@pytest.mark.parametrize("size", ["tiny", "standard", "deep"])
def test_sizes_cap(size):
    out = pack.build_pack(big_graph(), size)
    assert 0 < len(out) <= pack.SIZES[size]


def test_empty_graph_empty_pack():
    assert pack.build_pack(Graph()) == ""


def test_unknown_size_raises():
    with pytest.raises(ValueError):
        pack.build_pack(graph_from_fixture(), "huge")


def test_order_and_data_note():
    lines = pack.build_pack(graph_from_fixture(), "standard", Path("/x/myproj")).splitlines()
    assert lines[0] == "# Resume pack — myproj"
    assert lines[1] == DATA_NOTE
    assert pack.build_pack(graph_from_fixture()).splitlines()[0] == "# Resume pack — project"


def test_tiny_has_no_timeline_deep_has_commands():
    g = big_graph(40)
    g.meta["last_prompt"]["text"] = "short"
    g.meta["last_reply"]["text"] = "short"
    assert "## Timeline" not in pack.build_pack(g, "tiny")
    std = pack.build_pack(g, "standard")
    assert "## Timeline" in std and "  - ran:" not in std
    deep = pack.build_pack(g, "deep")
    assert "### 2026-11-01" in deep and "  - ran: pytest -k case" in deep


def test_where_uses_shared_session_rule():
    g = big_graph(8)
    assert "Where things stand: reply" in pack.build_pack(g)
    g.meta["last_reply"]["session"] = "other"
    assert where_things_stand(g) == ""
    assert "Where things stand" not in pack.build_pack(g)


def test_non_ascii_and_windows_paths_survive():
    g = Graph()
    g.meta["last_prompt"] = {"text": "fix café 日本語", "ts": "2026-11-01T10:00:00Z", "session": "s"}
    t = g.add_node("task", "t", "fix café 日本語", "2026-11-01T10:00:00Z")
    g.add_edge("session:s", "asked", t, "2026-11-01T10:00:00Z")
    f = g.add_node("file", "C:/proj/src/ünï.py", "C:/proj/src/ünï.py", "2026-11-01T10:00:00Z")
    g.add_edge(t, "edited", f, "2026-11-01T10:00:00Z")
    out = pack.build_pack(g, "standard", Path("C:/proj"))
    assert "café 日本語" in out and "- src/ünï.py" in out


def test_write_pack_atomic(tmp_path):
    path = pack.write_pack(graph_from_fixture(), tmp_path)
    assert path == tmp_path / "resume-pack.md"
    assert path.read_text(encoding="utf-8").splitlines()[1] == DATA_NOTE
    assert [p.name for p in tmp_path.iterdir()] == ["resume-pack.md"]
