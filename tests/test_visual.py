import builtins
import importlib.util
import struct

import pytest

from memgraph import visual
from memgraph.graph import Graph
from tests.helpers import graph_from_fixture
from tests.test_pack import big_graph

needs_pil = pytest.mark.skipif(importlib.util.find_spec("PIL") is None, reason="Pillow not installed")
TS = "2026-11-01T10:00:00Z"


class FakeFont:
    def getlength(self, text):
        return 7 * len(text)


def text_of(lines):
    return "\n".join("".join(t for t, _s in segs) for _i, segs in lines)


def page(g, cap=60):
    return visual.compose(g, FakeFont(), 500, cap)


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


@needs_pil
def test_image_is_1092_square_png(tmp_path):
    out = visual.render_visual_pack(graph_from_fixture(), tmp_path / "m.png")
    assert out == tmp_path / "m.png" and png_size(out) == (1092, 1092)
    assert [p.name for p in tmp_path.iterdir()] == ["m.png"]


def test_returns_none_without_pillow(tmp_path, monkeypatch):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert visual.render_visual_pack(graph_from_fixture(), tmp_path / "m.png") is None
    assert not (tmp_path / "m.png").exists()


def test_empty_graph_returns_none(tmp_path):
    assert visual.render_visual_pack(Graph(), tmp_path / "m.png") is None
    assert not visual.has_story(Graph()) and visual.has_story(graph_from_fixture())
    assert list(tmp_path.iterdir()) == []


@needs_pil
def test_long_history_fits(tmp_path):
    out = visual.render_visual_pack(big_graph(2000), tmp_path / "m.png")
    assert png_size(out) == (1092, 1092)


@needs_pil
def test_non_ascii_renders(tmp_path):
    g = Graph()
    g.meta["sessions"] = {"s1": {"title": "Ünïcödé 日本語 café"}}
    t = g.add_node("task", "t", "Réparer le bogue ñandú — naïve", TS)
    g.add_edge("session:s1", "asked", t, TS)
    out = visual.render_visual_pack(g, tmp_path / "m.png", tmp_path)
    assert png_size(out) == (1092, 1092)


def test_paths_and_long_commands_do_not_leak():
    g = Graph()
    t = g.add_node("task", "t", "Fix in C:\\Users\\me\\proj\\src\\x.py and /home/me/proj/a/b.py now", TS)
    g.add_edge("session:s1", "asked", t, TS)
    cmd = g.add_node("command", "c", "run", TS)
    g.add_edge(t, "ran", cmd, TS)
    err = g.add_node("error", "e", "boom C:\\Users\\me\\proj\\src\\x.py:3 in /home/me/proj/a/b.py `" + "pytest --flag " * 5 + "`", TS)
    g.add_edge(cmd, "failed", err, TS)
    g.add_node("decision", "d", "use C:/work/repo/lib/core.py because /srv/app/data/y.txt is stale", TS)
    out = text_of(page(g)["flow"])
    for bad in ("C:\\Users", "C:/work", "/home/me", "/srv/app", "pytest --flag"):
        assert bad not in out
    assert "x.py" in out and "b.py" in out and "core.py" in out and "failed: boom" in out


def test_failure_on_file_node_goes_to_editing_task_and_newest_task_wins():
    g = Graph()
    g.meta["sessions"] = {"s1": {"title": "S"}}
    for tid, ts in (("old", "2026-11-01T09:00:00Z"), ("new", "2026-11-01T10:00:00Z")):
        t = g.add_node("task", tid, f"request {tid}", ts)
        g.add_edge("session:s1", "asked", t, ts)
    shared = g.add_node("command", "c", "pytest", TS)
    g.add_edge("task:old" if "task:old" in g.nodes else next(i for i, n in g.nodes.items() if n["label"] == "request old"),
               "ran", shared, "2026-11-01T09:00:00Z")
    ids = {n["label"]: i for i, n in g.nodes.items()}
    g.add_edge(ids["request new"], "ran", shared, "2026-11-01T10:00:00Z")
    f = g.add_node("file", "f", "src/a.py", TS)
    g.add_edge(ids["request old"], "edited", f, "2026-11-01T09:30:00Z")
    e1 = g.add_node("error", "e1", "cmd failure", TS)
    e2 = g.add_node("error", "e2", "file failure", TS)
    # failure edges added BEFORE ownership edges would be order-dependent; add them last and first both ways
    g.add_edge(shared, "failed", e1, TS)
    g.add_edge(f, "failed", e2, TS)
    lines = text_of(page(g)["flow"]).splitlines()
    i_new, i_old = lines.index("\u2022 request new"), lines.index("\u2022 request old")
    assert "failed: cmd failure" in lines[i_new + 1]
    assert "changed 1 file" in lines[i_old + 1] and "failed: file failure" in lines[i_old + 1]


def test_failure_attribution_ignores_edge_order():
    g = Graph()
    t = g.add_node("task", "t", "request", TS)
    f = g.add_node("file", "f", "a.py", TS)
    e = g.add_node("error", "e", "bad", TS)
    g.add_edge(f, "failed", e, TS)          # failure first,
    g.add_edge("session:s1", "asked", t, TS)
    g.add_edge(t, "read", f, TS)            # ownership after
    assert "failed: bad" in text_of(page(g)["flow"])


def test_story_lines_marker_footer_failures_decisions():
    g = big_graph(2000)
    p = page(g, cap=20)
    text = text_of(p["flow"])
    assert len(p["flow"]) == 40 and text.endswith(visual.MORE)
    assert p["footer"] == "Exact paths and commands: see resume-pack.md"
    assert "Decisions" in text and "decided " in text
    assert any(style == "red" and t.startswith("failed: ") for _i, segs in p["flow"] for t, style in segs)
    small = text_of(page(graph_from_fixture())["flow"])
    assert visual.MORE not in small and "failed: " in small


def test_decisions_come_first():
    g = big_graph(40)
    assert page(g)["flow"][0][1][0][0] == "Decisions"


def test_meta_is_one_line_and_requests_capped_at_two_lines():
    g = Graph()
    t = g.add_node("task", "t", "word " * 200, TS)
    g.add_edge("session:s1", "asked", t, TS)
    f = g.add_node("file", "f", "a.py", TS)
    g.add_edge(t, "edited", f, TS)
    e = g.add_node("error", "e", "oops", TS)
    g.add_edge(t, "failed", e, TS)
    lines = text_of(page(g)["flow"]).splitlines()
    assert lines[2].endswith("\u2026") and lines[3] == "changed 1 file \u00b7 failed: oops"
