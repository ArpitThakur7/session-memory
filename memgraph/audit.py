"""`audit`: what every new chat costs before you type, and what could be switched off.

Two kinds of number, never mixed:
- measured: the first real reply of each main chat, from the transcripts (input + cache writes + cache read);
- estimate: the files and tools Claude Code loads into every chat (CLAUDE.md at characters / 4, plugins'
  "Projected token cost" from `claude plugin details`, MCP servers and skills as counts).

Everything about `claude` is best-effort: a missing or slow CLI marks rows "not checked", it never fails the command.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO

from memgraph.savings import read_events
from memgraph.transcript import parse_ts
from memgraph.usage import collect_usage, transcripts_dir_for

MAX_PLUGINS = 20
MIN_SUGGEST_TOKENS = 100
WEEK_DAYS = 7
NOT_CHECKED = "not checked"
USAGE = "Usage: audit [--project PATH] [--json] [--transcripts PATH]\n"


# ------------------------------------------------------------ measured

def start_cost(replies) -> dict:
    """Context of the first real reply of each main chat: median, min, max and how many chats."""
    first: dict[str, int] = {}
    for r in replies:  # oldest first
        if not r.helper and r.session and r.session not in first and r.context > 0:
            first[r.session] = r.context
    values = sorted(first.values())
    if not values:
        return {"sessions": 0, "median": None, "min": None, "max": None}
    return {"sessions": len(values), "median": int(statistics.median(values)), "min": values[0], "max": values[-1]}


# ------------------------------------------------------------ CLAUDE.md files

def _config_dir(home: Path) -> Path:
    given = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(given).expanduser() if given else home / ".claude"


def claude_md_files(project: Path, home: Path) -> list[dict]:
    """Every CLAUDE.md Claude Code loads for `project`, with estimated tokens (characters // 4).

    The user file, then CLAUDE.md, .claude/CLAUDE.md and CLAUDE.local.md in the project and each parent
    folder up to the drive root."""
    candidates = [_config_dir(home) / "CLAUDE.md"]
    folder = Path(project).resolve()
    while True:
        candidates += [folder / "CLAUDE.md", folder / ".claude" / "CLAUDE.md", folder / "CLAUDE.local.md"]
        if folder.parent == folder:
            break
        folder = folder.parent
    out, seen = [], set()
    for path in candidates:
        try:
            if not path.is_file():
                continue
            key = os.path.normcase(str(path.resolve()))
            if key in seen:
                continue
            seen.add(key)
            chars = len(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        out.append({"path": str(path), "chars": chars, "tokens": chars // 4})
    return out


# ------------------------------------------------------------ the claude CLI

def _ask(project: Path, args: list[str]):
    """(returncode, stdout) of `claude <args>` (15 s limit), or None when it could not be asked."""
    from memgraph.doctor import _run_claude

    try:
        exe = shutil.which("claude")
    except Exception:
        exe = None
    return _run_claude({"claude": exe, "project": Path(project)}, args)


def _ok(res) -> str | None:
    return res[1] if res is not None and res[0] == 0 else None


_MCP_LINE = re.compile(r"^(?P<name>\S.*?):\s+(?P<rest>\S.*)$")
_TOOLS = re.compile(r"(\d+)\s+tools?\b", re.I)


def parse_mcp_list(text: str) -> list[dict]:
    """Servers from `claude mcp list` text: name, status words, and the tool count only when shown."""
    out = []
    for raw in text.splitlines():
        m = _MCP_LINE.match(raw.strip())
        if not m:
            continue
        tools = _TOOLS.search(m["rest"])
        out.append({"name": m["name"], "tools": int(tools.group(1)) if tools else None,
                    "line": m["rest"][:120]})
    return out


_PLUGIN_ID = re.compile(r"^\W*\s*([A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+)\s*$")


def parse_plugin_list(text: str) -> list[str]:
    """Enabled plugin ids ('name@marketplace') from `claude plugin list` text."""
    ids, current, enabled = [], None, True
    for raw in text.splitlines():
        m = _PLUGIN_ID.match(raw)
        if m:
            if current and enabled:
                ids.append(current)
            current, enabled = m.group(1), True
        elif current and raw.strip().lower().startswith("status:") and "disabled" in raw.lower():
            enabled = False
    if current and enabled:
        ids.append(current)
    return ids


_ALWAYS_ON = re.compile(r"Always-on:\s*~?\s*([\d.,]+)\s*(k?)\s*tok", re.I)
_SKILLS = re.compile(r"^\s*Skills\s*\((\d+)\)", re.M)


def _number(digits: str, kilo: str) -> int | None:
    try:
        value = float(digits.replace(",", ""))
    except ValueError:
        return None
    return int(round(value * (1000 if kilo else 1)))


def parse_plugin_details(text: str) -> dict:
    """tokens: the 'Projected token cost' always-on figure (None when absent); skills: count shown."""
    section = text.split("Projected token cost", 1)
    m = _ALWAYS_ON.search(section[1]) if len(section) > 1 else None
    skills = _SKILLS.search(text)
    return {"tokens": _number(m.group(1), m.group(2)) if m else None,
            "skills": int(skills.group(1)) if skills else None}


def _plugin_row(project: Path, pid: str) -> dict:
    text = _ok(_ask(project, ["plugin", "details", pid]))
    info = parse_plugin_details(text) if text is not None else {"tokens": None, "skills": None}
    return {"name": pid, **info}


def check_claude(project: Path) -> dict:
    """MCP servers and plugins from the CLI; every part is None / empty with a note when unavailable."""
    mcp_text = _ok(_ask(project, ["mcp", "list"]))
    list_text = _ok(_ask(project, ["plugin", "list"]))
    res = {"mcp": None if mcp_text is None else parse_mcp_list(mcp_text), "plugins": None, "plugins_skipped": 0}
    if list_text is not None:
        ids = parse_plugin_list(list_text)
        res["plugins_skipped"] = max(0, len(ids) - MAX_PLUGINS)
        with ThreadPoolExecutor(max_workers=4) as pool:
            res["plugins"] = list(pool.map(lambda p: _plugin_row(project, p), ids[:MAX_PLUGINS]))
    return res


def local_skills(project: Path, home: Path) -> int:
    """Skill folders (with a SKILL.md) the user and the project define themselves."""
    total = 0
    for base in (_config_dir(home) / "skills", Path(project) / ".claude" / "skills"):
        try:
            total += sum(1 for p in base.iterdir() if (p / "SKILL.md").is_file())
        except OSError:
            continue
    return total


# ------------------------------------------------------------ events

def week_events(mem_dir: Path, now: datetime) -> dict:
    """Counts of the plugin's own events in the last 7 days (read from events.jsonl).

    Denials and steers that were read anyway (`read_retry`) are subtracted: they saved nothing."""
    since = now - timedelta(days=WEEK_DAYS)
    res = {"read_denied": 0, "big_read_steers": 0, "steer_outline_chars": 0, "steer_file_lines": 0,
           "related_memory": 0, "related_memory_chars": 0, "read_retries": 0, "steer_retries": 0}
    for ev in read_events(mem_dir):
        ts = parse_ts(ev.get("t")) if isinstance(ev.get("t"), str) else None
        if ts is None or ts < since:
            continue
        kind = ev.get("type")

        def num(key):
            v = ev.get(key)
            return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0

        if kind == "read_denied":
            res["read_denied"] += 1
        elif kind == "big_read_steer":
            res["big_read_steers"] += 1
            res["steer_outline_chars"] += num("outline_chars")
            res["steer_file_lines"] += num("lines")
        elif kind == "jit_context":
            res["related_memory"] += 1
            res["related_memory_chars"] += num("chars")
        elif kind == "read_retry" and ev.get("kind") == "guard":
            res["read_retries"] += 1
        elif kind == "read_retry" and ev.get("kind") == "steer":
            res["steer_retries"] += 1
            res["steer_file_lines"] -= num("lines")
    res["read_denied"] = max(0, res["read_denied"] - res["read_retries"])
    res["big_read_steers"] = max(0, res["big_read_steers"] - res["steer_retries"])
    res["steer_file_lines"] = max(0, res["steer_file_lines"])
    return res


# ------------------------------------------------------------ suggestions

def suggestions(files: list[dict], claude: dict, limit: int = 3) -> list[dict]:
    """The biggest fixed costs that can be reduced, largest first."""
    items = []
    for f in files:
        if f["tokens"] >= MIN_SUGGEST_TOKENS:
            items.append({"item": f["path"], "tokens": f["tokens"],
                          "text": f"Shorten {f['path']} (about {f['tokens']:,} tokens in every chat): "
                                  "move rarely needed parts into files Claude reads only when asked"})
    for p in claude.get("plugins") or []:
        if p["tokens"] is not None and p["tokens"] >= MIN_SUGGEST_TOKENS:
            items.append({"item": p["name"], "tokens": p["tokens"],
                          "text": f"Turn off plugin {p['name']} (about {p['tokens']:,} tokens in every chat): "
                                  f"claude plugin disable {p['name']}"})
    items.sort(key=lambda i: (-i["tokens"], i["item"]))
    return items[:limit]


# ------------------------------------------------------------ the report

def run_audit(project: Path, transcripts: Path | None = None, home: Path | None = None,
              now: datetime | None = None) -> dict:
    from datetime import timezone

    from memgraph.store import memory_path

    project = Path(project).resolve()
    home = Path(home) if home else Path.home()
    now = now or datetime.now(timezone.utc)
    mem = memory_path(project)  # read-only: an audit of another folder must not create memory there
    tdir = transcripts if transcripts is not None else transcripts_dir_for(project, mem)
    usage = collect_usage(tdir) if Path(tdir).is_dir() else None
    start = start_cost(usage.replies if usage else [])
    files = claude_md_files(project, home)
    claude = check_claude(project)
    skills_local = local_skills(project, home)
    plugin_skills = sum(p["skills"] or 0 for p in claude["plugins"] or [])
    return {"start": start, "claude_md": files, "claude_md_tokens": sum(f["tokens"] for f in files),
            "mcp": claude["mcp"], "plugins": claude["plugins"], "plugins_skipped": claude["plugins_skipped"],
            "skills": {"local": skills_local, "plugins": plugin_skills if claude["plugins"] is not None else None},
            "suggestions": suggestions(files, claude), "this_week": week_events(mem, now),
            "estimate_note": "Rows below the measured line are estimates (CLAUDE.md at characters / 4; plugin "
                             "figures from `claude plugin details`)."}


def format_audit(res: dict) -> str:
    s = res["start"]
    if s["sessions"]:
        out = [f"Every new chat starts at about {s['median']:,} tokens before your first message is answered "
               f"(measured from {s['sessions']} chats; smallest {s['min']:,}, largest {s['max']:,})."]
    else:
        out = ["Every new chat: no measured start yet (no main-chat replies found in the transcripts)."]
    out += ["", "What loads into every chat (estimate):"]
    for f in res["claude_md"]:
        out.append(f"- CLAUDE.md  {f['path']}: about {f['tokens']:,} tokens ({f['chars']:,} chars / 4)")
    if not res["claude_md"]:
        out.append("- CLAUDE.md files: none found")
    if res["mcp"] is None:
        out.append(f"- MCP servers: {NOT_CHECKED} (`claude` not available)")
    elif not res["mcp"]:
        out.append("- MCP servers: none")
    for m in res["mcp"] or []:
        tools = f"{m['tools']} tools" if m["tools"] is not None else "tool count not shown"
        out.append(f"- MCP server {m['name']}: {tools}")
    if res["plugins"] is None:
        out.append(f"- Plugins: {NOT_CHECKED} (`claude` not available)")
    for p in res["plugins"] or []:
        cost = f"about {p['tokens']:,} tokens" if p["tokens"] is not None else NOT_CHECKED
        out.append(f"- Plugin {p['name']}: {cost}")
    if res["plugins_skipped"]:
        out.append(f"- {res['plugins_skipped']} more plugins not checked (limit {MAX_PLUGINS})")
    sk = res["skills"]
    out.append(f"- Skills: {sk['local']} of your own" + (f", {sk['plugins']} from plugins" if sk["plugins"] is not None
                                                          else f", plugins {NOT_CHECKED}"))
    out += ["", "Biggest things to trim (estimates):"]
    out += [f"{i}. {s_['text']}" for i, s_ in enumerate(res["suggestions"], 1)] or ["Nothing large found."]
    w = res["this_week"]
    def anyway(n):
        return f" ({n} more read again anyway)" if n else ""

    out += ["", "What this plugin saved this week:",
            f"- repeated reads declined: {w['read_denied']}{anyway(w['read_retries'])}",
            f"- big-file steers: {w['big_read_steers']}{anyway(w['steer_retries'])} (outlines of "
            f"{w['steer_outline_chars']:,} characters sent instead of files of {w['steer_file_lines']:,} lines)",
            "", "What it added this week:",
            f"- related memory for a first question: {w['related_memory']} ({w['related_memory_chars']:,} characters)"]
    return "\n".join(out)


def audit_main(args: list[str], stdout: TextIO, now: datetime | None = None) -> int:
    rest = list(args)
    project, transcripts = Path.cwd(), None
    for flag in ("--project", "--transcripts"):
        if flag in rest:
            i = rest.index(flag)
            if i + 1 >= len(rest):
                stdout.write(USAGE)
                return 2
            value = Path(rest[i + 1]).expanduser()
            del rest[i:i + 2]
            if flag == "--project":
                project = value
            else:
                transcripts = value
    as_json = "--json" in rest
    if [a for a in rest if a != "--json"]:
        stdout.write(USAGE)
        return 2
    res = run_audit(project, transcripts, now=now)
    stdout.write(json.dumps(res, indent=1) + "\n" if as_json else format_audit(res) + "\n")
    return 0
