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
    data = build_view_data(g, "demo-project")
    assert set(data) >= {"project", "generated", "nodes", "edges", "sessions", "brief"}
    assert data["project"] == "demo-project"
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
    assert not [p for p in mem.iterdir() if p.suffix == ".tmp"]


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


def test_page_has_time_range_picker():
    html = render_map(graph_from_fixture(), "demo-project")
    for label in ("1 hour", "5 hours", "1 day", "7 days", "All time", "Time range"):
        assert label in html


def test_view_data_tokens_absent_without_transcripts(tmp_path):
    data = build_view_data(graph_from_fixture(), "p")
    assert data["tokens"]["available"] is False
    html = render_map(graph_from_fixture(), "p")
    assert 'id="tokens"' in html and "<h2>" in html


def test_view_data_tokens_from_transcripts(tmp_path):
    from datetime import datetime, timezone

    from memgraph.savings import token_view
    from tests.test_savings import seed

    t, mem = seed(tmp_path)
    tokens = token_view(t, mem, now=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc))
    assert tokens["available"] is True and tokens["prices_as_of"] == "2026-10-06"
    assert len(tokens["days"]) == 14 and tokens["days"][-2]["key"] == "2026-11-01"
    assert tokens["days"][-2]["replies"] == 7 and tokens["days"][0]["replies"] == 0
    assert tokens["totals"]["replies"] == 7 and tokens["duplicates"] == 1
    assert {m["key"] for m in tokens["models"]} == {"claude-opus-5-5"}
    assert {w["key"] for w in tokens["who"]} == {"main", "helper"}
    assert tokens["savings"]["totals"]["measured_saved_tokens"] == 120003 - 20004
    assert tokens["savings"]["fresh_starts"][0]["cache_state"] in ("warm", "cold")
    html_t = render_map(graph_from_fixture(), "p")
    assert "all time" in html_t and "cache_state" in html_t
    html = render_map(graph_from_fixture(), "p", tokens=tokens)
    assert embedded_data(html)["tokens"]["totals"]["replies"] == 7


# ---------------------------------------------------------------- session tape

def _tape_tokens(tmp_path, ttl=60):
    from datetime import datetime, timezone

    from memgraph.savings import token_view
    from tests.test_savings import seed

    t, mem = seed(tmp_path)
    return token_view(t, mem, now=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc), ttl_minutes=ttl)


def test_tape_ticks_are_main_replies_only_sorted_numbers(tmp_path):
    tape = _tape_tokens(tmp_path)["tape"]
    assert tape["available"] is True and tape["ttl_minutes"] == 60
    ticks = tape["ticks"]
    # old: 3 replies (duplicate dropped), new: 2 replies; the helper's 2 replies never appear
    assert [tk[0][:16] for tk in ticks] == ["2026-11-01T09:00", "2026-11-01T09:01", "2026-11-01T11:01",
                                            "2026-11-01T11:30", "2026-11-01T11:31"]
    assert all(len(tk) == 4 for tk in ticks)
    assert ticks[0][1] == 3 + 120000 and ticks[0][2] == 120000
    assert ticks[1][1] == 3 + 500 + 119000 and ticks[1][2] == 500
    assert ticks[0][3] == pytest.approx((3 * 4 + 120000 * 8 + 50 * 20) / 1e6)
    assert all(isinstance(tk[1], int) and isinstance(tk[2], int) for tk in ticks)


def test_tape_restarts_and_fresh_starts(tmp_path):
    tape = _tape_tokens(tmp_path)["tape"]
    (r,) = tape["restarts"]
    assert r["t"].startswith("2026-11-01T11:01") and r["session"] == "session:old"
    assert r["idle_minutes"] == 120 and r["rebuild_tokens"] == 120000
    assert r["rebuild_usd"] == pytest.approx(120000 * 8.0 / 1e6)
    (f,) = tape["fresh_starts"]
    assert f["t"].startswith("2026-11-01T11:30") and f["session"] == "session:new"
    assert f["saved"] == 120003 - 20004 and f["cache_state"] == "warm"
    assert set(r) == {"t", "session", "idle_minutes", "rebuild_tokens", "rebuild_usd"}
    assert set(f) == {"t", "session", "saved", "cache_state"}


def test_tape_is_capped_to_most_recent(tmp_path):
    from datetime import datetime, timedelta, timezone

    from memgraph.savings import tape_view
    from memgraph.usage import Reply, UsageResult

    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    replies = [Reply(ts=t0 + timedelta(seconds=i), session="s", model="x", helper=False, input=i,
                     cache_write_5m=0, cache_write_1h=0, cache_write_unknown=0, cache_read=0, output=0)
               for i in range(30)]
    tape = tape_view(UsageResult(replies=replies), {"fresh_starts": [], "cold_restarts": []}, 5, cap=10)
    assert [tk[1] for tk in tape["ticks"]] == list(range(20, 30))
    assert tape["ticks"][0][3] is None  # unknown model: no price
    assert tape["truncated"] == 20


def test_tape_carries_no_transcript_text(tmp_path):
    tape = _tape_tokens(tmp_path)["tape"]
    blob = json.dumps(tape)
    assert '"ok"' not in blob and "assistant" not in blob


def test_view_data_tape_top_level_and_not_duplicated(tmp_path):
    tokens = _tape_tokens(tmp_path)
    data = build_view_data(graph_from_fixture(), "p", tokens=tokens)
    assert data["tape"]["available"] is True and len(data["tape"]["ticks"]) == 5
    assert "tape" not in data["tokens"]
    assert "tape" in tokens  # the caller's dict is not changed
    html = render_map(graph_from_fixture(), "p", tokens=tokens)
    assert len(embedded_data(html)["tape"]["ticks"]) == 5


def test_view_data_tape_degrades_without_transcripts(tmp_path):
    from memgraph.savings import token_view

    data = build_view_data(graph_from_fixture(), "p")
    assert data["tape"]["available"] is False and data["tape"]["reason"]
    missing = token_view(tmp_path / "nope", tmp_path)
    data = build_view_data(graph_from_fixture(), "p", tokens=missing)
    assert data["tape"] == {"available": False, "reason": missing["reason"]}


def test_tape_counts_replies_per_main_session(tmp_path):
    tape = _tape_tokens(tmp_path)["tape"]
    assert tape["replies_by_session"] == {"session:old": 3, "session:new": 2}


def test_view_data_pack_path_relative_to_project(tmp_path):
    root = tmp_path / "proj"
    mem = root / ".claude" / "session-memory"
    mem.mkdir(parents=True)
    assert build_view_data(graph_from_fixture(), "p", root)["pack_path"] == ""
    (mem / "resume-pack.md").write_text("x", encoding="utf-8")
    path = write_map(graph_from_fixture(), mem, "p", root)
    assert embedded_data(path.read_text(encoding="utf-8"))["pack_path"] == ".claude/session-memory/resume-pack.md"


def test_page_never_builds_html_from_strings():
    from memgraph.viewer import TEMPLATE_PATH

    page = TEMPLATE_PATH.read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write"):
        assert banned not in page
    assert page.count("/*__MEMORY_DATA__*/null") == 1


def test_page_has_devtool_shell():
    html = render_map(graph_from_fixture(), "p")
    for marker in ('id="titlebar"', 'id="activity"', 'id="side"', 'id="insp"', 'id="panel"', 'id="status"',
                   'id="paletteWrap"', 'id="tapeCanvas"', 'data-tab="json"', 'data-ptab="problems"',
                   'id="sideSplit"', 'id="inspSplit"', 'id="panelSplit"', "Hand Claude this"):
        assert marker in html
    assert "backdrop-filter" in html and "@supports" in html  # glass with a solid fallback
    assert "prefers-reduced-motion" in html and ":focus-visible" in html
    assert "text-transform: uppercase" not in html and "@font-face" not in html and "@import" not in html


# ---- snapshot (visual pack on the map page) ----

def _story_graph():
    g = Graph()
    g.add_node("task", "t1", "Add login page to the app", "2026-11-01T10:00:00Z")
    return g


def test_view_data_snapshot_available(tmp_path):
    pytest.importorskip("PIL")
    from memgraph.viewer import snapshot_view
    import base64

    snap = snapshot_view(_story_graph(), tmp_path, None)
    assert snap["available"] is True and snap["reason"] is None
    assert snap["tokens"] == 1590 and snap["built"]
    prefix = "data:image/png;base64,"
    assert snap["data_uri"].startswith(prefix)
    assert base64.b64decode(snap["data_uri"][len(prefix):])[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(snap["data_uri"]) < 400_000


def test_view_data_snapshot_unavailable_reasons(tmp_path, monkeypatch):
    import memgraph.viewer as viewer

    monkeypatch.setattr(viewer, "render_visual_pack", lambda *a, **k: None)
    snap = viewer.snapshot_view(Graph(), tmp_path, None)
    assert snap == {"available": False, "data_uri": None, "tokens": 1590, "built": "", "reason": "nothing-to-draw"}
    monkeypatch.setattr(viewer, "has_story", lambda g: True)
    assert viewer.snapshot_view(_story_graph(), tmp_path, None)["reason"] == "pillow-missing"


def test_snapshot_render_failure_never_breaks_map(tmp_path, monkeypatch):
    import memgraph.viewer as viewer

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(viewer, "render_visual_pack", boom)
    monkeypatch.setattr(viewer, "has_story", lambda g: True)
    path = write_map(_story_graph(), tmp_path, "p")
    snap = embedded_data(path.read_text(encoding="utf-8"))["snapshot"]
    assert snap["available"] is False and snap["data_uri"] is None


def test_build_view_data_defaults_snapshot_unavailable():
    snap = build_view_data(Graph(), "p")["snapshot"]
    assert snap["available"] is False and snap["data_uri"] is None


def test_page_has_snapshot_view():
    html = render_map(graph_from_fixture(), "p")
    for marker in ('data-view="snapshot"', 'id="view-snapshot"', "'download', 'memory.png'", "Open snapshot",
                   "Save image", "One image of this chat", "Install Pillow to make the snapshot",
                   "Nothing to draw yet", 'id="lightbox"'):
        assert marker in html
    assert html.count("/*__MEMORY_DATA__*/null") == 0
    assert "http://" not in html and "https://" not in html
    assert CSP in html
