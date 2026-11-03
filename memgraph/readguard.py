"""Read guard: stop Claude from re-reading a file it already read, unchanged, in this chat.

PreToolUse(Read) calls `check`. Each chat (session id, plus agent id inside a helper agent)
keeps, per normalized path, the file's mtime_ns and size when it was read, the line ranges
read and the compaction epoch at that time, in reads.json in the memory folder. A request is
denied only when the file is unchanged, the range was already read and no compaction has
happened since. A second identical request within 2 minutes of a denial is allowed, so
Claude can always get the file back. Anything odd (missing, huge, unreadable file, bad
input) is allowed. Env SESSION_MEMORY_READ_GUARD=0 turns it off.
"""
from __future__ import annotations

import ntpath
import os
import posixpath
from datetime import datetime
from pathlib import Path

from memgraph.graph import write_json_atomic
from memgraph.ingest import load_state

READS_FILE = "reads.json"
MAX_BYTES = 2 * 1024 * 1024          # bigger files: always allowed, never recorded
WHOLE_READ_MAX_BYTES = 100_000       # a whole-file Read of a bigger file may have failed as too large
DEFAULT_LINES = 2000                 # lines Claude Code's Read returns without a limit
RETRY_SECONDS = 120
MAX_SCOPES = 30
MAX_PATHS = 500
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


def _pos_int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _span(tool_input: dict) -> tuple[int, int, bool]:
    """(first line, line after the last, whole-file request?) of a Read request."""
    start = _pos_int(tool_input.get("offset")) or 1
    limit = _pos_int(tool_input.get("limit"))
    return start, start + (limit or DEFAULT_LINES), limit is None


def _covered(start: int, end: int, ranges) -> bool:
    for r in ranges if isinstance(ranges, list) else []:
        if isinstance(r, list) and len(r) == 2 and all(isinstance(x, int) for x in r) and r[0] <= start and end <= r[1]:
            return True
    return False


def _merge(ranges, start: int, end: int) -> list[list[int]]:
    spans = [r for r in (ranges if isinstance(ranges, list) else [])
             if isinstance(r, list) and len(r) == 2 and all(isinstance(x, int) for x in r)]
    spans.append([start, end])
    spans.sort()
    out: list[list[int]] = []
    for s, e in spans:
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _epoch(state: dict, scope: str) -> int:
    value = state.get("epochs", {}).get(scope, 0) if isinstance(state.get("epochs"), dict) else 0
    return value if isinstance(value, int) else 0


def _scopes(state: dict) -> dict:
    scopes = state.get("scopes")
    if not isinstance(scopes, dict):
        scopes = state["scopes"] = {}
    return scopes


def _prune(state: dict) -> None:
    scopes = _scopes(state)
    if len(scopes) > MAX_SCOPES:
        def seen(item):
            v = item[1].get("seen") if isinstance(item[1], dict) else None
            return v if isinstance(v, (int, float)) else 0
        keep = dict(sorted(scopes.items(), key=seen)[-MAX_SCOPES:])
        state["scopes"] = keep
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


def check(payload: dict, mem: Path, now: datetime) -> str | None:
    """The deny reason for this Read, or None to allow it (allowed reads are recorded)."""
    if not enabled():
        return None
    tool_input = payload.get("tool_input")
    scope = scope_key(payload)
    if not isinstance(tool_input, dict) or scope is None or "pages" in tool_input:
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    full = file_path if os.path.isabs(file_path) else os.path.join(str(payload.get("cwd") or ""), file_path)
    try:
        st = os.stat(full)
    except (OSError, ValueError):
        return None
    if not os.path.isfile(full) or st.st_size > MAX_BYTES:
        return None
    start, end, whole = _span(tool_input)
    if whole and st.st_size > WHOLE_READ_MAX_BYTES:
        return None  # it may have been refused as too large; never claim it was read
    key = norm_path(os.path.abspath(full))
    t = now.timestamp()

    state = load_state(mem / READS_FILE)
    epoch = _epoch(state, scope)
    entry_scope = _scopes(state).get(scope)
    if not isinstance(entry_scope, dict):
        entry_scope = {}
    files = entry_scope.get("files") if isinstance(entry_scope.get("files"), dict) else {}
    rec = files.get(key) if isinstance(files.get(key), dict) else None
    same = (rec is not None and rec.get("mtime_ns") == st.st_mtime_ns and rec.get("size") == st.st_size
            and rec.get("compact_epoch") == epoch)
    if same and _covered(start, end, rec.get("ranges")):
        denied = rec.get("denied")
        retry = (isinstance(denied, list) and len(denied) == 3 and denied[:2] == [start, end]
                 and isinstance(denied[2], (int, float)) and 0 <= t - denied[2] <= RETRY_SECONDS)
        if not retry:
            rec["denied"] = [start, end, t]
            entry_scope["seen"] = t
            _store(state, scope, entry_scope, files)
            _save(mem, state)
            at = rec.get("at")
            return REASON.format(file=file_path, at=_clock(at if isinstance(at, (int, float)) else t))
    ranges = _merge(rec.get("ranges") if same else [], start, end)
    files[key] = {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "ranges": ranges, "at": t, "compact_epoch": epoch}
    if len(files) > MAX_PATHS:
        for old in sorted(files, key=lambda k: files[k].get("at", 0) if isinstance(files[k], dict) else 0)[
                :len(files) - MAX_PATHS]:
            del files[old]
    entry_scope["seen"] = t
    _store(state, scope, entry_scope, files)
    _prune(state)
    _save(mem, state)
    return None


def _store(state: dict, scope: str, entry_scope: dict, files: dict) -> None:
    entry_scope["files"] = files
    _scopes(state)[scope] = entry_scope


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
