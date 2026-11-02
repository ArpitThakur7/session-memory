import builtins
import struct

import pytest

from memgraph import visual
from memgraph.graph import Graph
from tests.helpers import graph_from_fixture
from tests.test_pack import big_graph

PIL = pytest.importorskip("PIL")


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


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
    assert list(tmp_path.iterdir()) == []


def test_long_history_fits(tmp_path):
    out = visual.render_visual_pack(big_graph(2000), tmp_path / "m.png")
    assert png_size(out) == (1092, 1092)


def test_non_ascii_renders(tmp_path):
    g = Graph()
    g.meta["sessions"] = {"s1": {"title": "Ünïcödé 日本語 café"}}
    t = g.add_node("task", "t", "Réparer le bogue ñandú — naïve", "2026-11-01T10:00:00Z")
    g.add_edge("session:s1", "asked", t, "2026-11-01T10:00:00Z")
    out = visual.render_visual_pack(g, tmp_path / "m.png", tmp_path)
    assert png_size(out) == (1092, 1092)
