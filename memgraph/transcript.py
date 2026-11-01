"""Parse Claude Code transcript JSONL lines into small, typed events."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

_MAX_TEXT = 200
_MAX_REPLY = 600
_INTERRUPTED = "[Request interrupted by user"
_TITLE_FIELDS = {"custom-title": ("customTitle", "custom"), "ai-title": ("aiTitle", "ai")}
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_COMMAND_TOOLS = {"Bash", "PowerShell"}
_PLAN_TOOL = "mcp__ccd_turn__update_plan"
_WRAP_UP_TOOL = "mcp__ccd_turn__wrap_up"


def parse_ts(value) -> datetime | None:
    """Aware UTC datetime from an ISO string (naive means UTC); None if unparseable."""
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Event:
    kind: str  # prompt | edit | read | command | error | todo | todo_delta | usage | reply | title
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
    if name == _PLAN_TOOL and isinstance(inp.get("items"), list):
        return Event("todo", ts, session, {"items": _todo_items(inp["items"])})
    if name == _PLAN_TOOL:
        return Event("todo_delta", ts, session, {
            "advance": inp.get("advance") is True,
            "done": [n for n in inp.get("done") or [] if _is_int(n)] if isinstance(inp.get("done"), list) else [],
            "start": inp.get("start") if _is_int(inp.get("start")) else None,
            "complete_all": False,
        })
    if name == _WRAP_UP_TOOL:  # closes the turn: every step of the plan is done
        return Event("todo_delta", ts, session,
                     {"advance": False, "done": [], "start": None, "complete_all": True})
    return None


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_user(msg: dict, obj: dict, ts: str, session: str) -> list[Event]:
    events: list[Event] = []
    content = msg.get("content")
    if obj.get("isMeta") is not True and obj.get("isCompactSummary") is not True:
        text = _text_of(content)
        if text and not text.startswith("<") and not text.startswith(_INTERRUPTED):
            events.append(Event("prompt", ts, session, {"text": text[:_MAX_TEXT]}))
    for block in _blocks(content):
        if block.get("type") == "tool_result" and block.get("is_error") is True:
            text = _text_of(block.get("content"))
            if _is_rejection(text):
                continue
            events.append(
                Event("error", ts, session, {"text": text[:_MAX_TEXT], "tool_use_id": block.get("tool_use_id")})
            )
    return events


def _is_rejection(text: str) -> bool:
    """A tool call the user declined: not a real failure worth remembering."""
    return text.startswith("The user doesn't want to proceed") or "STOP what you are doing" in text


_CONTEXT_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def is_synthetic(obj: dict) -> bool:
    """True for assistant lines Claude Code writes itself, not model replies.

    "No response requested.", "API Error: ..." and usage-limit notices carry
    model "<synthetic>", isApiErrorMessage, and/or an all-zero input usage.
    They must not count as activity or as the chat's context size.
    """
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return False
    if msg.get("model") == "<synthetic>" or obj.get("isApiErrorMessage") is True:
        return True
    usage = msg.get("usage")
    return isinstance(usage, dict) and sum(usage.get(k) or 0 for k in _CONTEXT_FIELDS) == 0


def _parse_assistant(msg: dict, obj: dict, ts: str, session: str) -> list[Event]:
    events: list[Event] = []
    reply = ""  # the line's last text block, or a wrap_up's recap + ask, whichever comes last
    for block in _blocks(msg.get("content")):
        if block.get("type") == "tool_use":
            event = _tool_use_event(block, ts, session)
            if event is not None:
                events.append(event)
            if block.get("name") == _WRAP_UP_TOOL and isinstance(block.get("input"), dict):
                inp = block["input"]
                reply = " ".join(str(inp.get(k) or "").strip() for k in ("recap", "ask")).strip() or reply
        elif block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
            reply = block["text"].strip()
    synthetic = is_synthetic(obj)
    if reply and not synthetic:
        events.append(Event("reply", ts, session, {"text": reply[:_MAX_REPLY]}))
    usage = msg.get("usage")
    if isinstance(usage, dict) and not synthetic:
        context = sum(usage.get(k) or 0 for k in _CONTEXT_FIELDS)
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
    if obj.get("type") in _TITLE_FIELDS:
        field, source = _TITLE_FIELDS[obj["type"]]
        title = obj.get(field)
        if isinstance(title, str) and title.strip():
            return [Event("title", obj.get("timestamp"), obj.get("sessionId"),
                          {"text": title.strip()[:_MAX_TEXT], "source": source})]
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
        return _parse_assistant(msg, obj, ts, session)
    return []


def parse_lines(lines: Iterable[str]) -> list[Event]:
    events: list[Event] = []
    for line in lines:
        events.extend(parse_line(line))
    return events
