import os
from pathlib import Path

import pytest

from memgraph import pack
from memgraph.brief import DATA_NOTE, where_things_stand
from memgraph.graph import Graph
from memgraph import transcript
from tests.helpers import graph_from_fixture

_IST = __import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30))


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


def _graph_with_file(label):
    g = Graph()
    ts = "2026-11-01T10:00:00Z"
    g.meta["last_prompt"] = {"text": "fix café 日本語", "ts": ts, "session": "s"}
    t = g.add_node("task", "t", "fix café 日本語", ts)
    g.add_edge("session:s", "asked", t, ts)
    f = g.add_node("file", label, label, ts)
    g.add_edge(t, "edited", f, ts)
    return g


def test_non_ascii_paths_survive(tmp_path):
    root = tmp_path / "proj"
    label = (root / "src" / "ünï.py").as_posix()
    out = pack.build_pack(_graph_with_file(label), "standard", root)
    assert "café 日本語" in out and "- src/ünï.py" in out


@pytest.mark.skipif(os.name != "nt", reason="backslash drive paths are absolute only on Windows")
def test_windows_backslash_paths_survive():
    out = pack.build_pack(_graph_with_file(r"C:\proj\src\ünï.py"), "standard", Path("C:/proj"))
    assert "- src/ünï.py" in out


def test_write_pack_atomic(tmp_path):
    path = pack.write_pack(graph_from_fixture(), tmp_path)
    assert path == tmp_path / "resume-pack.md"
    assert path.read_text(encoding="utf-8").splitlines()[1] == DATA_NOTE
    assert [p.name for p in tmp_path.iterdir()] == ["resume-pack.md"]


# ---- I1: built stamp, never an empty pack, unchanged pack keeps its file ------

def test_built_line_follows_data_note(monkeypatch):
    monkeypatch.setattr(transcript, "_LOCAL_TZ", _IST)
    from datetime import datetime, timezone
    lines = pack.build_pack(graph_from_fixture(), "tiny", now=datetime(2026, 11, 2, 14, 30)).splitlines()
    assert lines[1] == DATA_NOTE and lines[2] == "Built 2026-11-02 14:30"
    aware = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
    local = aware.astimezone(_IST).strftime("%Y-%m-%d %H:%M")
    assert pack.build_pack(graph_from_fixture(), now=aware).splitlines()[2] == f"Built {local}"
    assert pack.build_pack(graph_from_fixture()).splitlines()[2].startswith("Built 20")


def test_write_pack_never_writes_an_empty_pack(tmp_path):
    (tmp_path / "resume-pack.md").write_text("old pack", encoding="utf-8")
    assert pack.write_pack(Graph(), tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_write_pack_leaves_an_unchanged_pack_alone(tmp_path):
    from datetime import datetime
    g = graph_from_fixture()
    path = pack.write_pack(g, tmp_path, now=datetime(2026, 11, 2, 9, 0))
    os.utime(path, (1_000_000_000, 1_000_000_000))
    assert pack.write_pack(g, tmp_path, now=datetime(2026, 11, 2, 10, 0)) == path
    assert path.stat().st_mtime == 1_000_000_000  # same content: the picture made with it stays current
    assert "Built 2026-11-02 09:00" in path.read_text(encoding="utf-8")
    g.meta["last_prompt"]["text"] = "something new"
    pack.write_pack(g, tmp_path, now=datetime(2026, 11, 2, 10, 0))
    assert "Built 2026-11-02 10:00" in path.read_text(encoding="utf-8")


def test_timeline_dates_and_times_are_local(monkeypatch):
    monkeypatch.setattr(transcript, "_LOCAL_TZ", _IST)
    g = Graph()
    g.apply(transcript.Event("prompt", "2026-11-01T20:00:00Z", "s1", {"text": "late request"}))
    out = pack.build_pack(g, "standard")
    assert "### 2026-11-02 s1" in out and "- 01:30 late request" in out
    assert "Last request (2026-11-02 01:30): late request" in out
