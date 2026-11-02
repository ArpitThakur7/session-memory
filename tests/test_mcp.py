import base64
import io
import json
import subprocess
import sys
from pathlib import Path

from memgraph import mcp_server
from memgraph.graph import Graph
from memgraph.ingest import memory_dir
from tests.helpers import graph_from_fixture


def seed(tmp_path, graph=None):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    (graph or graph_from_fixture()).save(memory_dir(proj) / "graph.json")
    return proj


def drive(project, *msgs):
    lines = [m if isinstance(m, str) else json.dumps(m) for m in msgs]
    data = b"".join(l.encode("utf-8") + b"\n" for l in lines)
    out = io.BytesIO()
    mcp_server.serve(io.BytesIO(data), out, project)
    return [json.loads(l) for l in out.getvalue().decode("utf-8").splitlines()]


def req(i, method, params=None):
    m = {"jsonrpc": "2.0", "id": i, "method": method}
    if params is not None:
        m["params"] = params
    return m


def call(i, name, **args):
    return req(i, "tools/call", {"name": name, "arguments": args})


def test_initialize_echoes_version(tmp_path):
    (r,) = drive(seed(tmp_path), req(1, "initialize", {"protocolVersion": "2025-03-26"}))
    assert r["id"] == 1
    assert r["result"] == {
        "protocolVersion": "2025-03-26",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "session-memory", "version": "0.2.0"},
    }


def test_tools_list_has_four_tools_with_schemas(tmp_path):
    (r,) = drive(seed(tmp_path), req(1, "tools/list"))
    tools = {t["name"]: t for t in r["result"]["tools"]}
    assert set(tools) == {"memory_brief", "memory_recall", "memory_remember", "memory_pack"}
    for t in tools.values():
        assert t["description"] and t["inputSchema"]["type"] == "object"
    assert tools["memory_recall"]["inputSchema"]["required"] == ["query"]
    assert tools["memory_pack"]["inputSchema"]["properties"]["size"]["enum"] == ["tiny", "standard", "deep"]


def test_brief_recall_remember_roundtrip(tmp_path):
    proj = seed(tmp_path)
    brief, miss, saved, found = drive(
        proj,
        call(1, "memory_brief"),
        call(2, "memory_recall", query="zzzqqq"),
        call(3, "memory_remember", text="Use sqlite for the cache"),
        call(4, "memory_recall", query="sqlite"),
    )
    assert "Fix login test" in brief["result"]["content"][0]["text"]
    assert miss["result"]["content"][0]["text"] == 'Nothing found for "zzzqqq".'
    assert saved["result"] == {"content": [{"type": "text", "text": "Saved to project memory."}], "isError": False}
    assert found["result"]["content"][0]["text"].startswith("[decision] Use sqlite for the cache")
    assert Graph.load(memory_dir(proj) / "graph.json").search("sqlite", 5)


def test_tool_errors_are_results_not_crashes(tmp_path):
    bad, bad2, unknown = drive(
        seed(tmp_path),
        call(1, "memory_recall", query=""),
        call(2, "memory_pack", size="huge"),
        call(3, "nope"),
    )
    assert bad["result"]["isError"] is True and bad2["result"]["isError"] is True
    assert unknown["error"]["code"] == -32602


def test_pack_text_and_image(tmp_path, monkeypatch):
    proj = seed(tmp_path)
    (plain,) = drive(proj, call(1, "memory_pack", size="tiny"))
    content = plain["result"]["content"]
    assert len(content) == 1 and content[0]["type"] == "text" and content[0]["text"]

    png_bytes = b"\x89PNG fake"

    def fake_render(graph, path, project_root=None):
        Path(path).write_bytes(png_bytes)
        return Path(path)

    monkeypatch.setattr(mcp_server, "has_story", lambda g: True)
    monkeypatch.setattr(mcp_server, "render_visual_pack", fake_render)
    (vis,) = drive(proj, call(1, "memory_pack", visual=True))
    img = vis["result"]["content"][1]
    assert img == {"type": "image", "data": base64.b64encode(png_bytes).decode(), "mimeType": "image/png"}

    monkeypatch.setattr(mcp_server, "render_visual_pack", lambda *a, **k: None)
    (nopil,) = drive(proj, call(1, "memory_pack", visual=True))
    assert nopil["result"]["content"][1] == {"type": "text", "text": "Visual pack unavailable: install Pillow"}


def test_unknown_method_error(tmp_path):
    (r,) = drive(seed(tmp_path), req(7, "resources/list"))
    assert r["id"] == 7 and r["error"]["code"] == -32601


def test_bad_json_error_and_keeps_running(tmp_path):
    bad, ping = drive(seed(tmp_path), "{not json", req(2, "ping"))
    assert bad["id"] is None and bad["error"]["code"] == -32700
    assert ping == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_notifications_get_no_reply(tmp_path):
    out = drive(
        seed(tmp_path),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "tools/list"},
        req(1, "ping"),
    )
    assert [r["id"] for r in out] == [1]


def test_non_ascii_output(tmp_path):
    proj = seed(tmp_path)
    _, found = drive(proj, call(1, "memory_remember", text="Décision: café naïve 日本語"),
                     call(2, "memory_recall", query="café"))
    text = found["result"]["content"][0]["text"]
    assert "café" in text and "日本語" in text


def test_serve_subprocess_smoke(tmp_path):
    proj = seed(tmp_path)
    msgs = [
        req(1, "initialize", {"protocolVersion": "2024-11-05"}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        req(2, "tools/list"),
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in msgs).encode("utf-8")
    root = Path(__file__).resolve().parent.parent
    p = subprocess.run([sys.executable, "-m", "memgraph.cli", "mcp", "--project", str(proj)],
                       input=stdin, capture_output=True, timeout=30, cwd=root)
    replies = [json.loads(l) for l in p.stdout.decode("utf-8").splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert len(replies[1]["result"]["tools"]) == 4
    assert p.returncode == 0


def test_lone_surrogate_does_not_kill_server(tmp_path):
    proj = seed(tmp_path)
    raw = r'{"jsonrpc":"2.0","id":"\ud800","method":"tools/call","params":{"name":"memory_recall","arguments":{"query":"\ud800"}}}'
    unknown = r'{"jsonrpc":"2.0","id":2,"method":"\ud800x"}'
    out = drive(proj, raw, unknown, req(3, "ping"))
    assert len(out) == 3 and out[2]["id"] == 3


def test_deeply_nested_line_keeps_running(tmp_path):
    out = drive(seed(tmp_path), "[" * 100000, req(2, "ping"))
    assert out[0]["error"]["code"] == -32700 and out[0]["id"] is None
    assert out[1]["id"] == 2


def test_batch_array_rejected_and_next_line_works(tmp_path):
    out = drive(seed(tmp_path), json.dumps([req(1, "ping")]), req(2, "ping"))
    assert out[0]["error"]["code"] == -32600 and out[1]["id"] == 2


def test_string_id_preserved(tmp_path):
    (r,) = drive(seed(tmp_path), req("abc-1", "ping"))
    assert r["id"] == "abc-1"


def test_unwritable_project_still_initializes(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    init, tool = drive(blocker / "proj", req(1, "initialize", {"protocolVersion": "2024-11-05"}),
                       call(2, "memory_brief"))
    assert "result" in init
    assert tool["result"]["isError"] is True


def test_real_pillow_visual_png(tmp_path):
    import importlib.util
    import pytest
    if importlib.util.find_spec("PIL") is None:
        pytest.skip("Pillow not installed")
    import struct
    (r,) = drive(seed(tmp_path), call(1, "memory_pack", visual=True))
    img = r["result"]["content"][1]
    assert img["type"] == "image"
    data = base64.b64decode(img["data"])
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (1092, 1092)


def test_brief_tool_points_to_pack(tmp_path):
    proj = seed(tmp_path)
    (memory_dir(proj) / "resume-pack.md").write_text("x", encoding="utf-8")
    (r,) = drive(proj, call(1, "memory_brief"))
    assert "Full resume pack: .claude/session-memory/resume-pack.md" in r["result"]["content"][0]["text"]
