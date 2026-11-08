"""Parse Claude Code transcript JSONL lines into small, typed events."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from memgraph.redact import redact

_MAX_TEXT = 200
_MAX_REPLY = 600
# Secrets are hidden in this much of a text before it is cut to its stored length, so a
# secret that straddles the cut is never kept in part. Longer texts are cut first (speed).
_REDACT_SPAN = 4000
_INTERRUPTED = "[Request interrupted by user"
_HARNESS_ERROR = "<tool_use_error>"
_TITLE_FIELDS = {"custom-title": ("customTitle", "custom"), "ai-title": ("aiTitle", "ai")}
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_COMMAND_TOOLS = {"Bash", "PowerShell"}
_PLAN_TOOL = "mcp__ccd_turn__update_plan"
_WRAP_UP_TOOL = "mcp__ccd_turn__wrap_up"
# A helper agent's (sidechain) line only contributes its work, never prompts, replies,
# todos, usage or titles: its "prompt" is the dispatch text, not a user request.
SIDECHAIN_KINDS = frozenset({"edit", "read", "command", "error"})


def parse_ts(value) -> datetime | None:
    """Aware UTC datetime from an ISO string (naive means UTC); None if unparseable."""
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


_LOCAL_TZ = None  # zone for displayed times; None means the machine's own zone


def local_time(value) -> datetime | None:
    """An ISO timestamp (or datetime; naive means UTC) in the zone used for display.

    Stored timestamps stay UTC; every time shown to a person goes through here."""
    ts = value if isinstance(value, datetime) else parse_ts(value)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(_LOCAL_TZ)


def fmt_local(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """`local_time(value)` formatted with `fmt`; "" when it does not parse."""
    ts = local_time(value)
    return ts.strftime(fmt) if ts is not None else ""


@dataclass(frozen=True)
class Event:
    """One thing that happened in a chat, as read from a transcript line."""
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


_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.S)
_PASTED = re.compile(r"<pasted_content\b[^>]*>(.*?)(?:</pasted_content>|\Z)", re.S)
# Wrapper tags: a lowercase name with "_" or "-" (pasted_content, system-reminder, ...),
# or any lowercase tag with attributes. Plain text like List<String> or a < b is kept.
_TAG = re.compile(r"</?[a-z][a-z0-9]*[_-][a-z0-9_-]*(?:\s[^<>]*)?/?>|<[a-z][a-z0-9_-]*\s[^<>]*=[^<>]*>")
_PASTED_PREVIEW = 100


def clean_prompt(text) -> str:
    """Prompt text without markup: system reminders and wrapper tags removed, a prompt
    that is only pasted content shown as "[pasted] " + the start of the paste, and
    pasted content inside other text as "[pasted]". Cut-off markup is handled too."""
    text = _SYSTEM_REMINDER.sub("", str(text or ""))
    pasted = list(_PASTED.finditer(text))
    if pasted and not _PASTED.sub("", text).strip():
        inner = " ".join(_TAG.sub("", " ".join(m.group(1) for m in pasted)).split())
        return f"[pasted] {inner[:_PASTED_PREVIEW]}".rstrip()
    text = _PASTED.sub("[pasted]", text)
    return _TAG.sub("", text).strip()


def _blocks(content) -> list[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _safe(text, limit: int | None = None) -> str:
    """Text as it may be stored: secrets hidden (memgraph.redact), then cut to `limit`."""
    text = redact(str(text or "")[:_REDACT_SPAN])
    return text if limit is None else text[:limit]


def _safe_path(path):
    """A file path with secrets hidden; None stays None."""
    return _safe(path) if isinstance(path, str) else path


def _todo_items(raw) -> list[dict]:
    return [
        {"content": _safe(t.get("content")) if isinstance(t.get("content"), str) else t.get("content"),
         "status": t.get("status")}
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
        return Event("edit", ts, session, {"path": _safe_path(path), "id": block.get("id")})
    if name == "Read":
        return Event("read", ts, session, {"path": _safe_path(inp.get("file_path")), "id": block.get("id")})
    if name in _COMMAND_TOOLS:
        cmd = _safe(inp.get("command"), _MAX_TEXT)
        return Event("command", ts, session, {"cmd": cmd, "id": block.get("id")})
    if name == "TodoWrite" and isinstance(inp.get("todos"), list):
        return Event("todo", ts, session, {"items": _todo_items(inp["todos"])})
    if name == _PLAN_TOOL and isinstance(inp.get("items"), list):
        return Event("todo", ts, session, {"items": _todo_items(inp["items"])})
    if name == _PLAN_TOOL:
        return Event("todo_delta", ts, session, _plan_delta(inp))
    if name == _WRAP_UP_TOOL:  # closes the turn: every step of the plan is done
        return Event("todo_delta", ts, session,
                     {"advance": False, "done": [], "start": None, "complete_all": True})
    return None


def _plan_delta(inp: dict) -> dict:
    """A plan update that changes step states (advance / done / start) instead of listing the steps."""
    done = inp.get("done") if isinstance(inp.get("done"), list) else []
    return {
        "advance": inp.get("advance") is True,
        "done": [n for n in done if _is_int(n)],
        "start": inp.get("start") if _is_int(inp.get("start")) else None,
        "complete_all": False,
    }


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_user(msg: dict, obj: dict, ts: str, session: str) -> list[Event]:
    events: list[Event] = []
    content = msg.get("content")
    if obj.get("isMeta") is not True and obj.get("isCompactSummary") is not True:
        raw = _SYSTEM_REMINDER.sub("", _text_of(content)).strip()
        # Other leading markup (<command-name>, <local-command-stdout>, ...) is not a request.
        if raw and (not raw.startswith("<") or raw.startswith("<pasted_content")) \
                and not raw.startswith(_INTERRUPTED):
            text = clean_prompt(raw)
            if text:
                events.append(Event("prompt", ts, session, {"text": _safe(text, _MAX_TEXT)}))
    for block in _blocks(content):
        if block.get("type") == "tool_result" and block.get("is_error") is True:
            text = _text_of(block.get("content"))
            if _is_rejection(text) or text.lstrip().startswith(_HARNESS_ERROR):
                continue  # declined by the user, or Claude misusing a tool: not project work
            events.append(
                Event("error", ts, session, {"text": _safe(text, _MAX_TEXT), "tool_use_id": block.get("tool_use_id")})
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
        events.append(Event("reply", ts, session, {"text": _safe(reply, _MAX_REPLY)}))
    usage = msg.get("usage")
    if isinstance(usage, dict) and not synthetic:
        context = sum(usage.get(k) or 0 for k in _CONTEXT_FIELDS)
        events.append(Event("usage", ts, session, {"context": context}))
    return events


def parse_line(line: str) -> list[Event]:
    """Events for one transcript line; invalid JSON or unknown types give [].

    A sidechain (helper agent) line yields only SIDECHAIN_KINDS events."""
    try:
        obj = json.loads(line)
    except ValueError:
        return []
    if not isinstance(obj, dict):
        return []
    if obj.get("isSidechain") is True:
        return [e for e in _parse_obj(obj) if e.kind in SIDECHAIN_KINDS]
    return _parse_obj(obj)


def _parse_obj(obj: dict) -> list[Event]:
    if obj.get("type") in _TITLE_FIELDS:
        field, source = _TITLE_FIELDS[obj["type"]]
        title = obj.get(field)
        if isinstance(title, str) and title.strip():
            return [Event("title", obj.get("timestamp"), obj.get("sessionId"),
                          {"text": _safe(title.strip(), _MAX_TEXT), "source": source})]
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
