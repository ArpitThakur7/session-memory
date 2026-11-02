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


def _long_request_graph():
    g = Graph()
    t = g.add_node("task", "t", "word " * 200, TS)
    g.add_edge("session:s1", "asked", t, TS)
    f = g.add_node("file", "f", "a.py", TS)
    g.add_edge(t, "edited", f, TS)
    e = g.add_node("error", "e", "oops", TS)
    g.add_edge(t, "failed", e, TS)
    return g


def test_meta_is_one_line_and_requests_use_four_lines_when_space_remains():
    lines = text_of(page(_long_request_graph())["flow"]).splitlines()
    assert not lines[2].endswith("\u2026") and lines[4].endswith("\u2026")
    assert lines[5] == "changed 1 file \u00b7 failed: oops"


def test_requests_stay_at_two_lines_when_space_is_short():
    lines = text_of(page(_long_request_graph(), cap=4)["flow"]).splitlines()
    assert lines[2].endswith("\u2026") and lines[3] == "changed 1 file \u00b7 failed: oops"


def test_short_story_is_balanced_across_both_columns():
    p = page(graph_from_fixture())
    left, right = p["columns"]
    assert left and right and abs(len(left) - len(right)) <= 3
    assert left[0][1] != [] and right[0][1] != []          # no blank line at the top of a column
    assert [ln for ln in p["flow"] if ln[1]] == [ln for ln in left + right if ln[1]]


def test_overflowing_story_fills_both_columns_to_the_cap():
    p = page(big_graph(2000), cap=20)
    assert [len(c) for c in p["columns"]] == [20, 20]
    assert text_of(p["columns"][1]).endswith(visual.MORE)


def test_next_steps_and_files_touched_fill_spare_space():
    g = graph_from_fixture()
    for _ in range(3):
        g.add_node("file", "C:/Users/me/proj/src/busy.py", "C:/Users/me/proj/src/busy.py", TS)
    text = text_of(page(g)["flow"])
    assert "Next steps" in text and "Fix login test" in text and "Add logout" in text
    assert "Create login form" not in text                       # completed
    assert text.index("Next steps") < text.index("Files touched")
    files = text[text.index("Files touched"):]
    assert files.index("busy.py") < files.index("login.py") and "C:/Users" not in text and "src/" not in files


def _story_len(g):
    flow = page(g)["flow"]
    heads = [i for i, (_ind, segs) in enumerate(flow) if segs and segs[0][0].startswith("Next steps")]
    return heads[0] - 1   # the blank line before the heading ends the story


def test_extras_need_space_and_keep_priority_order():
    g = graph_from_fixture()
    n = _story_len(g)
    none = text_of(page(g, cap=(n + 1) // 2)["flow"])
    assert "Next steps" not in none and "Files touched" not in none
    some = text_of(page(g, cap=(n + 4) // 2)["flow"])                # blank + heading + one step
    assert "Next steps" in some and "Fix login test" in some and "Files touched" not in some


def test_next_steps_heading_uses_local_time(monkeypatch):
    from datetime import timedelta, timezone

    from memgraph import transcript
    monkeypatch.setattr(transcript, "_LOCAL_TZ", timezone(timedelta(hours=5, minutes=30)))
    assert "Next steps (as of 2026-11-01 15:30)" in text_of(page(graph_from_fixture())["flow"])


def test_story_labels_drop_markup_from_older_graphs():
    g = Graph()
    t = g.add_node("task", "t", '<pasted_content id="a331">pasted body text', TS)
    g.add_edge("session:s1", "asked", t, TS)
    text = text_of(page(g)["flow"])
    assert "[pasted] pasted body text" in text and "<pasted_content" not in text


def test_scrub_urls_show_host_only_and_plain_text_is_untouched():
    assert visual.scrub("https://example.com/a/b.html") == "example.com"
    assert visual.scrub("see http://localhost:8000/x/y now") == "see localhost:8000 now"
    assert visual.scrub("file:///C:/Users/me/a.txt") == "a.txt"
    for same in ("and/or", "1/2", "11/02/2026"):
        assert visual.scrub(same) == same


def test_header_shows_data_note_and_built_stamp(tmp_path, monkeypatch):   # M4, I1
    import re

    pytest.importorskip("PIL")
    from PIL import ImageDraw

    from memgraph.brief import DATA_NOTE
    drawn = []
    orig = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *a, **k):
        drawn.append(text)
        return orig(self, xy, text, *a, **k)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    visual.render_visual_pack(graph_from_fixture(), tmp_path / "m.png")
    header = " ".join(drawn[:3])
    assert DATA_NOTE in header
    assert re.search(r"Built \d{4}-\d{2}-\d{2} \d{2}:\d{2}", header)
