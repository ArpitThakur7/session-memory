"""A small JSON-backed memory graph built from transcript events."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from memgraph.transcript import Event

_MAX_TOOL_NODES = 500
_MAX_LABEL = 200


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def write_json_atomic(path: Path, obj) -> None:
    """Write `obj` as JSON to `path` atomically: unique temp file beside it, then os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}
        self.meta: dict = {}

    # ---- persistence -------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "Graph":
        """Load a graph; a missing or corrupt file yields an empty graph."""
        g = cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return g
        if not isinstance(data, dict):
            return g
        nodes, edges, meta = data.get("nodes"), data.get("edges"), data.get("meta")
        if isinstance(nodes, dict) and isinstance(edges, dict) and isinstance(meta, dict):
            g.nodes, g.edges, g.meta = nodes, edges, meta
        return g

    def save(self, path: Path) -> None:
        """Atomically write the graph to `path`."""
        write_json_atomic(path, {"nodes": self.nodes, "edges": self.edges, "meta": self.meta})

    # ---- mutation ----------------------------------------------------

    def add_node(self, kind: str, key: str, label: str, ts: str) -> str:
        node_id = f"{kind}:{key}"
        node = self.nodes.get(node_id)
        if node is None:
            self.nodes[node_id] = {
                "id": node_id, "kind": kind, "label": label,
                "first": ts, "last": ts, "count": 1,
            }
        else:
            node["count"] += 1
            if ts:
                node["last"] = ts
                if not node["first"]:
                    node["first"] = ts
        return node_id

    def add_edge(self, src: str, rel: str, dst: str, ts: str) -> None:
        key = f"{src}|{rel}|{dst}"
        edge = self.edges.get(key)
        if edge is None:
            self.edges[key] = {"src": src, "rel": rel, "dst": dst, "count": 1, "last": ts}
        else:
            edge["count"] += 1
            if ts:
                edge["last"] = ts

    def apply(self, event: Event) -> None:
        ts = event.ts
        sid = event.session or "unknown"
        data = event.data or {}
        stamp = ts or ""

        sess = self.meta.setdefault("sessions", {}).setdefault(sid, {})
        if ts:
            sess.setdefault("first", ts)
            sess["last"] = ts
        session_node = self.add_node("session", sid, sid, stamp)
        current = self.meta.get("current_task", {}).get(sid) or session_node

        kind = event.kind
        if kind == "prompt":
            text = str(data.get("text") or "")
            task = self.add_node("task", _digest(text), text[:_MAX_LABEL], stamp)
            self.add_edge(session_node, "asked", task, stamp)
            self.meta.setdefault("current_task", {})[sid] = task
            self.meta["last_prompt"] = {"text": text, "ts": ts, "session": sid}
        elif kind in ("edit", "read"):
            path = data.get("path")
            if path:
                path = str(path).replace("\\", "/")
                node = self.add_node("file", path, path, stamp)
                self.add_edge(current, "edited" if kind == "edit" else "read", node, stamp)
                self._remember_tool(data.get("id"), node)
        elif kind == "command":
            cmd = str(data.get("cmd") or "")
            node = self.add_node("command", _digest(cmd), cmd[:_MAX_LABEL], stamp)
            self.add_edge(current, "ran", node, stamp)
            self._remember_tool(data.get("id"), node)
        elif kind == "error":
            text = str(data.get("text") or "")
            node = self.add_node("error", _digest(text), text[:_MAX_LABEL], stamp)
            src = self.meta.get("tool_nodes", {}).get(data.get("tool_use_id")) or session_node
            self.add_edge(src, "failed", node, stamp)
        elif kind == "todo":
            items = data.get("items") or []
            self.meta.setdefault("todos", {})[sid] = items
            self.meta["last_todos"] = {"session": sid, "ts": ts, "items": items}
        elif kind == "usage":
            if isinstance(data.get("context"), int):
                sess["context"] = data["context"]

    def _remember_tool(self, tool_id, node_id: str) -> None:
        if not tool_id:
            return
        tools = self.meta.setdefault("tool_nodes", {})
        tools.pop(tool_id, None)  # re-insert so it counts as newest
        tools[tool_id] = node_id
        while len(tools) > _MAX_TOOL_NODES:
            del tools[next(iter(tools))]

    # ---- query -------------------------------------------------------

    def search(self, term: str, limit: int = 20) -> list[dict]:
        needle = term.lower()
        hits = [n for n in self.nodes.values() if needle in str(n.get("label", "")).lower()]
        hits.sort(key=lambda n: n.get("last") or "", reverse=True)
        return hits[:limit]
