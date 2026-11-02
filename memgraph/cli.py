"""Command line entry point: Claude Code hooks plus remember/recall/stats/map."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import traceback
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from memgraph import watch
from memgraph.brief import build_brief
from memgraph.graph import Graph, _digest
from memgraph.guard import GuardConfig, decide, last_activity
from memgraph.ingest import ingest_dir, load_state, memory_dir, save_state
from memgraph.pack import SIZES, write_pack
from memgraph.viewer import write_map
from memgraph.visual import IMAGE_FILE, has_story, render_visual_pack

_RECALL_MAX_CHARS = 2000


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


def _ingest(payload: dict, mem: Path) -> tuple[Graph, dict]:
    graph = Graph.load(mem / "graph.json")
    state = load_state(mem / "state.json")
    ingest_dir(Path(os.path.dirname(payload["transcript_path"])), graph, state)
    return graph, state


def _run_hook(name: str, payload: dict, mem: Path, stdout: TextIO, now: datetime) -> None:
    if name == "hook-stop":
        graph, state = _ingest(payload, mem)
        graph.save(mem / "graph.json")
        save_state(mem / "state.json", state)
        watch.arm(mem, Path(payload["transcript_path"]), Path(payload["cwd"]), now)
    elif name == "hook-session-start":
        graph, state = _ingest(payload, mem)
        graph.save(mem / "graph.json")
        save_state(mem / "state.json", state)
        brief = build_brief(graph, project_root=Path(payload["cwd"]))
        if brief:
            _emit(stdout, {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": brief}})
    else:  # hook-prompt
        state = load_state(mem / "guard.json")  # own file: never races ingest's state.json
        last_ts, context = last_activity(Path(payload["transcript_path"]))
        reason = decide(now, last_ts, context, payload.get("prompt") or "", state, GuardConfig.from_env())
        save_state(mem / "guard.json", state)
        if reason:
            _emit(stdout, {"decision": "block", "reason": reason})


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
    mem, graph = _project()
    ts = now.isoformat()
    decision = graph.add_node("decision", _digest(text), text, ts)
    last_prompt = graph.meta.get("last_prompt")
    if isinstance(last_prompt, dict):
        task = graph.meta.get("current_task", {}).get(last_prompt.get("session"))
        if task:
            graph.add_edge(task, "decided", decision, ts)
    graph.save(mem / "graph.json")
    stdout.write("Saved to project memory.\n")
    return 0


def _recall(args: list[str], stdout: TextIO) -> int:
    term = " ".join(args).strip()
    _, graph = _project()
    lines = []
    for node in graph.search(term, 20):
        label = " ".join(str(node.get("label", "")).split())
        lines.append(f"[{node.get('kind')}] {label} (last {str(node.get('last') or '')[:10]})")
    if not lines:
        stdout.write(f'Nothing found for "{term}".\n')
        return 0
    out, used = [], 0
    for line in lines:
        room = _RECALL_MAX_CHARS - used - 1
        if room <= 0:
            break
        out.append(line[:room])
        used += len(out[-1]) + 1
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
    path = write_map(graph, mem, project.name, project)
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
    stdout.write(f"Resume pack: {write_pack(graph, mem, size, project)}\n")
    if "--no-visual" not in args:
        target = mem / IMAGE_FILE
        if not has_story(graph):
            target.unlink(missing_ok=True)  # never leave a stale picture behind
            stdout.write("Visual pack skipped: nothing to draw yet\n")
            return 0
        png = render_visual_pack(graph, target, project)
        stdout.write(f"Visual pack: {png}\n" if png else "Visual pack skipped: install Pillow\n")
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
    if cmd == "pack":
        return _pack(args, stdout)
    stdout.write("Usage: cli (hook-stop | hook-session-start | hook-prompt | remember <text> | recall <term> | stats | map [--no-open] | pack [--size tiny|standard|deep] [--no-visual] | watch --project <dir>)\n")
    return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:], io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"), sys.stdout))
