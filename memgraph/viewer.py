"""Render the memory graph as a self-contained, offline 3D map (one HTML file)."""
from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from memgraph.brief import display_path, where_things_stand
from memgraph.graph import Graph, edge_conf
from memgraph.pack import PACK_FILE
from memgraph.visual import IMAGE_FILE, IMAGE_SIZE, has_story, render_visual_pack

TEMPLATE_PATH = Path(__file__).with_name("viewer_template.html")
PLACEHOLDER = "/*__MEMORY_DATA__*/null"
MAP_FILE = "memory-map.html"

_MAX_LABEL = 2000

# Characters that could end the <script> element or break a JS string literal.
_SCRIPT_SAFE = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", " ": "\\u2028", " ": "\\u2029"}


def _text(value) -> str:
    return str(value or "")[:_MAX_LABEL]


def _label(node: dict, project_root: Path | None) -> str:
    label = str(node.get("label", ""))
    if node.get("kind") == "file":
        label = display_path(label, project_root) or label  # outside the project: as recorded
    return label[:_MAX_LABEL]


IMAGE_TOKENS = round(IMAGE_SIZE[0] * IMAGE_SIZE[1] / 750)  # 1590

_log = logging.getLogger(__name__)


def _no_snapshot(reason: str | None) -> dict:
    return {"available": False, "data_uri": None, "tokens": IMAGE_TOKENS, "built": "", "reason": reason}


def snapshot_view(graph: Graph, mem_dir: Path, project_root: Path | None = None) -> dict:
    """Render a fresh visual pack into `mem_dir` and describe it for the page; never raises."""
    try:
        target = Path(mem_dir) / IMAGE_FILE
        png = render_visual_pack(graph, target, project_root)
        if png is None:
            return _no_snapshot("nothing-to-draw" if not has_story(graph) else "pillow-missing")
        data = base64.b64encode(Path(png).read_bytes()).decode("ascii")
        return {"available": True, "data_uri": "data:image/png;base64," + data, "tokens": IMAGE_TOKENS,
                "built": datetime.now().strftime("%Y-%m-%d %H:%M"), "reason": None}
    except Exception:  # the map must still draw
        _log.exception("snapshot render failed")
        return _no_snapshot(None)


def build_view_data(graph: Graph, project_name: str, project_root: Path | None = None,
                    tokens: dict | None = None, pack_path: str = "", snapshot: dict | None = None) -> dict:
    """The plain JSON-able data the map page draws (`tokens` from savings.token_view).

    The session tape inside `tokens` is lifted to the top-level `tape` key."""
    nodes = [
        {"id": str(n.get("id", nid)), "kind": str(n.get("kind", "")), "label": _label(n, project_root),
         "count": n.get("count") if isinstance(n.get("count"), int) else 1,
         "first": str(n.get("first") or ""), "last": str(n.get("last") or "")}
        for nid, n in graph.nodes.items() if isinstance(n, dict)
    ]
    ids = {n["id"] for n in nodes}
    edges = [
        {"s": e["src"], "d": e["dst"], "r": str(e.get("rel", "")),
         "c": e.get("count") if isinstance(e.get("count"), int) else 1, "f": edge_conf(e)}
        for e in graph.edges.values()
        if isinstance(e, dict) and e.get("src") in ids and e.get("dst") in ids
    ]
    sessions = []
    for sid, s in (graph.meta.get("sessions") or {}).items():
        if not isinstance(s, dict):
            continue
        sessions.append({"id": f"session:{sid}", "title": _text(s.get("title")),
                         "first": str(s.get("first") or ""), "last": str(s.get("last") or ""),
                         "context": s.get("context") if isinstance(s.get("context"), int) else 0})

    def meta_text(key: str) -> str:
        entry = graph.meta.get(key)
        return _text(entry.get("text")) if isinstance(entry, dict) else ""

    where = _text(where_things_stand(graph))

    todos = graph.meta.get("last_todos")
    items = todos.get("items") if isinstance(todos, dict) else None
    next_steps = [{"content": _text(t.get("content")), "status": _text(t.get("status"))}
                  for t in items or [] if isinstance(t, dict)]
    if not isinstance(tokens, dict):
        tokens = {"available": False, "reason": "No Claude Code transcripts found for this project."}
    tape = tokens.get("tape")
    if not isinstance(tape, dict):
        tape = {"available": False, "reason": str(tokens.get("reason") or "No session tape for this project.")}
    tokens = {k: v for k, v in tokens.items() if k != "tape"}
    return {
        "project": str(project_name),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nodes": nodes,
        "edges": edges,
        "sessions": sessions,
        "brief": {"last_request": meta_text("last_prompt"), "where": where,
                  "next_steps": next_steps},
        "tokens": tokens,
        "tape": tape,
        "pack_path": _text(pack_path),
        "snapshot": snapshot if isinstance(snapshot, dict) else _no_snapshot("nothing-to-draw"),
    }


def _script_json(data: dict) -> str:
    text = json.dumps(data, ensure_ascii=False)
    for char, escaped in _SCRIPT_SAFE.items():
        text = text.replace(char, escaped)
    return text


def render_map(graph: Graph, project_name: str, project_root: Path | None = None,
               tokens: dict | None = None, pack_path: str = "", snapshot: dict | None = None) -> str:
    """The whole map page as one HTML string, with the data embedded."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = _script_json(build_view_data(graph, project_name, project_root, tokens, pack_path, snapshot))
    return template.replace(PLACEHOLDER, payload, 1)


def write_map(graph: Graph, mem_dir: Path, project_name: str, project_root: Path | None = None,
              tokens: dict | None = None) -> Path:
    """Write the page to `mem_dir`/memory-map.html atomically and return its path."""
    path = Path(mem_dir) / MAP_FILE
    pack = Path(mem_dir) / PACK_FILE
    pack_path = (display_path(str(pack), project_root) or str(pack)) if pack.is_file() else ""
    snapshot = snapshot_view(graph, Path(mem_dir), project_root)
    html = render_map(graph, project_name, project_root, tokens, pack_path, snapshot)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(html)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
