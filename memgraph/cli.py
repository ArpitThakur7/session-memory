"""Command line entry point: Claude Code hooks plus remember/recall/stats/map."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO

from memgraph import savings, watch
from memgraph.brief import build_brief
from memgraph.graph import Graph, GraphBusy, graph_lock, update
from memgraph.guard import GuardConfig, decide, last_activity
from memgraph.ingest import ingest_dir, load_state, memory_dir, pack_size, record_pack_size, save_state
from memgraph.memory_ops import BUSY, REMEMBER_MAX_CHARS, TOO_LONG, recall_lines, remember
from memgraph.pack import PACK_FILE, SIZES, write_pack
from memgraph.usage import (GROUPINGS, PRICES_AS_OF, collect_usage, summarize, totals, totals_line,
                            transcripts_dir_for, usage_table)
from memgraph.viewer import write_map
from memgraph.visual import IMAGE_FILE, has_story, render_visual_pack


def _log_error(mem: Path | None) -> None:
    """Append the current traceback to error.log, or to the temp dir if that fails."""
    text = traceback.format_exc()
    targets = []
    if mem is not None:
        targets.append(Path(mem) / "error.log")
    targets.append(Path(tempfile.gettempdir()) / "session-memory-error.log")
    for target in targets:
        try:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now(timezone.utc).isoformat()}\n{text}\n")
            return
        except Exception:
            continue


def _ingest(payload: dict, mem: Path) -> Graph:
    """Ingest new transcript lines under graph.lock (graph and offsets saved together).

    When another writer holds the lock past the timeout, skip this run: ingest is
    offset-based, so the next hook catches up. The graph as stored is returned.
    """
    path = mem / "graph.json"
    try:
        with graph_lock(path):
            graph = Graph.load(path)
            state = load_state(mem / "state.json")
            transcripts = Path(os.path.dirname(payload["transcript_path"]))
            state["transcripts_dir"] = str(transcripts)  # lets `usage`/`savings` find them later
            ingest_dir(transcripts, graph, state)
            graph.save(path)
            save_state(mem / "state.json", state)
            return graph
    except GraphBusy:
        return Graph.load(path)


def _run_hook(name: str, payload: dict, mem: Path, stdout: TextIO, now: datetime) -> None:
    if name == "hook-stop":
        _ingest(payload, mem)
        watch.arm(mem, Path(payload["transcript_path"]), Path(payload["cwd"]), now)
    elif name == "hook-session-start":
        graph = _ingest(payload, mem)
        try:  # the brief points at the pack, so it must show what the brief shows
            write_pack(graph, mem, pack_size(mem), Path(payload["cwd"]))
        except Exception:
            _log_error(mem)  # the brief still works without a fresh pack; never point at a stale one
            try:
                (mem / PACK_FILE).unlink(missing_ok=True)
            except OSError:
                pass
        brief = build_brief(graph, project_root=Path(payload["cwd"]), mem_dir=mem)
        if brief:
            _emit(stdout, {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": brief}})
            if payload.get("source") in ("startup", "clear"):
                _log_event(mem, _fresh_start(graph, payload, brief))
    else:  # hook-prompt
        state = load_state(mem / "guard.json")  # own file: never races ingest's state.json
        state["transcripts_dir"] = os.path.dirname(payload["transcript_path"])
        last_ts, context = last_activity(Path(payload["transcript_path"]))
        reason = decide(now, last_ts, context, payload.get("prompt") or "", state, GuardConfig.from_env())
        save_state(mem / "guard.json", state)
        if reason:
            _emit(stdout, {"decision": "block", "reason": reason})
            _log_event(mem, {"type": "guard_block", "session": payload.get("session_id"), "context": context})


def _fresh_start(graph: Graph, payload: dict, brief: str) -> dict:
    """The fresh_start event: the new session, its brief size, and the chat it replaces."""
    new_sid = payload.get("session_id")
    last = graph.meta.get("last_prompt")
    prev = last.get("session") if isinstance(last, dict) else None
    prev = prev if isinstance(prev, str) and prev and prev != new_sid else None
    sess = (graph.meta.get("sessions") or {}).get(prev) if prev else None
    ctx = sess.get("context") if isinstance(sess, dict) else None
    return {"type": "fresh_start", "session": new_sid, "brief_chars": len(brief),
            "prev_session": prev, "prev_context": ctx if isinstance(ctx, int) else None}


def _log_event(mem: Path, event: dict) -> None:
    """Append to events.jsonl; a failure is logged, never raised (the hook's output already went out)."""
    try:
        savings.append_event(mem, event)
    except Exception:
        _log_error(mem)


def _emit(stdout: TextIO, obj: dict) -> None:
    stdout.write(json.dumps(obj, ensure_ascii=True))


def _project() -> tuple[Path, Graph]:
    mem = memory_dir(Path.cwd())
    return mem, Graph.load(mem / "graph.json")


def _remember(args: list[str], stdout: TextIO, now: datetime) -> int:
    text = " ".join(args).strip()
    if not text:
        stdout.write("Usage: remember <text>\n")
        return 2
    if len(text) > REMEMBER_MAX_CHARS:
        stdout.write(TOO_LONG.format(n=len(text)) + "\n")
        return 2
    mem = memory_dir(Path.cwd())
    try:
        update(mem / "graph.json", lambda graph: remember(graph, text, now))
    except GraphBusy:
        stdout.write(BUSY + "\n")
        return 1
    stdout.write("Saved to project memory.\n")
    return 0


def _recall(args: list[str], stdout: TextIO) -> int:
    term = " ".join(args).strip()
    _, graph = _project()
    out = recall_lines(graph, term)
    if not out:
        stdout.write(f'Nothing found for "{term}".\n')
        return 0
    stdout.write("\n".join(out) + "\n")
    return 0


def _stats(stdout: TextIO) -> int:
    _, graph = _project()
    sessions = graph.meta.get("sessions", {})
    stdout.write(
        f"nodes={len(graph.nodes)} edges={len(graph.edges)} "
        f"sessions={len(sessions)} brief_chars={len(build_brief(graph, project_root=Path.cwd()))}\n"
    )
    return 0


def _map(args: list[str], stdout: TextIO) -> int:
    project = Path.cwd()
    mem, graph = _project()
    try:
        tokens = savings.token_view(transcripts_dir_for(project, mem), mem)
    except Exception:  # the map must still draw; the Tokens section then says why
        _log_error(mem)
        tokens = {"available": False, "reason": "Token data could not be read (see error.log)."}
    path = write_map(graph, mem, project.name, project, tokens)
    stdout.write(f"Memory map: {path}\n")
    if "--no-open" not in args:
        webbrowser.open(path.as_uri())
    return 0


def _pack(args: list[str], stdout: TextIO) -> int:
    size = "standard"
    if "--size" in args:
        i = args.index("--size")
        size = args[i + 1] if i + 1 < len(args) else ""
    if size not in SIZES:
        stdout.write(f"Usage: pack [--size {'|'.join(SIZES)}] [--no-visual]\n")
        return 2
    project = Path.cwd()
    mem, graph = _project()
    written = write_pack(graph, mem, size, project)
    if written:
        record_pack_size(mem, size)
    stdout.write(f"Resume pack: {written}\n" if written else "Resume pack skipped: nothing to pack yet\n")
    if "--no-visual" not in args:
        target = mem / IMAGE_FILE
        if not has_story(graph):
            target.unlink(missing_ok=True)  # never leave a stale picture behind
            stdout.write("Visual pack skipped: nothing to draw yet\n")
            return 0
        png = render_visual_pack(graph, target, project)
        stdout.write(f"Visual pack: {png}\n" if png else "Visual pack skipped: install Pillow\n")
    return 0


_USAGE_HELP = "Usage: usage [--days N] [--by day|session|model|who] [--json] [--transcripts PATH]\n"
_SAVINGS_HELP = "Usage: savings [--json] [--transcripts PATH]\n"
NO_TRANSCRIPTS = "No Claude Code transcripts found at {path}. Pass --transcripts PATH.\n"


def _opt(args: list[str], name: str) -> str | None:
    """Value after `name`, "" when it is the last word, None when absent."""
    if name not in args:
        return None
    i = args.index(name)
    return args[i + 1] if i + 1 < len(args) else ""


def _transcripts(args: list[str]) -> tuple[Path, Path]:
    mem = memory_dir(Path.cwd())
    given = _opt(args, "--transcripts")
    return (Path(given).expanduser() if given else transcripts_dir_for(Path.cwd(), mem)), mem


def _usage(args: list[str], stdout: TextIO, now: datetime) -> int:
    days_text, by = _opt(args, "--days"), _opt(args, "--by") or "day"
    try:
        days = 7 if days_text is None else int(days_text)
    except ValueError:
        days = -1
    if days < 0 or by not in GROUPINGS:
        stdout.write(_USAGE_HELP)
        return 2
    tdir, _mem = _transcripts(args)
    if not tdir.is_dir():
        stdout.write(NO_TRANSCRIPTS.format(path=tdir))
        return 0
    result = collect_usage(tdir)
    since = now - timedelta(days=days)
    recent = [r for r in result.replies if r.ts >= since]
    rows, total = summarize(recent, by), totals(recent)
    if "--json" in args:
        stdout.write(json.dumps({"by": by, "days": days, "since": since.isoformat(), "rows": rows, "totals": total,
                                 "duplicates": result.duplicates, "prices_as_of": PRICES_AS_OF}, indent=1) + "\n")
        return 0
    stdout.write(f"Token usage, last {days} days, by {by} (exact, from transcripts):\n")
    stdout.write(usage_table(rows, by) + "\n\n" + totals_line(total, result.duplicates) + "\n")
    return 0


def _savings(args: list[str], stdout: TextIO) -> int:
    if any(a.startswith("-") and a not in ("--json", "--transcripts") for a in args):
        stdout.write(_SAVINGS_HELP)
        return 2
    tdir, mem = _transcripts(args)
    if not tdir.is_dir():
        stdout.write(NO_TRANSCRIPTS.format(path=tdir))
        return 0
    res = savings.measure_savings(tdir, mem, watch.ttl_minutes())
    stdout.write(json.dumps(res, indent=1) + "\n" if "--json" in args else savings.format_savings(res) + "\n")
    return 0


def _watch(args: list[str]) -> int:
    """Background cache watcher (started detached by hook-stop). Silent; errors go to error.log."""
    mem = None
    try:
        i = args.index("--project") if "--project" in args else -1
        project = Path(args[i + 1]) if 0 <= i < len(args) - 1 else Path.cwd()
        mem = memory_dir(project)
        watch.run_watcher(mem, project)
    except Exception:
        _log_error(mem)
    return 0


def _mcp(args: list[str]) -> int:
    from memgraph.mcp_server import serve

    i = args.index("--project") if "--project" in args else -1
    project = Path(args[i + 1]) if 0 <= i < len(args) - 1 else Path.cwd()
    serve(sys.stdin.buffer, sys.stdout.buffer, project)
    return 0


def main(argv: list[str], stdin: TextIO, stdout: TextIO, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd.startswith("hook-"):
        mem = None
        try:
            if cmd not in ("hook-stop", "hook-session-start", "hook-prompt"):
                raise ValueError(f"unknown hook {cmd}")
            payload = json.loads(stdin.read())
            mem = memory_dir(Path(payload["cwd"]))
            _run_hook(cmd, payload, mem, stdout, now)
        except Exception:
            _log_error(mem)
        return 0
    if cmd == "watch":
        return _watch(args)
    if cmd == "remember":
        return _remember(args, stdout, now)
    if cmd == "recall":
        return _recall(args, stdout)
    if cmd == "stats":
        return _stats(stdout)
    if cmd == "map":
        return _map(args, stdout)
    if cmd == "mcp":
        return _mcp(args)
    if cmd == "pack":
        return _pack(args, stdout)
    if cmd == "usage":
        return _usage(args, stdout, now)
    if cmd == "savings":
        return _savings(args, stdout)
    if cmd == "doctor":
        from memgraph.doctor import doctor_main

        return doctor_main(args, stdout)
    if cmd == "setup-mcp":
        from memgraph.setup_mcp import setup_main

        return setup_main(args, stdout)
    stdout.write("Usage: cli (hook-stop | hook-session-start | hook-prompt | remember <text> | recall <term> | stats | map [--no-open] | pack [--size tiny|standard|deep] [--no-visual] | usage [--days N] [--by day|session|model|who] [--json] | savings [--json] | watch --project <dir> | mcp [--project <dir>] | doctor [--project <dir>] [--json] | setup-mcp <claude|vscode|antigravity> [--project <dir>] [--dry-run] [--scope user|project|local])\n")
    return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:], io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"), sys.stdout))
