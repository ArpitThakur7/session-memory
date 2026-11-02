import json
import re
import shutil
import subprocess

import pytest

from memgraph.graph import Graph
from memgraph.viewer import build_view_data, render_map, write_map
from tests.helpers import graph_from_fixture

DATA_PREFIX = "const DATA = "
CSP = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
       "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:\">")


def embedded_data(html: str):
    """Parse the JSON that render_map put after `const DATA = `."""
    start = html.index(DATA_PREFIX) + len(DATA_PREFIX)
    obj, _end = json.JSONDecoder().raw_decode(html, start)
    return obj


def test_build_view_data_shape():
    g = graph_from_fixture()
    data = build_view_data(g, "RepoQuest")
    assert set(data) >= {"project", "generated", "nodes", "edges", "sessions", "brief"}
    assert data["project"] == "RepoQuest"
    assert len(data["nodes"]) == len(g.nodes)
    node = data["nodes"][0]
    assert set(node) >= {"id", "kind", "label", "count", "first", "last"}
    ids = {n["id"] for n in data["nodes"]}
    assert data["edges"] and all(e["s"] in ids and e["d"] in ids for e in data["edges"])
    assert set(data["edges"][0]) >= {"s", "d", "r", "c"}
    assert data["sessions"] == [{"id": "session:s1", "title": "", "first": "2026-11-01T10:00:00.000Z",
                                 "last": "2026-11-01T10:00:30.000Z", "context": 42010}]
    assert data["brief"]["last_request"] == "Add login page to the app"
    assert "Fix login test" in [s["content"] for s in data["brief"]["next_steps"]]
    assert {"content": "Fix login test", "status": "in_progress"} in data["brief"]["next_steps"]
    json.dumps(data)  # JSON-able


def test_build_view_data_drops_dangling_edges_and_caps_labels():
    g = Graph()
    a = g.add_node("task", "a", "x" * 5000, "2026-11-01T10:00:00Z")
    g.add_edge(a, "ran", "command:missing", "2026-11-01T10:00:01Z")
    data = build_view_data(g, "p")
    assert data["edges"] == []
    assert len(data["nodes"][0]["label"]) == 2000
    assert data["brief"] == {"last_request": "", "where": "", "next_steps": []}


def test_file_labels_relative_to_project_root(tmp_path):
    root = tmp_path / "proj"
    g = Graph()
    inside = g.add_node("file", "in", str(root / "src" / "app.py"), "")
    outside = g.add_node("file", "out", str(tmp_path / "other" / "x.py"), "")
    rel = g.add_node("file", "rel", "docs/readme.md", "")
    labels = {n["id"]: n["label"] for n in build_view_data(g, "proj", root)["nodes"]}
    assert labels[inside] == "src/app.py"
    assert labels[outside] == str(tmp_path / "other" / "x.py")  # outside the project: kept as recorded
    assert labels[rel] == "docs/readme.md"


def test_render_map_embeds_data_safely():
    evil = "</script><script>alert(1)</script> &<>"
    g = Graph()
    g.add_node("task", "evil", evil, "2026-11-01T10:00:00Z")
    html = render_map(g, "p")
    template = (__import__("memgraph.viewer", fromlist=["x"]).TEMPLATE_PATH).read_text(encoding="utf-8")
    assert "/*__MEMORY_DATA__*/null" in template
    assert "/*__MEMORY_DATA__*/" not in html
    assert html.count("</script>") == template.count("</script>") == html.count("<script")
    assert "alert(1)</script>" not in html and " " not in html and " " not in html
    data = embedded_data(html)
    assert [n["label"] for n in data["nodes"]] == [evil]


def test_render_map_has_no_external_resources():
    html = render_map(graph_from_fixture(), "p")
    assert "http://" not in html and "https://" not in html
    assert CSP in html
    assert not re.search(r"<(link|img|iframe)\b", html)
    assert not re.search(r"<script[^>]*\bsrc=", html)


def test_empty_graph_renders():
    html = render_map(Graph(), "x")
    assert "No memory yet. Work in this project with the plugin installed, then run /memory-map again." in html
    assert embedded_data(html)["nodes"] == []


def test_write_map_atomic_and_path(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    path = write_map(graph_from_fixture(), mem, "p")
    assert path == mem / "memory-map.html"
    assert path.exists() and "Add login page to the app" in path.read_text(encoding="utf-8")
    assert [p.name for p in mem.iterdir()] == ["memory-map.html"]


def test_page_script_is_valid_javascript(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    html = render_map(graph_from_fixture(), "p")
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert len(scripts) == 1
    js = tmp_path / "page.js"
    js.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_where_things_stand_only_from_last_requests_session():
    g = Graph()
    g.meta["last_prompt"] = {"text": "Add logout", "ts": "2026-11-01T10:00:00Z", "session": "a"}
    g.meta["last_reply"] = {"text": "Old reply", "ts": "2026-11-01T09:00:00Z", "session": "b"}
    assert build_view_data(g, "p")["brief"]["where"] == ""
    g.meta["last_reply"] = {"text": "Logout added", "ts": "2026-11-01T10:01:00Z", "session": "a"}
    assert build_view_data(g, "p")["brief"]["where"] == "Logout added"
