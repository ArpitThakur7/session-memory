"""Status line: `<model> · chat 142k (71%) · cache 38m · saved 1.2M`, and `setup-statusline`.

`statusline` reads the JSON Claude Code pipes to a status line command and prints one line.
It reads only small files (watch.json, summary.json), never transcripts, and never creates the
memory folder. Field names follow code.claude.com/docs/en/statusline: model.display_name,
context_window.{current_usage,total_input_tokens,context_window_size,used_percentage},
prompt_cache.{warm,expires_at} (epoch seconds), workspace.{current_dir,project_dir}, cwd.

`setup-statusline` writes a small stable shim to <config dir>/session-memory/ (statusline.py plus
statusline.sh and statusline.cmd that start it with one Python) and points ~/.claude/settings.json
at it: backup first, printed diff, --dry-run, refuses invalid JSON and someone else's status line
(unless --force). The shim finds the current plugin install itself (CLAUDE_PLUGIN_ROOT is not set
for status lines), so a plugin update never leaves the status line pointing at a deleted folder.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

REPO = Path(__file__).resolve().parent.parent
DEFAULT_WARN_TOKENS = 150_000
BIG_WINDOW = 200_000          # larger windows (1M) warn by percentage, not by token count
WARN_PERCENT, RED_PERCENT = 75, 90
MAX_CHARS = 120
SEP = " · "
YELLOW, RED, RESET = "\x1b[33m", "\x1b[31m", "\x1b[0m"


def _num(value) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _get(data, *keys):
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def warn_tokens() -> int:
    try:
        value = int(os.environ.get("SESSION_MEMORY_WARN_TOKENS", ""))
    except ValueError:
        return DEFAULT_WARN_TOKENS
    return value if value > 0 else DEFAULT_WARN_TOKENS


def fmt_tokens(n: float) -> str:
    if n >= 999_500:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{round(n / 1000)}k"
    return str(int(n))


def _fmt_minutes(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _context(data: dict) -> tuple[float | None, float | None]:
    """(tokens in the chat now, percent of the window) from the most recent API response."""
    cw = _get(data, "context_window")
    usage = _get(cw, "current_usage")
    tokens = None
    if isinstance(usage, dict):
        parts = [_num(usage.get(k)) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
        if any(p is not None for p in parts):
            tokens = sum(p or 0 for p in parts)
    if tokens is None:
        tokens = _num(_get(cw, "total_input_tokens"))
    pct = _num(_get(cw, "used_percentage"))
    size = _num(_get(cw, "context_window_size"))
    if pct is None and tokens is not None and size:
        pct = 100 * tokens / size
    return tokens, pct


def _mem_dir(data: dict) -> Path | None:
    """The project's memory folder if it already exists (never created here).

    The project is CLAUDE_PROJECT_DIR, else workspace.project_dir, else the current folder, so a
    stray folder in a subdirectory is never used."""
    env = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    for value in (env, _get(data, "workspace", "project_dir"), _get(data, "workspace", "current_dir"),
                  data.get("cwd")):
        if isinstance(value, str) and value:
            d = Path(value) / ".claude" / "session-memory"
            return d if d.is_dir() else None
    return None


def settings_path(home: Path | None = None) -> Path:
    """Claude Code's user settings file: $CLAUDE_CONFIG_DIR/settings.json, else ~/.claude/settings.json."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if cfg:
        return Path(cfg).expanduser() / "settings.json"
    return (Path(home) if home else Path.home()) / ".claude" / "settings.json"


def _read_text(path: Path) -> str | None:
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read().replace("\r\n", "\n")
    except OSError:
        return None


def _read_json(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _cache(data: dict, mem: Path | None, now: datetime) -> str | None:
    pc = data.get("prompt_cache")
    if isinstance(pc, dict) and ("expires_at" in pc or pc.get("warm") is False):
        expires = _num(pc.get("expires_at"))
        if pc.get("warm") is False or expires is None or expires <= now.timestamp():
            return "cache cold"
        return f"cache {_fmt_minutes(expires - now.timestamp())}"
    if mem is None:
        return None
    from memgraph.transcript import parse_ts

    text = _read_json(mem / "watch.json").get("deadline")
    dl = parse_ts(text) if isinstance(text, str) else None
    if dl is None:
        return None
    dl = dl if dl.tzinfo else dl.replace(tzinfo=timezone.utc)
    left = (dl - now).total_seconds()
    return f"cache {_fmt_minutes(left)}" if left > 0 else "cache cold"


def render(data: dict, now: datetime) -> str:
    if not isinstance(data, dict):
        return ""
    parts: list[tuple[str, str]] = []  # (plain text, colour)
    model = _get(data, "model", "display_name")
    tokens, pct = _context(data)
    if not isinstance(model, str) and tokens is None:
        return ""  # nothing we understand: print nothing rather than noise
    if isinstance(model, str) and model.strip():
        model = model.strip()
        parts.append((model if len(model) <= 30 else model[:29] + "…", ""))
    if tokens is not None and tokens <= 0:
        parts.append(("chat —", ""))  # no reply yet: the size is not known
    elif tokens is not None:
        size = _num(_get(data, "context_window", "context_window_size"))
        if size is not None and size > BIG_WINDOW and pct is not None:
            colour = RED if pct >= RED_PERCENT else YELLOW if pct >= WARN_PERCENT else ""
        else:
            warn = warn_tokens()
            colour = RED if tokens >= 1.5 * warn else YELLOW if tokens >= warn else ""
        if colour:
            parts.append((f"chat {fmt_tokens(tokens)} — /compact or /clear soon", colour))
        else:
            parts.append((f"chat {fmt_tokens(tokens)}" + (f" ({round(pct)}%)" if pct is not None else ""), ""))
    mem = _mem_dir(data)
    cache = _cache(data, mem, now)
    if cache:
        parts.append((cache, ""))
    if mem is not None:
        saved = _num(_read_json(mem / "summary.json").get("measured_saved_tokens"))
        if saved is not None:
            parts.append((f"saved {fmt_tokens(saved)}", ""))
    while parts and len(SEP.join(p for p, _ in parts)) > MAX_CHARS:
        parts.pop()
    return SEP.join(f"{c}{p}{RESET}" if c else p for p, c in parts)


def statusline_main(stdin: TextIO, stdout: TextIO, now: datetime) -> int:
    """Never fails: a status line that crashes is worse than an empty one."""
    if os.environ.get("SESSION_MEMORY_STATUSLINE", "1").strip() == "0":
        return 0
    try:
        data = json.loads(stdin.read())
        text = render(data, now)
    except Exception:
        text = ""
    if text:
        stdout.write(text + "\n")
    return 0


# ---- setup-statusline ----

USAGE = "Usage: setup-statusline [--dry-run] [--force]\n"


SHIM_DIR = "session-memory"
SHIM_FILES = ("statusline.py", "statusline.sh", "statusline.cmd")

_SHIM_PY = """# session-memory status line shim, written by `setup-statusline` (run it again to rewrite this).
# Status lines get no CLAUDE_PLUGIN_ROOT, so find the install here: the development checkout recorded
# at setup when it still exists, else the newest plugin install in Claude Code's plugin cache.
import glob
import io
import os
import sys

RECORDED = {recorded!r}
CONFIG = {cfg!r}


def _ok(root):
    return bool(root) and os.path.isfile(os.path.join(root, "memgraph", "cli.py"))


def _root():
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or CONFIG
    cache = os.path.normcase(os.path.abspath(os.path.join(cfg, "plugins", "cache")))
    recorded = os.path.normcase(os.path.abspath(RECORDED))
    if _ok(RECORDED) and not recorded.startswith(cache + os.sep):
        return RECORDED
    found = [d for d in glob.glob(os.path.join(glob.escape(cfg), "plugins", "cache", "*", "session-memory", "*"))
             if _ok(d)]
    if found:
        return max(found, key=os.path.getmtime)
    return RECORDED if _ok(RECORDED) else None


def run():
    if sys.version_info < (3, 10):
        return 0
    root = _root()
    if root is None:
        return 0
    sys.path.insert(0, root)
    from memgraph.cli import main

    sys.stdout.reconfigure(encoding="utf-8")
    return main(["statusline"], io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"), sys.stdout)


if __name__ == "__main__":
    sys.exit(run())
"""

_SHIM_SH = """#!/usr/bin/env bash
# session-memory status line shim (written by setup-statusline): one Python start, Store stub skipped.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
shopt -s execfail
for c in python3 python py; do
  p="$(command -v "$c" 2>/dev/null)" || continue
  case "$p" in *WindowsApps*) continue ;; esac
  exec "$p" "$D/statusline.py"
done
exit 0
"""

_SHIM_CMD = """@echo off
rem session-memory status line shim (written by setup-statusline): one Python start.
where py >nul 2>&1
if errorlevel 1 goto trypython
py -3 "%~dp0statusline.py"
if errorlevel 100 goto trypython
exit /b 0
:trypython
python "%~dp0statusline.py"
exit /b 0
"""


def shim_dir(home: Path | None = None) -> Path:
    """Stable folder for the shim, next to the settings file (survives plugin updates)."""
    return settings_path(home).parent / SHIM_DIR


def shim_contents(recorded: Path = REPO, cfg: Path | None = None) -> dict[str, tuple[str, str]]:
    """file name -> (text, newline) of the three shim files."""
    cfg = Path(cfg) if cfg is not None else settings_path().parent
    py = _SHIM_PY.replace("{recorded!r}", repr(str(recorded))).replace("{cfg!r}", repr(str(cfg)))
    return {"statusline.py": (py, "\n"), "statusline.sh": (_SHIM_SH, "\n"), "statusline.cmd": (_SHIM_CMD, "\r\n")}


def write_shim(folder: Path, recorded: Path = REPO, cfg: Path | None = None) -> Path:
    """Write the shim files into `folder`; returns the path of statusline.py."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name, (text, newline) in shim_contents(recorded, cfg).items():
        target = folder / name
        with open(target, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
        if name == "statusline.sh":
            try:
                target.chmod(0o755)
            except OSError:
                pass
    return folder / "statusline.py"


def command(home: Path | None = None) -> str:
    """The shim for this platform, quoted (works from cmd and bash)."""
    name = "statusline.cmd" if os.name == "nt" else "statusline.sh"
    return f'"{(shim_dir(home) / name).as_posix()}"'


def entry(home: Path | None = None) -> dict:
    return {"type": "command", "command": command(home), "padding": 0}


def is_ours(value) -> bool:
    cmd = value.get("command") if isinstance(value, dict) else None
    if not isinstance(cmd, str):
        return False
    cmd = cmd.strip().strip('"').replace("\\", "/")
    return (("run-python" in cmd and cmd.endswith(" statusline"))  # before the shim: the launcher itself
            or cmd.endswith(f"{SHIM_DIR}/statusline.cmd") or cmd.endswith(f"{SHIM_DIR}/statusline.sh"))


def shim_problem(home: Path | None = None) -> str | None:
    """Why the installed shim cannot work (missing files, no install found), else None."""
    folder = shim_dir(home)
    missing = [n for n in SHIM_FILES if not (folder / n).is_file()]
    if missing:
        return f"shim files missing in {folder}: {', '.join(missing)}"
    import importlib.util

    try:
        spec = importlib.util.spec_from_file_location("_sm_statusline_shim", folder / "statusline.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        root = mod._root()
    except Exception as exc:  # a broken shim: report it, never raise
        return f"shim cannot run ({type(exc).__name__})"
    return None if root else "no session-memory install found for the shim"


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def setup_statusline_main(args: list[str], out: TextIO, home: Path | None = None) -> int:
    from memgraph.setup_mcp import Refused, _load, _write_atomic

    if any(a not in ("--dry-run", "--force") for a in args):
        out.write(USAGE)
        return 2
    dry_run, force = "--dry-run" in args, "--force" in args
    path = settings_path(home)
    new_entry = entry(home)
    folder = shim_dir(home)
    shim_text = {n: c for n, (c, _nl) in shim_contents(REPO, path.parent).items()}
    shim_stale = any(_read_text(folder / n) != c for n, c in shim_text.items())
    snippet = json.dumps({"statusLine": new_entry}, indent=2)
    try:
        data = _load(path, "statusLine")
    except Refused as exc:
        out.write(f"Not changed: {exc}\n{snippet}\n")
        return 2
    except OSError as exc:
        out.write(f"Not changed: cannot read {path} ({exc}).\n")
        return 2
    current = data.get("statusLine")
    if current == new_entry and not shim_stale:
        out.write(f"Already set up: {path} uses the session-memory status line. Nothing to change.\n")
        return 0
    if current is not None and not is_ours(current) and not force:
        out.write(f"Not changed: {path} already has a different status line:\n"
                  f"{json.dumps({'statusLine': current}, indent=2)}\n"
                  "Run again with --force to replace it (a backup is kept), or add this by hand:\n"
                  f"{snippet}\n")
        return 2
    updated = {**data, "statusLine": new_entry}
    before = _dump(data).splitlines(keepends=True) if path.exists() else []
    diff = "".join(difflib.unified_diff(before, _dump(updated).splitlines(keepends=True),
                                        f"{path} (before)", f"{path} (after)", n=1))
    if shim_stale:
        out.write(f"Write the status line shim (finds the current plugin install): "
                  f"{', '.join(str(folder / n) for n in SHIM_FILES)}\n")
    if current != new_entry:
        out.write(f"{'Update' if current is not None else 'Add'} statusLine in {path} "
                  f"({'existing file' if path.exists() else 'new file'}):\n{diff}")
    if dry_run:
        out.write("Dry run: nothing changed.\n")
        return 0
    try:
        if shim_stale:
            write_shim(folder, REPO, path.parent)
        if current == new_entry:
            out.write("Shim updated; settings unchanged.\n")
            return 0
        if path.exists():
            backup = path.with_name(f"{path.name}.{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak")
            shutil.copy2(path, backup)
            out.write(f"Backup saved: {backup}\n")
        _write_atomic(path, updated)
    except OSError as exc:
        out.write(f"Could not write {path}: {exc}\n")
        return 1
    out.write("Status line set. It appears after Claude Code's next reply (or restart Claude Code).\n")
    return 0
