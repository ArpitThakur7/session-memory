"""A small JSON-backed memory graph built from transcript events."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memgraph.transcript import Event, parse_ts

_MAX_TOOL_NODES = 500
_MAX_LABEL = 200
_MAX_RECENT_PROMPTS = 5
_NO_TIME = datetime.min.replace(tzinfo=timezone.utc)  # sorts entries with no usable time last


def _is_newer(ts, stored) -> bool:
    """May an event at `ts` replace the dict `stored` (which has a "ts")?

    Yes when nothing is stored yet, or when `ts` parses and is at or after the
    stored time (a stored value with an unparseable time is replaceable). An
    event whose own time is missing or unparseable never replaces a value.
    """
    if not isinstance(stored, dict):
        return True
    new = parse_ts(ts)
    if new is None:
        return False
    old = parse_ts(stored.get("ts"))
    return old is None or new >= old


def _apply_todo_delta(items, delta: dict) -> None:
    """Apply an update_plan/wrap_up delta to a todo list in place (indices are 1-based)."""
    if not isinstance(items, list):
        return
    todos = [t for t in items if isinstance(t, dict)]

    def mark(n, status):
        if isinstance(n, int) and 1 <= n <= len(todos):
            todos[n - 1]["status"] = status

    for n in delta.get("done") or []:
        mark(n, "completed")
    mark(delta.get("start"), "in_progress")
    if delta.get("advance"):
        for t in todos:
            if t.get("status") == "in_progress":
                t["status"] = "completed"
                break
        for t in todos:
            if t.get("status") == "pending":
                t["status"] = "in_progress"
                break
    if delta.get("complete_all"):
        for t in todos:
            t["status"] = "completed"


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


LOCK_TIMEOUT = 2.0  # seconds a writer waits for graph.lock before giving up
LOCK_STALE_SECONDS = 30  # a lock older than this was left by a writer that died
_LOCK_POLL = 0.02


class GraphBusy(Exception):
    """Another writer held graph.lock for longer than the timeout."""


def _lock_path(path: Path) -> Path:
    return Path(path).with_name(Path(path).stem + ".lock")


@contextlib.contextmanager
def graph_lock(path: Path, timeout: float | None = None):
    """Hold `<graph>.lock` (created with O_EXCL) next to the graph at `path`.

    Waits up to `timeout` seconds (default LOCK_TIMEOUT), replacing a lock older
    than LOCK_STALE_SECONDS; raises GraphBusy when it cannot get the lock in time.
    """
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    give_up = time.monotonic() + (LOCK_TIMEOUT if timeout is None else timeout)
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            # Windows: a delete-pending or briefly open lock file raises PermissionError
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                    lock.unlink()
                    continue
            except OSError:
                pass  # vanished meanwhile, or held open on Windows: just retry
            if time.monotonic() >= give_up:
                raise GraphBusy(f"{lock.name} is held by another writer") from None
            time.sleep(_LOCK_POLL)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        break
    try:
        yield
    finally:
        try:
            if lock.read_text(encoding="utf-8") == token:
                for attempt in range(5):
                    try:
                        lock.unlink()
                        break
                    except PermissionError:  # Windows: briefly held open by a scanner or reader
                        if attempt == 4:
                            raise
                        time.sleep(0.02)
        except OSError:
            pass


def update(path: Path, fn, timeout: float | None = None):
    """Locked load-modify-save of the graph at `path`; returns fn(graph).

    Raises GraphBusy when the lock cannot be taken in time (nothing is written).
    """
    with graph_lock(path, timeout):
        graph = Graph.load(path)
        result = fn(graph)
        graph.save(path)
        return result


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
            entry = {"text": text, "ts": ts, "session": sid}
            if _is_newer(ts, self.meta.get("last_prompt")):
                self.meta["last_prompt"] = dict(entry)
            self._remember_prompt(entry)
        elif kind == "reply":
            text = str(data.get("text") or "")
            if text and _is_newer(ts, self.meta.get("last_reply")):
                self.meta["last_reply"] = {"text": text, "ts": ts, "session": sid}
        elif kind == "title":
            text = str(data.get("text") or "")
            # A title the user set (/rename) wins over Claude Code's generated one.
            if text and (data.get("source") == "custom" or sess.get("title_source") != "custom"):
                sess["title"] = text
                sess["title_source"] = data.get("source")
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
            items = [dict(t) for t in data.get("items") or [] if isinstance(t, dict)]
            self.meta.setdefault("todos", {})[sid] = items
            if _is_newer(ts, self.meta.get("last_todos")):
                # A separate copy, so a later delta is applied to each list exactly once.
                self.meta["last_todos"] = {"session": sid, "ts": ts, "items": [dict(t) for t in items]}
        elif kind == "todo_delta":
            _apply_todo_delta(self.meta.get("todos", {}).get(sid), data)
            last = self.meta.get("last_todos")
            if isinstance(last, dict) and last.get("session") == sid:
                _apply_todo_delta(last.get("items"), data)
                if _is_newer(ts, last):
                    last["ts"] = ts
        elif kind == "usage":
            if isinstance(data.get("context"), int):
                sess["context"] = data["context"]

    def _remember_prompt(self, entry: dict) -> None:
        """Keep meta["recent_prompts"]: the newest prompts overall by time, no exact repeats."""
        recent = [r for r in self.meta.get("recent_prompts") or [] if isinstance(r, dict) and r != entry]
        recent.append(entry)
        recent.sort(key=lambda r: parse_ts(r.get("ts")) or _NO_TIME, reverse=True)
        self.meta["recent_prompts"] = recent[:_MAX_RECENT_PROMPTS]

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
