"""Cache guard: warn once before a message that would re-read a large, cold chat."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memgraph.transcript import is_synthetic, parse_line, parse_ts

_TAIL_BYTES = 256 * 1024

_REASON = (
    "Cache guard: this chat has been idle for {idle} minutes, so its prompt cache has likely expired. "
    "Sending now re-reads about {context:,} tokens at full price.\n"
    "Type /clear to start fresh with your memory brief. To open the memory map without Claude, run: {map_cmd}\n"
    "To send anyway, send the same message again within 10 minutes."
)


# Claude Code built-in commands that never call the model: they pass the guard.
# Everything else starting with "/" (plugin commands, skills, custom commands, and
# built-ins that run the model such as /init or /review) is guarded like a normal prompt.
FREE_COMMANDS = frozenset(
    "clear compact help exit quit model config settings cost usage status context doctor login logout "
    "mcp hooks permissions plugin plugins resume rewind theme vim fast effort output-style terminal-setup "
    "add-dir agents memory export release-notes upgrade ide todos bug feedback privacy-settings".split()
)


def map_command() -> str:
    """Command line that opens the memory map without Claude (the launcher in this repo)."""
    name = "run-python.cmd" if os.name == "nt" else "run-python"
    path = str(Path(__file__).resolve().parent.parent / "bin" / name)
    return f'"{path}" map' if " " in path else f"{path} map"


def _is_free_command(prompt: str) -> bool:
    text = prompt.lstrip()
    if not text.startswith("/"):
        return False
    word = text[1:].split(None, 1)[0].lower() if text[1:].strip() else ""
    return word in FREE_COMMANDS


@dataclass
class GuardConfig:
    idle_minutes: int = 55
    min_tokens: int = 30000
    repeat_window_minutes: int = 10

    @classmethod
    def from_env(cls) -> "GuardConfig":
        defaults = cls()

        def read(name: str, default: int) -> int:
            try:
                value = int(os.environ[name])
            except (KeyError, ValueError):
                return default
            return value if value >= 0 else default

        return cls(
            idle_minutes=read("SESSION_MEMORY_IDLE_MIN", defaults.idle_minutes),
            min_tokens=read("SESSION_MEMORY_MIN_TOKENS", defaults.min_tokens),
        )


def _tail_lines(path: Path) -> list[str]:
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        start = max(0, size - _TAIL_BYTES)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").split("\n")  # not splitlines(): that also splits on U+2028 etc. inside JSON strings
    if start > 0 and lines:
        lines = lines[1:]  # first line of the chunk is probably cut off
    return lines


def last_activity(transcript_path: Path) -> tuple[datetime | None, int | None]:
    """(UTC time of the newest activity, newest usage context) from the file's last 256 KB.

    Activity is a model reply (assistant line) or a compaction (system line with
    subtype "compact_boundary", which also resets the context to None because the
    old history is gone). Synthetic assistant lines (usage-limit notices, API
    errors, "No response requested.") are ignored for both.
    """
    try:
        lines = _tail_lines(Path(transcript_path))
    except Exception:
        return None, None
    newest: datetime | None = None
    context: int | None = None
    for line in lines:
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind == "system" and obj.get("subtype") == "compact_boundary":
                ts, compacted, line_context = parse_ts(obj.get("timestamp")), True, None
            elif kind == "assistant" and not is_synthetic(obj):
                ts, compacted, line_context = parse_ts(obj.get("timestamp")), False, None
                for ev in parse_line(line):
                    if ev.kind == "usage" and isinstance(ev.data.get("context"), int):
                        line_context = ev.data["context"]
            else:
                continue
        except Exception:
            continue
        if ts is not None and (newest is None or ts > newest):
            newest = ts
        if compacted:
            context = None
        elif line_context is not None:
            context = line_context
    return newest, context


def has_reply(transcript_path: Path) -> bool:
    """True when the tail of the transcript holds a real (non-synthetic) assistant reply."""
    try:
        lines = _tail_lines(Path(transcript_path))
    except Exception:
        return False
    for line in lines:
        if '"assistant"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "assistant" and not is_synthetic(obj):
            return True
    return False


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def decide(
    now: datetime,
    last_ts: datetime | None,
    context: int | None,
    prompt: str,
    state: dict,
    cfg: GuardConfig,
) -> str | None:
    """The block reason, or None to allow. Records/clears state["guard"]."""
    if last_ts is None or context is None:
        return None
    if _is_free_command(prompt) or prompt.lstrip().startswith("<"):
        return None  # built-in commands that skip the model; task notifications, agent reports, cross-session messages
    now, last_ts = _utc(now), _utc(last_ts)
    idle = now - last_ts
    if idle <= timedelta(minutes=cfg.idle_minutes) or context <= cfg.min_tokens:
        return None

    digest = hashlib.sha1(prompt.strip().encode("utf-8")).hexdigest()
    prev = state.get("guard")
    if isinstance(prev, dict) and prev.get("hash") == digest:
        at = parse_ts(prev.get("at"))
        if at is not None and now - at <= timedelta(minutes=cfg.repeat_window_minutes):
            del state["guard"]
            return None

    state["guard"] = {"hash": digest, "at": now.isoformat()}
    return _REASON.format(idle=int(idle.total_seconds() // 60), context=context, map_cmd=map_command())
