"""`doctor`: is session-memory set up for this project? Read-only; each check isolated.

Statuses: "ok" (✓), "fail" (✗) and "skip" (–, unknown or optional). Only a failed required
check (Python, plugin files) makes the exit code 1. Nothing is created or changed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TextIO

from memgraph.ingest import load_state
from memgraph.usage import project_slug

REPO = Path(__file__).resolve().parent.parent
PLUGIN_ID = "session-memory@session-memory"
CLAUDE_TIMEOUT = 15
MARKS = {"ok": "✓", "fail": "✗", "skip": "–"}


def _quote(path: Path) -> str:
    return f'"{path}"' if " " in str(path) else str(path)


def _cli() -> str:
    """How to run this CLI from any folder: the launcher script in the repo."""
    return _quote(REPO / "bin" / ("run-python.cmd" if os.name == "nt" else "run-python"))


def _install_fix() -> str:
    return (f"claude plugin marketplace add {_quote(REPO)}, then claude plugin install {PLUGIN_ID}"
            " (or run install.ps1 / install.sh)")


def _check(name, status, detail, fix="", required=False) -> dict:
    return {"name": name, "status": status, "detail": detail, "fix": fix if status != "ok" else "",
            "required": required}


def _version() -> tuple:
    return tuple(sys.version_info[:3])


def _has_pillow() -> bool:
    return importlib.util.find_spec("PIL") is not None


def _check_python(ctx) -> dict:
    v = _version()
    text = ".".join(str(x) for x in v)
    if v >= (3, 10):
        return _check("python", "ok", f"Python {text} ({sys.executable})", required=True)
    return _check("python", "fail", f"Python {text} ({sys.executable}) is too old",
                  "install Python 3.10 or newer from python.org", required=True)


def _check_pillow(ctx) -> dict:
    if _has_pillow():
        return _check("pillow", "ok", "Pillow installed (pictures in packs and the map)")
    return _check("pillow", "skip", "Pillow not installed (optional: only memory.png needs it)", "pip install pillow")


def _check_plugin_files(ctx) -> dict:
    repo = REPO
    wanted = ["hooks/hooks.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
              "bin/run-python", "bin/run-python.cmd"]
    missing = [w for w in wanted if not (repo / w).is_file()]
    if not list((repo / "commands").glob("*.md")):
        missing.append("commands/*.md")
    if missing:
        return _check("plugin_files", "fail", f"Plugin files missing in {repo}: {', '.join(missing)}",
                      "download the repository again (git clone or a fresh zip)", required=True)
    return _check("plugin_files", "ok", f"Plugin files present ({repo})", required=True)


def _check_claude_cli(ctx) -> dict:
    if ctx["claude"]:
        return _check("claude_cli", "ok", f"Claude Code CLI found ({ctx['claude']})")
    return _check("claude_cli", "fail", "Claude Code CLI (`claude`) not found on PATH",
                  "install Claude Code, then open a new terminal")


def _run_claude(ctx, args: list[str]):
    """(returncode, stdout) of `claude <args>`, or None when it could not be asked."""
    if not ctx["claude"]:
        return None
    try:
        r = subprocess.run([ctx["claude"], *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=CLAUDE_TIMEOUT, stdin=subprocess.DEVNULL,
                           cwd=str(ctx["project"]))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.returncode, r.stdout or ""


def _check_plugin_installed(ctx) -> dict:
    res = ctx["plugin_list"]
    if res is None or res[0] != 0:
        why = "Claude Code CLI not found" if not ctx["claude"] else "`claude plugin list` gave no answer"
        return _check("plugin_installed", "skip", f"Plugin install state unknown ({why})", _install_fix())
    try:
        plugins = json.loads(res[1])
    except ValueError:
        return _check("plugin_installed", "skip", "Plugin install state unknown (unreadable answer)", _install_fix())
    mine = [p for p in plugins if isinstance(p, dict) and str(p.get("id", "")).startswith("session-memory@")]
    if not mine:
        return _check("plugin_installed", "fail", "Plugin not installed in Claude Code", _install_fix())
    if not any(p.get("enabled") for p in mine):
        return _check("plugin_installed", "fail", f"Plugin installed but disabled ({mine[0]['id']})",
                      f"claude plugin enable {mine[0]['id']}")
    return _check("plugin_installed", "ok", f"Plugin installed and enabled ({mine[0]['id']})")


def _transcripts_dir(ctx) -> Path:
    mem = ctx["mem"]
    for name in ("state.json", "guard.json"):
        recorded = load_state(mem / name).get("transcripts_dir") if mem.is_dir() else None
        if isinstance(recorded, str) and recorded:
            return Path(recorded)
    return ctx["home"] / ".claude" / "projects" / project_slug(ctx["project"])


def _check_transcripts(ctx) -> dict:
    tdir = _transcripts_dir(ctx)
    if tdir.is_dir():
        n = len(list(tdir.glob("*.jsonl")))
        return _check("transcripts", "ok", f"Transcripts folder found: {n} chat file(s) in {tdir}")
    return _check("transcripts", "skip", f"No transcripts yet for this project (looked in {tdir})",
                  "open Claude Code in this folder and send a message")


def _check_memory(ctx) -> dict:
    mem = ctx["mem"]
    graph = mem / "graph.json"
    if not graph.is_file():
        return _check("memory", "skip", f"Memory not built yet ({mem})",
                      "it appears after Claude Code's first reply with the plugin installed")
    try:
        data = json.loads(graph.read_text(encoding="utf-8", errors="replace"))
        nodes = data.get("nodes") if isinstance(data, dict) else None
        count = len(nodes) if isinstance(nodes, dict) else 0
    except (OSError, ValueError):
        return _check("memory", "fail", f"Memory file unreadable ({graph})",
                      "delete graph.json and state.json there; the next reply rebuilds them")
    return _check("memory", "ok", f"Memory folder exists: {count} items ({mem})")


def _check_watcher(ctx) -> dict:
    from memgraph import watch

    mem = ctx["mem"]
    data = load_state(mem / watch.WATCH_FILE) if mem.is_dir() else {}
    deadline = data.get("deadline")
    if not deadline:
        return _check("watcher", "skip", "Cache watcher: no deadline yet (set after a reply)")
    running = mem.is_dir() and watch._lock_live(mem / watch.LOCK_FILE)
    told = " (already announced)" if data.get("notified_for") == deadline else ""
    return _check("watcher", "ok", f"Cache watcher: deadline {deadline}{told}, "
                                   f"{'watcher running' if running else 'no watcher running'}")


def _check_mcp_claude(ctx) -> dict:
    fix = f"{_cli()} setup-mcp claude (optional; the plugin already gives Claude Code the memory)"
    res = ctx["mcp_list"]
    if res is None or res[0] != 0:
        return _check("mcp_claude", "skip", "MCP in Claude Code: unknown (`claude mcp list` gave no answer)", fix)
    if any(re.match(r"\s*session-memory\s*:", line) for line in res[1].splitlines()):
        return _check("mcp_claude", "ok", "MCP configured in Claude Code")
    return _check("mcp_claude", "skip", "MCP not configured in Claude Code (optional)", fix)


def _json_has_server(path: Path, key: str, name: str, label: str, fix: str) -> dict:
    if not path.is_file():
        return _check(name, "skip", f"MCP not configured in {label} (optional; no {path})", fix)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return _check(name, "fail", f"MCP config for {label} is not valid JSON ({path})",
                      "fix the file by hand, then run " + fix)
    servers = data.get(key) if isinstance(data, dict) else None
    if isinstance(servers, dict) and "session-memory" in servers:
        return _check(name, "ok", f"MCP configured in {label} ({path})")
    return _check(name, "skip", f"MCP not configured in {label} (optional; {path} has no session-memory)", fix)


def _check_mcp_vscode(ctx) -> dict:
    return _json_has_server(ctx["project"] / ".vscode" / "mcp.json", "servers", "mcp_vscode", "VS Code",
                            f"{_cli()} setup-mcp vscode")


def _check_mcp_antigravity(ctx) -> dict:
    return _json_has_server(ctx["home"] / ".gemini" / "antigravity" / "mcp_config.json", "mcpServers",
                            "mcp_antigravity", "Antigravity", f"{_cli()} setup-mcp antigravity")


def _check_statusline(ctx) -> dict:
    from memgraph.statusline import is_ours, settings_path

    path = settings_path(ctx["home"])
    fix = f"{_cli()} setup-statusline (optional: chat size, cache time left and tokens saved)"
    if not path.is_file():
        return _check("statusline", "skip", f"Status line not set (optional; no {path})", fix)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return _check("statusline", "skip", f"Status line unknown: {path} is not valid JSON", fix)
    current = data.get("statusLine") if isinstance(data, dict) else None
    if current is None:
        return _check("statusline", "skip", "Status line not set (optional)", fix)
    if is_ours(current):
        from memgraph.statusline import entry, shim_problem

        if current != entry(ctx["home"]):
            return _check("statusline", "fail", "Status line points at the plugin folder directly (breaks on "
                          "plugin updates)", f"{_cli()} setup-statusline (writes a stable shim)")
        problem = shim_problem(ctx["home"])
        if problem:
            return _check("statusline", "fail", f"Status line set but {problem}", f"{_cli()} setup-statusline")
        return _check("statusline", "ok", f"Status line set ({path})")
    return _check("statusline", "skip", "Another status line is set (kept as is)",
                  f"{_cli()} setup-statusline --force to replace it (a backup is kept)")


CHECKS = [("python", "_check_python", True), ("pillow", "_check_pillow", False),
          ("plugin_files", "_check_plugin_files", True), ("claude_cli", "_check_claude_cli", False),
          ("plugin_installed", "_check_plugin_installed", False), ("transcripts", "_check_transcripts", False),
          ("memory", "_check_memory", False), ("watcher", "_check_watcher", False),
          ("mcp_claude", "_check_mcp_claude", False), ("mcp_vscode", "_check_mcp_vscode", False),
          ("mcp_antigravity", "_check_mcp_antigravity", False), ("statusline", "_check_statusline", False)]


def run_checks(project: Path, home: Path | None = None) -> list[dict]:
    project = Path(project).resolve()
    ctx = {"project": project, "home": Path(home) if home else Path.home(),
           "mem": project / ".claude" / "session-memory"}
    try:
        ctx["claude"] = shutil.which("claude")
    except Exception:
        ctx["claude"] = None
    with ThreadPoolExecutor(max_workers=2) as pool:  # both may take seconds; ask at once
        plugin_list = pool.submit(_run_claude, ctx, ["plugin", "list", "--json"])
        mcp_list = pool.submit(_run_claude, ctx, ["mcp", "list"])
        ctx["plugin_list"], ctx["mcp_list"] = plugin_list.result(), mcp_list.result()
    results = []
    for name, func, required in CHECKS:
        try:
            results.append(globals()[func](ctx))
        except Exception as exc:  # one broken check must not hide the others
            results.append(_check(name, "fail" if required else "skip", f"{name}: check failed ({exc})",
                                  "report this at github.com/ArpitThakur7/session-memory/issues", required))
    return results


USAGE = "Usage: doctor [--project PATH] [--json]\n"


def doctor_main(args: list[str], stdout: TextIO) -> int:
    project = Path.cwd()
    rest = list(args)
    if "--project" in rest:
        i = rest.index("--project")
        if i + 1 >= len(rest):
            stdout.write(USAGE)
            return 2
        project = Path(rest[i + 1]).expanduser()
        del rest[i:i + 2]
    if any(a != "--json" for a in rest):
        stdout.write(USAGE)
        return 2
    project = project.resolve()
    results = run_checks(project)
    ok = not any(c["required"] and c["status"] == "fail" for c in results)
    if "--json" in rest:
        stdout.write(json.dumps({"project": str(project), "ok": ok, "checks": results}, indent=1) + "\n")
    else:
        stdout.write(f"session-memory doctor for {project}\n")
        for c in results:
            stdout.write(f"{MARKS[c['status']]} {c['detail']}\n")
            if c["fix"]:
                stdout.write(f"    fix: {c['fix']}\n")
        stdout.write("All required checks passed.\n" if ok else "A required check failed; see the fix above.\n")
    return 0 if ok else 1
