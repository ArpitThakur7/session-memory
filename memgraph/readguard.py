"""Read guard: stop Claude from re-reading a file it already read, unchanged, in this chat.

PreToolUse(Read) calls `check`, which only decides. PostToolUse(Read) calls `record`, so only
reads that actually succeeded are remembered (a declined prompt or a failed read never is).
Each chat (session id, plus agent id inside a helper agent) keeps, per normalized path, the
file's mtime_ns and size, the line ranges read with their times, and the compaction epoch, in
reads.json in the project's memory folder. A request is denied only when the file is unchanged,
the lines were read in the last 45 minutes (older tool results may have been cleared from the
context) and no compaction has happened since. A second identical request within 2 minutes of
a denial is allowed, so Claude can always get the file back. Anything odd (missing or > 2 MB
file, unparseable offset/limit, bad input) is allowed. SESSION_MEMORY_READ_GUARD=0 turns it off.
"""
from __future__ import annotations

import ntpath
import os
import posixpath
from datetime import datetime
from pathlib import Path

from memgraph.store import load_state, write_json_atomic

READS_FILE = "reads.json"
MAX_BYTES = 2 * 1024 * 1024          # bigger files: always allowed, never recorded
DEFAULT_LINES = 2000                 # lines Claude Code's Read returns without a limit
RETRY_SECONDS = 120
FRESH_SECONDS = 45 * 60              # older reads may have been cleared from the context
MAX_SCOPES = 30
MAX_PATHS = 500
MAX_RANGES = 50
REASON = ("Not re-read: {file} is unchanged since you read it at {at} in this chat. Use what you read then. "
          "If you truly need it again (for example after losing it from context), read it again within "
          "2 minutes and it will be allowed.")


def enabled() -> bool:
    return os.environ.get("SESSION_MEMORY_READ_GUARD", "1").strip() != "0"


def norm_path(path: str, nt: bool | None = None) -> str:
    """One spelling per file: on Windows case and slashes do not matter; POSIX stays case-sensitive."""
    mod = ntpath if (os.name == "nt" if nt is None else nt) else posixpath
    return mod.normcase(mod.normpath(path))


def scope_key(payload: dict) -> str | None:
    """Which context the read lands in: the session, or a helper agent inside it."""
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    agent = payload.get("agent_id")
    return f"{sid}/{agent}" if isinstance(agent, str) and agent else sid


def _as_int(value) -> int | None:
    """An int, or a numeric string such as "200"; None when it cannot be read as one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _span(tool_input: dict) -> tuple[int, int] | None:
    """(first line, line after the last) of a Read request; None when offset/limit cannot be read."""
    start, limit = 1, DEFAULT_LINES
    if tool_input.get("offset") is not None:
        offset = _as_int(tool_input["offset"])
        if offset is None or offset < 0:
            return None
        start = max(1, offset)
    if tool_input.get("limit") is not None:
        limit = _as_int(tool_input["limit"])
        if limit is None or limit <= 0:
            return None
    return start, start + limit


def _target(payload: dict):
    """(scope, path as given, normalized path, stat, span) for a guardable Read, else None."""
    if not enabled():
        return None
    tool_input = payload.get("tool_input")
    scope = scope_key(payload)
    if not isinstance(tool_input, dict) or scope is None or "pages" in tool_input:
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    span = _span(tool_input)
    if span is None:
        return None
    full = file_path if os.path.isabs(file_path) else os.path.join(str(payload.get("cwd") or ""), file_path)
    try:
        st = os.stat(full)
    except (OSError, ValueError):
        return None
    if not os.path.isfile(full) or st.st_size > MAX_BYTES:
        return None
    return scope, file_path, norm_path(os.path.abspath(full)), st, span


def _ranges(value) -> list[list]:
    return [r for r in (value if isinstance(value, list) else [])
            if isinstance(r, list) and len(r) == 3 and isinstance(r[0], int) and isinstance(r[1], int)
            and isinstance(r[2], (int, float)) and not isinstance(r[2], bool)]


def _covered_at(start: int, end: int, ranges: list[list], since: float) -> float | None:
    """When the requested lines were last read (all of them, by reads after `since`), else None."""
    fresh = sorted(r for r in ranges if r[2] >= since)
    pos, latest = start, None
    for s, e, at in fresh:
        if s > pos:
            break
        if e > pos:
            latest = at if latest is None else max(latest, at)
            pos = e
        if pos >= end:
            return latest
    return None


def _epoch(state: dict, scope: str) -> int:
    epochs = state.get("epochs")
    value = epochs.get(scope, 0) if isinstance(epochs, dict) else 0
    return value if isinstance(value, int) else 0


def _scope_files(state: dict, scope: str) -> tuple[dict, dict]:
    scopes = state.get("scopes")
    if not isinstance(scopes, dict):
        scopes = state["scopes"] = {}
    entry = scopes.get(scope)
    if not isinstance(entry, dict):
        entry = scopes[scope] = {}
    files = entry.get("files")
    if not isinstance(files, dict):
        files = entry["files"] = {}
    return entry, files


def _prune(state: dict) -> None:
    scopes = state["scopes"]
    if len(scopes) > MAX_SCOPES:
        def seen(item):
            v = item[1].get("seen") if isinstance(item[1], dict) else None
            return v if isinstance(v, (int, float)) else 0
        state["scopes"] = dict(sorted(scopes.items(), key=seen)[-MAX_SCOPES:])
    epochs = state.get("epochs")
    if isinstance(epochs, dict):
        state["epochs"] = {k: v for k, v in epochs.items() if k in state["scopes"]}


def _save(mem: Path, state: dict) -> None:
    try:
        write_json_atomic(mem / READS_FILE, state)
    except OSError:
        pass  # a parallel Read hook won the race; losing one record only means one fewer denial


def _clock(at: float) -> str:
    from memgraph.transcript import _LOCAL_TZ

    return datetime.fromtimestamp(at, _LOCAL_TZ).strftime("%H:%M")


def _current(rec, st, epoch: int) -> bool:
    return (isinstance(rec, dict) and rec.get("mtime_ns") == st.st_mtime_ns and rec.get("size") == st.st_size
            and rec.get("compact_epoch") == epoch)


def check(payload: dict, mem: Path, now: datetime) -> str | None:
    """PreToolUse: the deny reason for this Read, or None to allow it. Records no read."""
    target = _target(payload)
    if target is None:
        return None
    scope, file_path, key, st, (start, end) = target
    state = load_state(mem / READS_FILE)
    entry, files = _scope_files(state, scope)
    rec = files.get(key)
    if not _current(rec, st, _epoch(state, scope)):
        return None
    t = now.timestamp()
    at = _covered_at(start, end, _ranges(rec.get("ranges")), t - FRESH_SECONDS)
    if at is None:
        return None
    denied = rec.get("denied")
    if (isinstance(denied, list) and len(denied) == 3 and denied[:2] == [start, end]
            and isinstance(denied[2], (int, float)) and 0 <= t - denied[2] <= RETRY_SECONDS):
        return None  # asked again right after a denial: let it through (PostToolUse records it)
    rec["denied"] = [start, end, t]
    entry["seen"] = t
    _save(mem, state)
    return REASON.format(file=file_path, at=_clock(at))


def record(payload: dict, mem: Path, now: datetime) -> None:
    """PostToolUse: the Read succeeded, so its lines are now in the chat."""
    target = _target(payload)
    if target is None:
        return
    scope, _file_path, key, st, (start, end) = target
    t = now.timestamp()
    state = load_state(mem / READS_FILE)
    epoch = _epoch(state, scope)
    entry, files = _scope_files(state, scope)
    rec = files.get(key)
    old = _ranges(rec.get("ranges")) if _current(rec, st, epoch) else []
    ranges = [r for r in old if r[2] >= t - FRESH_SECONDS and r[:2] != [start, end]] + [[start, end, t]]
    files[key] = {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "ranges": ranges[-MAX_RANGES:],
                  "compact_epoch": epoch}
    if len(files) > MAX_PATHS:
        def last(k):
            rs = _ranges(files[k].get("ranges")) if isinstance(files[k], dict) else []
            return max((r[2] for r in rs), default=0)
        for stale in sorted(files, key=last)[:len(files) - MAX_PATHS]:
            del files[stale]
    entry["seen"] = t
    _prune(state)
    _save(mem, state)


def bump_compaction(payload: dict, mem: Path) -> None:
    """Compaction removed what was read from the context: reads in this chat are allowed again."""
    scope = scope_key(payload)
    if scope is None:
        return
    state = load_state(mem / READS_FILE)
    epochs = state.get("epochs") if isinstance(state.get("epochs"), dict) else {}
    epochs[scope] = _epoch(state, scope) + 1
    state["epochs"] = epochs
    _save(mem, state)
