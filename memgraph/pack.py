"""Text resume pack: a bounded markdown summary of the memory graph, written at 0 model tokens."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from memgraph.brief import (DATA_NOTE, _changed_files, _fmt_ts, _newest, _one_line, _request_line,
                            where_things_stand)
from memgraph.graph import Graph
from memgraph.transcript import clean_prompt, fmt_local, local_time

SIZES = {"tiny": 2000, "standard": 8000, "deep": 24000}
PACK_FILE = "resume-pack.md"
BUILT_PREFIX = "Built "

# (todos, decisions, files, errors) shown per size before trimming to the cap.
_LIMITS = {"tiny": (5, 5, 8, 3), "standard": (10, 10, 15, 5), "deep": (20, 20, 40, 10)}
_MAX_TODO_TEXT = 200
_MAX_LINE = 300


def _clip(text) -> str:
    return _one_line(text)[:_MAX_LINE]


def _time(ts) -> tuple[str, str]:
    """Local (date, HH:MM) of a stored timestamp; ("", "") when unparseable."""
    return fmt_local(ts, "%Y-%m-%d"), fmt_local(ts, "%H:%M")


def _timeline(graph: Graph, with_commands: bool) -> list[str]:
    sessions = graph.meta.get("sessions") or {}
    asked: dict[str, list[dict]] = {}
    ran: dict[str, list[dict]] = {}
    for e in graph.edges.values():
        node = graph.nodes.get(e.get("dst"))
        if not node:
            continue
        if e.get("rel") == "asked" and node.get("kind") == "task":
            asked.setdefault(str(e.get("src", "")).removeprefix("session:"), []).append(node)
        elif e.get("rel") == "ran" and node.get("kind") == "command":
            ran.setdefault(e.get("src"), []).append(node)
    order = sorted(asked, key=lambda s: ((sessions.get(s) or {}).get("last") or "", s), reverse=True)
    lines: list[str] = []
    for sid in order:
        meta = sessions.get(sid) or {}
        first = min((n.get("first") or "" for n in asked[sid]), default="")
        date = _time(meta.get("first") or first)[0]
        lines.append(f"### {date} {_clip(meta.get('title') or sid)}".replace("###  ", "### "))
        for task in sorted(asked[sid], key=lambda n: (n.get("first") or "", str(n.get("label", "")))):
            hhmm = _time(task.get("first"))[1]
            lines.append(f"- {hhmm} {_clip(clean_prompt(task.get('label', '')))}".replace("-  ", "- "))
            if with_commands:
                for cmd in _newest(ran.get(task.get("id"), []), 5):
                    lines.append(f"  - ran: {_clip(cmd.get('label', ''))}")
    return lines


def built_line(now: datetime | None = None) -> str:
    """The line `Built YYYY-MM-DD HH:MM` in local time (`now` defaults to the current time)."""
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is not None:  # a naive `now` is already local
        now = local_time(now)
    return f"{BUILT_PREFIX}{now.strftime('%Y-%m-%d %H:%M')}"


def build_pack(graph: Graph, size: str = "standard", project_root: Path | None = None,
               now: datetime | None = None) -> str:
    """Markdown resume pack of at most SIZES[size] characters; "" for an empty graph.

    The third line says when it was built (local time, `now` defaults to the clock)."""
    if size not in SIZES:
        raise ValueError(f"unknown size {size!r}; expected one of {', '.join(SIZES)}")
    cap = SIZES[size]
    nodes = list(graph.nodes.values())
    by_kind = lambda kind: [n for n in nodes if n.get("kind") == kind]  # noqa: E731
    last_prompt = graph.meta.get("last_prompt")
    if not last_prompt and not any(by_kind(k) for k in ("decision", "file", "error", "task")):
        return ""
    n_todo, n_dec, n_files, n_err = _LIMITS[size]

    todos = (graph.meta.get("last_todos") or {}).get("items") or []
    stamp = _fmt_ts((graph.meta.get("last_todos") or {}).get("ts"))
    sections: list[tuple[str, list[str]]] = [
        (f"## Next steps (as of {stamp})" if stamp else "## Next steps",
         [f"- [{_one_line(t.get('status', ''))}] {_one_line(t.get('content', ''))[:_MAX_TODO_TEXT]}"
          for t in todos if isinstance(t, dict) and t.get("status") != "completed"][:n_todo]),
        ("## Decisions", [f"- {_clip(n.get('label', ''))}" for n in _newest(by_kind("decision"), n_dec)]),
        ("## Changed files", [l for l in _changed_files(graph, by_kind("file"), project_root)][:n_files]),
        ("## Recent errors", [f"- {_clip(n.get('label', ''))}" for n in _newest(by_kind("error"), n_err)]),
    ]
    if size != "tiny":
        sections.append(("## Timeline", _timeline(graph, size == "deep")))

    session_title = request_text = None
    reply_text = where_things_stand(graph)
    if last_prompt:
        sid = last_prompt.get("session")
        title = ((graph.meta.get("sessions") or {}).get(sid) or {}).get("title")
        session_title = _one_line(title) if title else None
        request_text = _one_line(clean_prompt(last_prompt.get("text", "")))

    name = project_root.name if project_root is not None and project_root.name else "project"
    built = built_line(now)

    def render() -> str:
        lines = [f"# Resume pack — {name}", DATA_NOTE, built]
        if session_title:
            lines.append(f"Session: {session_title}")
        if request_text is not None:
            lines.append(_request_line(last_prompt, request_text))
        if reply_text:
            lines.append(f"Where things stand: {reply_text}")
        for heading, items in sections:
            if items:
                lines += ["", heading, *items]
        return "\n".join(lines) + "\n"

    out = render()
    for _heading, items in reversed(sections):  # lowest priority first, bottom lines first
        over = len(out) - cap
        while items and over > 0:
            over -= len(items.pop()) + 1
            if items and items[-1].startswith("### "):  # no session heading without requests
                over -= len(items.pop()) + 1
        out = render()
        if len(out) <= cap:
            break
    if len(out) > cap and reply_text:
        reply_text = reply_text[: max(0, len(reply_text) - (len(out) - cap))].rstrip()
        out = render()
    if len(out) > cap and request_text is not None:
        request_text = request_text[: max(0, len(request_text) - (len(out) - cap))].rstrip()
        out = render()
    return out[:cap]


def _without_built(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.startswith(BUILT_PREFIX)]


def write_pack(graph: Graph, mem_dir: Path, size: str = "standard", project_root: Path | None = None,
               now: datetime | None = None) -> Path | None:
    """Write the pack to `mem_dir`/resume-pack.md atomically and return the path.

    Never writes an empty pack: with nothing to pack an existing file is removed
    and None is returned. When only the "Built" line would change, the file is
    left alone, so a memory.png made with it still counts as current.
    """
    path = Path(mem_dir) / PACK_FILE
    text = build_pack(graph, size, project_root, now)
    if not text:
        path.unlink(missing_ok=True)
        return None
    try:
        if _without_built(path.read_text(encoding="utf-8")) == _without_built(text):
            return path
    except (OSError, ValueError):
        pass  # missing or unreadable: write it
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
