"""Parse Claude Code transcript JSONL lines into small, typed events."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

_MAX_TEXT = 200
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_COMMAND_TOOLS = {"Bash", "PowerShell"}


@dataclass(frozen=True)
class Event:
    kind: str  # prompt | edit | read | command | error | todo | usage
    ts: str
    session: str
    data: dict


def _text_of(content) -> str:
    """Plain text of message content: a string, or the joined text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def _blocks(content) -> list[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _todo_items(raw) -> list[dict]:
    return [
        {"content": t.get("content"), "status": t.get("status")}
        for t in raw
        if isinstance(t, dict)
    ]


def _tool_use_event(block: dict, ts: str, session: str) -> Event | None:
    name = block.get("name")
    inp = block.get("input")
    if not isinstance(inp, dict):
        inp = {}
    if name in _EDIT_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path")
        return Event("edit", ts, session, {"path": path, "id": block.get("id")})
    if name == "Read":
        return Event("read", ts, session, {"path": inp.get("file_path"), "id": block.get("id")})
    if name in _COMMAND_TOOLS:
        cmd = str(inp.get("command") or "")[:_MAX_TEXT]
        return Event("command", ts, session, {"cmd": cmd, "id": block.get("id")})
    if name == "TodoWrite" and isinstance(inp.get("todos"), list):
        return Event("todo", ts, session, {"items": _todo_items(inp["todos"])})
    if name == "mcp__ccd_turn__update_plan" and isinstance(inp.get("items"), list):
        return Event("todo", ts, session, {"items": _todo_items(inp["items"])})
    return None


def _parse_user(msg: dict, obj: dict, ts: str, session: str) -> list[Event]:
    events: list[Event] = []
    content = msg.get("content")
    if obj.get("isMeta") is not True:
        text = _text_of(content)
        if text and not text.startswith("<"):
            events.append(Event("prompt", ts, session, {"text": text[:_MAX_TEXT]}))
    for block in _blocks(content):
        if block.get("type") == "tool_result" and block.get("is_error") is True:
            events.append(
                Event(
                    "error",
                    ts,
                    session,
                    {"text": _text_of(block.get("content"))[:_MAX_TEXT], "tool_use_id": block.get("tool_use_id")},
                )
            )
    return events


def _parse_assistant(msg: dict, ts: str, session: str) -> list[Event]:
    events: list[Event] = []
    for block in _blocks(msg.get("content")):
        if block.get("type") == "tool_use":
            event = _tool_use_event(block, ts, session)
            if event is not None:
                events.append(event)
    usage = msg.get("usage")
    if isinstance(usage, dict):
        context = sum(
            usage.get(k) or 0
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        events.append(Event("usage", ts, session, {"context": context}))
    return events


def parse_line(line: str) -> list[Event]:
    """Events for one transcript line; invalid JSON or unknown types give []."""
    try:
        obj = json.loads(line)
    except ValueError:
        return []
    if not isinstance(obj, dict):
        return []
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return []
    ts = obj.get("timestamp")
    session = obj.get("sessionId")
    kind = obj.get("type")
    if kind == "user":
        return _parse_user(msg, obj, ts, session)
    if kind == "assistant":
        return _parse_assistant(msg, ts, session)
    return []


def parse_lines(lines: Iterable[str]) -> list[Event]:
    events: list[Event] = []
    for line in lines:
        events.extend(parse_line(line))
    return events
