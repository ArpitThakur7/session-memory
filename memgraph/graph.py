"""A small JSON-backed memory graph built from transcript events."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from memgraph.store import write_json_atomic  # noqa: F401  (re-exported)
from memgraph.transcript import Event, parse_ts

_MAX_TOOL_NODES = 500
_MAX_LABEL = 200
_MAX_RECENT_PROMPTS = 5
SCHEMA_VERSION = 2  # 2: every edge has "conf" ("extracted" or "inferred")
EXTRACTED, INFERRED = "extracted", "inferred"
CONFS = (EXTRACTED, INFERRED)
_REL_CONF = {"mentions": INFERRED}  # links guessed from text; the rest were seen in a transcript turn
_MAX_MENTIONS = 3
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


def edge_conf(edge) -> str:
    """An edge's confidence tag; a missing or unknown tag falls back to the tag its relation implies."""
    if not isinstance(edge, dict):
        return EXTRACTED
    conf = edge.get("conf")
    return conf if conf in CONFS else _REL_CONF.get(edge.get("rel"), EXTRACTED)


def node_confs(graph) -> dict[str, str]:
    """Node id -> "extracted" when any edge touching it is, else "inferred"; nodes without edges are absent."""
    out: dict[str, str] = {}
    for e in graph.edges.values():
        if not isinstance(e, dict):
            continue
        conf = edge_conf(e)
        for nid in (e.get("src"), e.get("dst")):
            if out.get(nid) != EXTRACTED:
                out[nid] = conf
    return out


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]


@dataclass
class _Turn:
    """What every event handler needs: the event's time and data, and its chat's nodes."""
    ts: str
    stamp: str          # ts, or "" when the event has no time
    sid: str
    data: dict
    session: dict       # meta["sessions"][sid]
    session_node: str
    current: str        # the request this work belongs to (or the chat's node)


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
            for edge in edges.values():  # graphs before schema 2 have no tags; any version is read the same way
                if isinstance(edge, dict) and edge.get("conf") not in CONFS:
                    edge["conf"] = edge_conf(edge)
        return g

    def save(self, path: Path) -> None:
        """Atomically write the graph to `path`."""
        write_json_atomic(path, {"schema_version": SCHEMA_VERSION, "nodes": self.nodes, "edges": self.edges, "meta": self.meta})

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

    def add_edge(self, src: str, rel: str, dst: str, ts: str, conf: str | None = None) -> None:
        """Add or count a link. `conf` defaults to the relation's tag; an extracted sighting upgrades an inferred link."""
        conf = conf if conf in CONFS else _REL_CONF.get(rel, EXTRACTED)
        key = f"{src}|{rel}|{dst}"
        edge = self.edges.get(key)
        if edge is None:
            self.edges[key] = {"src": src, "rel": rel, "dst": dst, "count": 1, "last": ts, "conf": conf}
        else:
            if conf == EXTRACTED:
                edge["conf"] = EXTRACTED
            edge["count"] += 1
            if ts:
                edge["last"] = ts

    def apply(self, event: Event) -> None:
        """Add one transcript event to the graph (and to the session's bookkeeping in meta)."""
        data = event.data or {}
        if event.kind == "error" and data.get("tool_use_id") not in self.meta.get("tool_nodes", {}):
            return  # only failures of edit/read/command tools are the project's own errors

        sid = event.session or "unknown"
        session = self.meta.setdefault("sessions", {}).setdefault(sid, {})
        if event.ts:
            session.setdefault("first", event.ts)
            session["last"] = event.ts
        session_node = self.add_node("session", sid, sid, event.ts or "")
        turn = _Turn(
            ts=event.ts, stamp=event.ts or "", sid=sid, data=data, session=session,
            session_node=session_node,
            # work (edits, commands, ...) hangs off the request it answers, or the chat when there is none yet
            current=self.meta.get("current_task", {}).get(sid) or session_node,
        )
        handler = self._HANDLERS.get(event.kind)
        if handler is not None:
            handler(self, turn, event.kind)

    def _on_prompt(self, t: "_Turn", _kind: str) -> None:
        text = str(t.data.get("text") or "")
        task = self.add_node("task", _digest(text), text[:_MAX_LABEL], t.stamp)
        self.add_edge(t.session_node, "asked", task, t.stamp)
        self.meta.setdefault("current_task", {})[t.sid] = task
        entry = {"text": text, "ts": t.ts, "session": t.sid}
        if _is_newer(t.ts, self.meta.get("last_prompt")):
            self.meta["last_prompt"] = dict(entry)
        self._remember_prompt(entry)

    def _on_reply(self, t: "_Turn", _kind: str) -> None:
        text = str(t.data.get("text") or "")
        if text and _is_newer(t.ts, self.meta.get("last_reply")):
            self.meta["last_reply"] = {"text": text, "ts": t.ts, "session": t.sid}

    def _on_title(self, t: "_Turn", _kind: str) -> None:
        text = str(t.data.get("text") or "")
        # A title the user set (/rename) wins over Claude Code's generated one.
        if text and (t.data.get("source") == "custom" or t.session.get("title_source") != "custom"):
            t.session["title"] = text
            t.session["title_source"] = t.data.get("source")

    def _on_file(self, t: "_Turn", kind: str) -> None:
        path = t.data.get("path")
        if not path:
            return
        path = str(path).replace("\\", "/")
        node = self.add_node("file", path, path, t.stamp)
        self.add_edge(t.current, "edited" if kind == "edit" else "read", node, t.stamp)
        self._remember_tool(t.data.get("id"), node)

    def _on_command(self, t: "_Turn", _kind: str) -> None:
        cmd = str(t.data.get("cmd") or "")
        node = self.add_node("command", _digest(cmd), cmd[:_MAX_LABEL], t.stamp)
        self.add_edge(t.current, "ran", node, t.stamp)
        self._remember_tool(t.data.get("id"), node)

    def _on_error(self, t: "_Turn", _kind: str) -> None:
        text = str(t.data.get("text") or "")
        node = self.add_node("error", _digest(text), text[:_MAX_LABEL], t.stamp)
        failed_tool = self.meta["tool_nodes"][t.data["tool_use_id"]]
        self.add_edge(failed_tool, "failed", node, t.stamp)
        self._link_mentions(node, text, failed_tool, t.stamp)

    def _on_todo(self, t: "_Turn", _kind: str) -> None:
        items = [dict(item) for item in t.data.get("items") or [] if isinstance(item, dict)]
        self.meta.setdefault("todos", {})[t.sid] = items
        if _is_newer(t.ts, self.meta.get("last_todos")):
            # A separate copy, so a later delta is applied to each list exactly once.
            self.meta["last_todos"] = {"session": t.sid, "ts": t.ts, "items": [dict(item) for item in items]}

    def _on_todo_delta(self, t: "_Turn", _kind: str) -> None:
        _apply_todo_delta(self.meta.get("todos", {}).get(t.sid), t.data)
        last = self.meta.get("last_todos")
        if isinstance(last, dict) and last.get("session") == t.sid:
            _apply_todo_delta(last.get("items"), t.data)
            if _is_newer(t.ts, last):
                last["ts"] = t.ts

    def _on_usage(self, t: "_Turn", _kind: str) -> None:
        if isinstance(t.data.get("context"), int):
            t.session["context"] = t.data["context"]

    _HANDLERS = {
        "prompt": _on_prompt, "reply": _on_reply, "title": _on_title,
        "edit": _on_file, "read": _on_file, "command": _on_command, "error": _on_error,
        "todo": _on_todo, "todo_delta": _on_todo_delta, "usage": _on_usage,
    }

    def _link_mentions(self, error_id: str, text: str, skip: str, stamp: str) -> None:
        """Inferred links from an error to known file nodes whose path (or file name) its text names."""
        flat = text.replace("\\", "/")
        words = set(re.findall(r"[\w.-]+", flat))
        added = 0
        for nid, node in self.nodes.items():
            if node.get("kind") != "file" or nid == skip:
                continue
            path = str(node.get("label") or "")
            name = path.rsplit("/", 1)[-1]
            if not path or not ((len(path) > 4 and re.search(r"(?<![\w.-])" + re.escape(path) + r"(?![\w-])", flat))
                                or ("." in name and len(name) >= 5 and name in words)):
                continue
            self.add_edge(error_id, "mentions", nid, stamp)
            added += 1
            if added >= _MAX_MENTIONS:
                break

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
