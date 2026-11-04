"""Small, import-light file helpers shared by hooks: the memory folder and atomic JSON files.

Kept free of other memgraph imports so fast hooks (read guard, status line) start quickly.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

EVENTS_FILE = "events.jsonl"


def project_root(cwd) -> Path:
    """The project a hook works for: CLAUDE_PROJECT_DIR (set by Claude Code for hooks), else `cwd`.

    Using the root keeps one memory folder per project even when Claude has cd'd into a subfolder."""
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    return Path(env) if env else Path(cwd)


def memory_path(cwd) -> Path:
    """Where the project's memory folder is (or would be); never creates it. For read-only commands."""
    return Path(cwd) / ".claude" / "session-memory"


def existing_memory_dir(cwd) -> Path | None:
    """The project's memory folder when it already exists, else None."""
    d = memory_path(cwd)
    return d if d.is_dir() else None


def memory_dir(cwd: Path) -> Path:
    """The per-project memory directory, created if missing.

    It holds a `.gitignore` of `*` so graph.json (commands, prompts, errors)
    is never committed by accident; an existing .gitignore is left alone.
    """
    d = Path(cwd) / ".claude" / "session-memory"
    d.mkdir(parents=True, exist_ok=True)
    try:
        with open(d / ".gitignore", "x", encoding="utf-8", newline="\n") as fh:
            fh.write("*\n")
    except OSError:  # already there (FileExistsError) or not writable: memory still works
        pass
    return d


def load_state(path: Path) -> dict:
    """Load a JSON object file; a missing or corrupt file yields {}."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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


def append_event(mem_dir: Path, event: dict) -> None:
    """Append one JSON line to events.jsonl with a single O_APPEND write (no lock needed).

    The record starts with a newline too, so a torn earlier write cannot swallow it."""
    obj = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}
    data = ("\n" + json.dumps(obj, ensure_ascii=True) + "\n").encode("ascii")
    fd = os.open(Path(mem_dir) / EVENTS_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
