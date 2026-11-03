"""`setup-mcp`: add the session-memory MCP server to Claude Code, VS Code or Antigravity.

JSON edits keep every other key, back the old file up first (`<file>.<timestamp>.bak`),
write atomically and are idempotent. Every change is printed before it is made;
--dry-run changes nothing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TextIO

REPO = Path(__file__).resolve().parent.parent
NAME = "session-memory"
TARGETS = ("claude", "vscode", "antigravity")
SCOPES = ("user", "project", "local")
USAGE = ("Usage: setup-mcp <claude|vscode|antigravity> [--project PATH] [--dry-run] "
         "[--scope user|project|local]\n")


class Refused(Exception):
    """The config file cannot be edited safely; the message says why."""


def _is_windows() -> bool:
    return os.name == "nt"


def launcher() -> tuple[str, list[str]]:
    """(command, leading args) that start Python >= 3.10 without a shell script."""
    if _is_windows():
        if shutil.which("py"):
            return "py", ["-3"]
        return Path(sys.executable).as_posix(), []
    return sys.executable, []


def _server_args(project_arg: str) -> list[str]:
    return ["-m", "memgraph.cli", "mcp", "--project", project_arg]


def json_entry(project_arg: str, with_type: bool) -> dict:
    command, prefix = launcher()
    entry = {"type": "stdio"} if with_type else {}
    entry.update({"command": command, "args": prefix + _server_args(project_arg),
                  "env": {"PYTHONPATH": REPO.as_posix()}})
    return entry


def _load(path: Path, key: str) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        if re.search(r"^\s*//|/\*", text, re.M):
            raise Refused(f"{path} has comments (JSON with comments is not supported). "
                          "Add the entry below by hand instead.") from None
        raise Refused(f"{path} is not valid JSON. Fix it first, or add the entry below by hand.") from None
    if not isinstance(data, dict):
        raise Refused(f"{path} is not valid for this tool (expected a JSON object at the top).")
    if key in data and not isinstance(data[key], dict):
        raise Refused(f'{path}: "{key}" is not a JSON object. Fix it first, or add the entry below by hand.')
    return data


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_server(path: Path, key: str, entry: dict, dry_run: bool, out: TextIO) -> int:
    """Put `entry` at data[key]["session-memory"] in the JSON file at `path`."""
    snippet = json.dumps({NAME: entry}, indent=2)
    try:
        data = _load(path, key)
    except Refused as exc:
        out.write(f"Not changed: {exc}\n{snippet}\n")
        return 2
    except OSError as exc:
        out.write(f"Not changed: cannot read {path} ({exc}).\n")
        return 2
    servers = data.get(key) or {}
    if servers.get(NAME) == entry:
        out.write(f"Already set up: {path} has this session-memory entry. Nothing to change.\n")
        return 0
    verb = "Update" if NAME in servers else "Add"
    where = "existing file" if path.exists() else "new file"
    out.write(f'{verb} "{NAME}" under "{key}" in {path} ({where}):\n{snippet}\n')
    if dry_run:
        out.write("Dry run: nothing changed.\n")
        return 0
    try:
        if path.exists():
            backup = path.with_name(f"{path.name}.{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak")
            shutil.copy2(path, backup)
            out.write(f"Backup saved: {backup}\n")
        data[key] = {**servers, NAME: entry}
        _write_atomic(path, data)
    except OSError as exc:
        out.write(f"Could not write {path}: {exc}\n")
        return 1
    out.write(f"{'Updated' if verb == 'Update' else 'Added'} session-memory in {path}. "
              "Restart the editor (or reload its MCP servers) to use it.\n")
    return 0


def claude_command(claude: str, project: Path, scope: str) -> list[str]:
    command, prefix = launcher()
    return [claude, "mcp", "add", "-s", scope, NAME, "-e", f"PYTHONPATH={REPO.as_posix()}", "--",
            command, *prefix, *_server_args(str(project))]


def _show(cmd: list[str]) -> str:
    if _is_windows():
        return subprocess.list2cmdline(cmd)
    import shlex

    return shlex.join(cmd)


def setup_claude(project: Path, scope: str, dry_run: bool, out: TextIO) -> int:
    claude = shutil.which("claude")
    cmd = claude_command("claude", project, scope)
    out.write(f"Command:\n  {_show(cmd)}\n")
    if dry_run:
        out.write("Dry run: nothing changed.\n")
        return 0
    if not claude:
        out.write("Claude Code CLI (`claude`) not found on PATH. Install Claude Code, or run the command above later.\n")
        return 1
    try:
        r = subprocess.run([claude, *cmd[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60, stdin=subprocess.DEVNULL, cwd=str(project))
    except (OSError, subprocess.SubprocessError) as exc:
        out.write(f"Could not run claude: {exc}\n")
        return 1
    said = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode == 0:
        out.write((said + "\n" if said else "") + "Added session-memory to Claude Code. Restart Claude Code to use it.\n")
        return 0
    if "already exists" in said.lower():
        out.write(f"Already set up: {said}\nTo replace it: claude mcp remove {NAME} -s {scope}, then run this again.\n")
        return 0
    out.write(f"claude mcp add failed (exit {r.returncode}): {said}\n")
    return 1


def setup_main(args: list[str], out: TextIO, home: Path | None = None) -> int:
    rest = list(args)
    opts: dict[str, str] = {}
    for flag in ("--project", "--scope"):
        if flag in rest:
            i = rest.index(flag)
            if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
                out.write(USAGE)
                return 2
            opts[flag] = rest[i + 1]
            del rest[i:i + 2]
    dry_run = "--dry-run" in rest
    rest = [a for a in rest if a != "--dry-run"]
    scope = opts.get("--scope", "user")
    if len(rest) != 1 or rest[0] not in TARGETS or scope not in SCOPES:
        out.write(USAGE)
        return 2
    target = rest[0]
    project = Path(opts.get("--project") or Path.cwd()).expanduser().resolve()
    if target == "claude":
        return setup_claude(project, scope, dry_run, out)
    if target == "vscode":
        return merge_server(project / ".vscode" / "mcp.json", "servers", json_entry("${workspaceFolder}", True),
                            dry_run, out)
    home = Path(home) if home else Path.home()
    return merge_server(home / ".gemini" / "antigravity" / "mcp_config.json", "mcpServers",
                        json_entry(project.as_posix(), False), dry_run, out)
