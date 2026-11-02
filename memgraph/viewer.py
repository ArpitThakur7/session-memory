"""Render the memory graph as a self-contained, offline 3D map (one HTML file)."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from memgraph.brief import display_path, where_things_stand
from memgraph.graph import Graph

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


def build_view_data(graph: Graph, project_name: str, project_root: Path | None = None,
                    tokens: dict | None = None) -> dict:
    """The plain JSON-able data the map page draws (`tokens` from savings.token_view)."""
    nodes = [
        {"id": str(n.get("id", nid)), "kind": str(n.get("kind", "")), "label": _label(n, project_root),
         "count": n.get("count") if isinstance(n.get("count"), int) else 1,
         "first": str(n.get("first") or ""), "last": str(n.get("last") or "")}
        for nid, n in graph.nodes.items() if isinstance(n, dict)
    ]
    ids = {n["id"] for n in nodes}
    edges = [
        {"s": e["src"], "d": e["dst"], "r": str(e.get("rel", "")),
         "c": e.get("count") if isinstance(e.get("count"), int) else 1}
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
    return {
        "project": str(project_name),
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nodes": nodes,
        "edges": edges,
        "sessions": sessions,
        "brief": {"last_request": meta_text("last_prompt"), "where": where,
                  "next_steps": next_steps},
        "tokens": tokens if isinstance(tokens, dict) else {
            "available": False, "reason": "No Claude Code transcripts found for this project."},
    }


def _script_json(data: dict) -> str:
    text = json.dumps(data, ensure_ascii=False)
    for char, escaped in _SCRIPT_SAFE.items():
        text = text.replace(char, escaped)
    return text


def render_map(graph: Graph, project_name: str, project_root: Path | None = None,
               tokens: dict | None = None) -> str:
    """The whole map page as one HTML string, with the data embedded."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = _script_json(build_view_data(graph, project_name, project_root, tokens))
    return template.replace(PLACEHOLDER, payload, 1)


def write_map(graph: Graph, mem_dir: Path, project_name: str, project_root: Path | None = None,
              tokens: dict | None = None) -> Path:
    """Write the page to `mem_dir`/memory-map.html atomically and return its path."""
    path = Path(mem_dir) / MAP_FILE
    html = render_map(graph, project_name, project_root, tokens)
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
