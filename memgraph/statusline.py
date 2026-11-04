"""Status line: `<model> · chat 142k (71%) · cache 38m · saved 1.2M`, and `setup-statusline`.

`statusline` reads the JSON Claude Code pipes to a status line command and prints one line.
It reads only small files (watch.json, summary.json), never transcripts, and never creates the
memory folder. Field names follow code.claude.com/docs/en/statusline: model.display_name,
context_window.{current_usage,total_input_tokens,context_window_size,used_percentage},
prompt_cache.{warm,expires_at} (epoch seconds), workspace.{current_dir,project_dir}, cwd.

`setup-statusline` adds that command to ~/.claude/settings.json: backup first, printed diff,
--dry-run, refuses invalid JSON and someone else's status line (unless --force).
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
    if tokens is not None:
        warn = warn_tokens()
        if tokens >= warn:
            colour = RED if tokens >= 1.5 * warn else YELLOW
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


def command() -> str:
    """The launcher in this repository, quoted, plus `statusline` (works from cmd and bash)."""
    launcher = (REPO / "bin" / ("run-python.cmd" if os.name == "nt" else "run-python")).as_posix()
    return f'"{launcher}" statusline'


def entry() -> dict:
    return {"type": "command", "command": command(), "padding": 0}


def is_ours(value) -> bool:
    cmd = value.get("command") if isinstance(value, dict) else None
    return isinstance(cmd, str) and "run-python" in cmd and cmd.rstrip().endswith(" statusline")


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def setup_statusline_main(args: list[str], out: TextIO, home: Path | None = None) -> int:
    from memgraph.setup_mcp import Refused, _load, _write_atomic

    if any(a not in ("--dry-run", "--force") for a in args):
        out.write(USAGE)
        return 2
    dry_run, force = "--dry-run" in args, "--force" in args
    path = settings_path(home)
    new_entry = entry()
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
    if current == new_entry:
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
    out.write(f"{'Update' if current is not None else 'Add'} statusLine in {path} "
              f"({'existing file' if path.exists() else 'new file'}):\n{diff}")
    if dry_run:
        out.write("Dry run: nothing changed.\n")
        return 0
    try:
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
