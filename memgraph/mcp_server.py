"""Minimal MCP server (stdio, newline-delimited JSON-RPC 2.0) exposing project memory."""
from __future__ import annotations

import base64
import json
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from memgraph.brief import MCP_FOOTER, build_brief
from memgraph.graph import Graph, GraphBusy, update
from memgraph.ingest import memory_dir
from memgraph.memory_ops import BUSY, REMEMBER_MAX_CHARS, TOO_LONG, recall_lines, remember
from memgraph.pack import SIZES, build_pack
from memgraph.visual import IMAGE_FILE, has_story, render_visual_pack

SERVER_VERSION = "0.2.0"
DEFAULT_PROTOCOL = "2024-11-05"

TOOLS = [
    {
        "name": "memory_brief",
        "description": "Short briefing of this project's memory: current task, recent decisions, files, open errors.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "memory_recall",
        "description": "Search project memory (tasks, files, commands, errors, decisions) for a term.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search term"}},
            "required": ["query"],
        },
    },
    {
        "name": "memory_remember",
        "description": "Save a decision or note to project memory.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "What to remember"}},
            "required": ["text"],
        },
    },
    {
        "name": "memory_pack",
        "description": "Resume pack: a compact summary to continue the previous session in a fresh chat. Optionally with a PNG overview.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "size": {"type": "string", "enum": list(SIZES), "default": "standard"},
                "visual": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "memory_usage",
        "description": "Exact token usage of this project from Claude Code's local transcripts (last N days), "
                       "plus measured savings from fresh starts and estimated missed savings.",
        "inputSchema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1, "default": 7}},
        },
    },
    {
        "name": "code_outline",
        "description": "Outline of one code file in this project: classes, functions and methods with their line "
                       "ranges and definition lines. Read only the lines you need afterwards.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path, project-relative or absolute"}},
            "required": ["path"],
        },
    },
    {
        "name": "code_find",
        "description": "Find classes, functions and methods by name across this project's code "
                       "(exact matches first, then prefix, then substring), with file and line range.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name or part of it"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            },
            "required": ["name"],
        },
    },
    {
        "name": "code_read",
        "description": "The source lines (with line numbers) of one class, function or method in a file, "
                       "instead of reading the whole file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, project-relative or absolute"},
                "symbol": {"type": "string", "description": "Symbol name, e.g. Class.method or function"},
            },
            "required": ["path", "symbol"],
        },
    },
]
CODE_TOOLS = frozenset({"code_outline", "code_find", "code_read"})


class _ParamError(Exception):
    pass


def _log(mem: Path | None) -> None:
    text = traceback.format_exc()
    targets = ([Path(mem) / "error.log"] if mem is not None else []) + [
        Path(tempfile.gettempdir()) / "session-memory-error.log"
    ]
    for target in targets:
        try:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now(timezone.utc).isoformat()}\n{text}\n")
            return
        except Exception:
            continue


def _text(s: str) -> dict:
    return {"type": "text", "text": s}


def _call_tool(name: str, args: dict, mem: Path | None, project: Path, mem_error: str = "") -> list[dict]:
    if name not in {t["name"] for t in TOOLS}:
        raise _ParamError(f"Unknown tool: {name}")
    if mem is None:
        raise RuntimeError(mem_error or "Memory folder unavailable")
    if name in CODE_TOOLS:
        return [_text(_code_text(name, args, mem, project))]
    graph_path = mem / "graph.json"
    graph = Graph.load(graph_path)  # re-read every call: the Stop hook keeps updating it
    if name == "memory_brief":
        return [_text(build_brief(graph, project_root=project, mem_dir=mem, footer=MCP_FOOTER) or "No project memory yet.")]
    if name == "memory_recall":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        query = query.strip()
        lines = recall_lines(graph, query)
        return [_text("\n".join(lines) if lines else f'Nothing found for "{query}".')]
    if name == "memory_usage":
        return [_text(_usage_text(args, mem, project))]
    if name == "memory_remember":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        text = text.strip()
        if len(text) > REMEMBER_MAX_CHARS:
            raise ValueError(TOO_LONG.format(n=len(text)))
        try:
            update(graph_path, lambda g: remember(g, text, datetime.now(timezone.utc)))
        except GraphBusy:
            raise RuntimeError(BUSY) from None
        return [_text("Saved to project memory.")]
    size = args.get("size", "standard")
    visual = args.get("visual", False)
    if size not in SIZES:
        raise ValueError(f"size must be one of {', '.join(SIZES)}")
    if not isinstance(visual, bool):
        raise ValueError("visual must be a boolean")
    content = [_text(build_pack(graph, size, project) or "No project memory yet.")]
    if visual and has_story(graph):
        png = render_visual_pack(graph, mem / IMAGE_FILE, project)
        if png:
            data = base64.b64encode(Path(png).read_bytes()).decode("ascii")
            content.append({"type": "image", "data": data, "mimeType": "image/png"})
        else:
            content.append(_text("Visual pack unavailable: install Pillow"))
    return content


def _code_text(name: str, args: dict, mem: Path, project: Path) -> str:
    from memgraph import codeindex

    if name == "code_outline":
        return codeindex.outline(project, mem, args.get("path"))
    if name == "code_find":
        return codeindex.find(project, mem, args.get("name"), args.get("limit", 20))
    return codeindex.read_symbol(project, mem, args.get("path"), args.get("symbol"))


def _usage_text(args: dict, mem: Path, project: Path) -> str:
    from datetime import timedelta

    from memgraph.savings import measure_savings, savings_totals_line
    from memgraph.usage import collect_usage, totals, totals_line, transcripts_dir_for
    from memgraph.watch import ttl_minutes

    days = args.get("days", 7)
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise ValueError("days must be a positive integer")
    tdir = transcripts_dir_for(project, mem)
    if not tdir.is_dir():
        return "No Claude Code transcripts found for this project."
    result = collect_usage(tdir)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    recent = [r for r in result.replies if r.ts >= since]
    sav = measure_savings(tdir, mem, ttl_minutes(), result)
    return (f"Token usage, last {days} days (exact, from transcripts):\n"
            + totals_line(totals(recent), result.duplicates) + "\n" + savings_totals_line(sav))


def _handle(msg, mem: Path | None, project: Path, mem_error: str = ""):
    """Return a response dict, or None when no reply is due."""
    if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
        if isinstance(msg, dict) and "id" in msg and "method" not in msg:
            return None  # a response from the client: ignore
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in msg:
        return None  # notification
    rid, method = msg["id"], msg["method"]
    params = msg.get("params")
    params = params if isinstance(params, dict) else {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    if method == "initialize":
        version = params.get("protocolVersion")
        return ok({
            "protocolVersion": version if isinstance(version, str) else DEFAULT_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "session-memory", "version": SERVER_VERSION},
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(args, dict):
            return err(-32602, "Invalid params")
        try:
            return ok({"content": _call_tool(name, args, mem, project, mem_error), "isError": False})
        except _ParamError as e:
            return err(-32602, str(e))
        except Exception as e:
            _log(mem)
            return ok({"content": [_text(f"Error: {e}")], "isError": True})
    return err(-32601, f"Method not found: {method}")


def serve(stdin: BinaryIO, stdout: BinaryIO, project: Path) -> None:
    project = Path(project)
    mem: Path | None = None
    mem_error = ""
    try:
        mem = memory_dir(project)
    except Exception as e:
        _log(None)
        mem_error = f"Memory folder unavailable: {e}"

    def send(obj) -> None:
        try:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:  # lone surrogates: escape them instead of failing
            data = json.dumps(obj, ensure_ascii=True).encode("ascii")
        stdout.write(data + b"\n")
        stdout.flush()

    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        msg = None
        try:
            try:
                msg = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            resp = _handle(msg, mem, project, mem_error)
        except Exception:
            _log(mem)
            rid = msg.get("id") if isinstance(msg, dict) else None
            resp = None if isinstance(msg, dict) and "id" not in msg else {
                "jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "Internal error"}}
        if resp is not None:
            try:
                send(resp)
            except Exception:
                _log(mem)
