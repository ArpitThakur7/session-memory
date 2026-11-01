"""Cache guard: warn once before a message that would re-read a large, cold chat."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memgraph.transcript import parse_line

_TAIL_BYTES = 256 * 1024

_REASON = (
    "Cache guard: this chat has been idle for {idle} minutes, so its prompt cache has likely expired. "
    "Sending now re-reads about {context:,} tokens at full price.\n"
    "Cheaper: type /clear. Your project memory loads automatically as a short brief.\n"
    "To send anyway, send the same message again within 10 minutes."
)


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


def _parse_ts(value) -> datetime | None:
    """Aware UTC datetime from an ISO string (naive means UTC); None if unparseable."""
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _tail_lines(path: Path) -> list[str]:
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        start = max(0, size - _TAIL_BYTES)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # first line of the chunk is probably cut off
    return lines


def last_activity(transcript_path: Path) -> tuple[datetime | None, int | None]:
    """(UTC time of the newest assistant line, newest usage context) from the file's last 256 KB."""
    try:
        lines = _tail_lines(Path(transcript_path))
    except Exception:
        return None, None
    newest: datetime | None = None
    context: int | None = None
    for line in lines:
        try:
            events = parse_line(line)
            obj = json.loads(line)
            ts = _parse_ts(obj.get("timestamp")) if obj.get("type") == "assistant" else None
        except Exception:
            continue
        if ts is not None and (newest is None or ts > newest):
            newest = ts
        for ev in events:
            if ev.kind == "usage" and isinstance(ev.data.get("context"), int):
                context = ev.data["context"]
    return newest, context


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
    if last_ts is None or context is None or prompt.startswith("/"):
        return None
    now, last_ts = _utc(now), _utc(last_ts)
    idle = now - last_ts
    if idle <= timedelta(minutes=cfg.idle_minutes) or context <= cfg.min_tokens:
        return None

    digest = hashlib.sha1(prompt.strip().encode("utf-8")).hexdigest()
    prev = state.get("guard")
    if isinstance(prev, dict) and prev.get("hash") == digest:
        at = _parse_ts(prev.get("at"))
        if at is not None and now - at <= timedelta(minutes=cfg.repeat_window_minutes):
            del state["guard"]
            return None

    state["guard"] = {"hash": digest, "at": now.isoformat()}
    return _REASON.format(idle=int(idle.total_seconds() // 60), context=context)
