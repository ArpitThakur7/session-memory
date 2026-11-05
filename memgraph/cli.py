"""Command line entry point: Claude Code hooks plus remember/recall/stats/map."""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from memgraph.store import existing_memory_dir, load_state, memory_dir, memory_path, project_root

if TYPE_CHECKING:
    from memgraph.graph import Graph

# Everything else is imported where it is used: the Read hooks and the status line run on every
# tool call or reply and must not pay for the graph, packs, map or token counter.


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
    from memgraph.graph import Graph, GraphBusy, graph_lock
    from memgraph.ingest import ingest_dir, save_state

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
    if name in ("hook-pre-read", "hook-post-read", "hook-compact"):
        from memgraph import readguard

        if name == "hook-post-read":
            readguard.record(payload, mem, now)
        elif name == "hook-compact":
            readguard.bump_compaction(payload, mem)
        else:
            reason = readguard.check(payload, mem, now)
            event = {"type": "read_denied", "session": payload.get("session_id")}
            if not reason and _may_steer(payload):
                from memgraph import codeindex

                steer = codeindex.big_read_steer(payload, project_root(payload["cwd"]), mem, now)
                if steer:
                    reason, event = steer
            if reason:
                _emit(stdout, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                      "permissionDecisionReason": reason}})
                _log_event(mem, event)
        return
    root = project_root(payload["cwd"])
    if name == "hook-stop":
        from memgraph import savings, watch

        _ingest(payload, mem)
        watch.arm(mem, Path(payload["transcript_path"]), root, now)
        try:  # the status line's "saved" figure; cheap unless new events arrived
            savings.refresh_summary(Path(os.path.dirname(payload["transcript_path"])), mem, watch.ttl_minutes(),
                                    now)
        except Exception:
            _log_error(mem)
    elif name == "hook-session-start":
        from memgraph.brief import build_brief
        from memgraph.ingest import pack_size
        from memgraph.pack import PACK_FILE, write_pack

        graph = _ingest(payload, mem)
        try:  # the brief points at the pack, so it must show what the brief shows
            write_pack(graph, mem, pack_size(mem), root)
        except Exception:
            _log_error(mem)  # the brief still works without a fresh pack; never point at a stale one
            try:
                (mem / PACK_FILE).unlink(missing_ok=True)
            except OSError:
                pass
        brief = build_brief(graph, project_root=root, mem_dir=mem)
        if brief:
            _emit(stdout, {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": brief}})
            if payload.get("source") in ("startup", "clear"):
                _log_event(mem, _fresh_start(graph, payload, brief))
    else:  # hook-prompt
        from memgraph.guard import GuardConfig, decide, last_activity
        from memgraph.ingest import save_state

        state = load_state(mem / "guard.json")  # own file: never races ingest's state.json
        state["transcripts_dir"] = os.path.dirname(payload["transcript_path"])
        last_ts, context = last_activity(Path(payload["transcript_path"]))
        reason = decide(now, last_ts, context, payload.get("prompt") or "", state, GuardConfig.from_env())
        save_state(mem / "guard.json", state)
        if reason:
            _emit(stdout, {"decision": "block", "reason": reason})
            _log_event(mem, {"type": "guard_block", "session": payload.get("session_id"), "context": context})
        else:
            _related_memory(payload, mem, root, stdout)


_CODE_EXTS = frozenset(".py .pyi .js .jsx .mjs .cjs .ts .tsx .mts .cts .go .java .cs .rs .c .h .cc .cpp .cxx "
                       ".hpp .hh .hxx .rb .php .kt .kts .swift".split())


def _may_steer(payload: dict) -> bool:
    """Cheap test before importing the code index: a whole-file Read of a code file, steering on."""
    if os.environ.get("SESSION_MEMORY_BIG_READS", "1").strip() == "0":
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return False
    path = tool_input.get("file_path")
    return isinstance(path, str) and os.path.splitext(path)[1].lower() in _CODE_EXTS


def _related_memory(payload: dict, mem: Path, root: Path, stdout: TextIO) -> None:
    """First prompt of a chat: add the memory that matches it (not for slash commands or notifications)."""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or prompt.startswith("/") or prompt.lstrip().startswith("<"):
        return
    from memgraph import search

    if not search.enabled():
        return
    from memgraph.guard import has_reply

    if has_reply(Path(payload["transcript_path"])):
        return
    from memgraph.graph import Graph

    text, hits = search.related_memory(Graph.load(mem / "graph.json"), prompt, root)
    if text:
        _emit(stdout, {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}})
        _log_event(mem, {"type": "jit_context", "session": payload.get("session_id"), "chars": len(text), "hits": hits})


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
    from memgraph.store import append_event

    try:
        append_event(mem, event)
    except Exception:
        _log_error(mem)


def _emit(stdout: TextIO, obj: dict) -> None:
    stdout.write(json.dumps(obj, ensure_ascii=True))


def _project() -> tuple[Path, Graph]:
    from memgraph.graph import Graph

    mem = memory_dir(Path.cwd())
    return mem, Graph.load(mem / "graph.json")


def _remember(args: list[str], stdout: TextIO, now: datetime) -> int:
    from memgraph.graph import GraphBusy, update
    from memgraph.memory_ops import BUSY, REMEMBER_MAX_CHARS, TOO_LONG, remember

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


_BUDGET_FLAG = re.compile(r"(?:^|\s)--budget(?:=|\s+)?(\S*)")
RECALL_USAGE = "Usage: recall <word> [--budget N]   (N = most tokens to print, default 600)\n"


def _split_budget(text: str) -> tuple[str, int | None]:
    """(text without `--budget N`, N); N is DEFAULT_RECALL_BUDGET when absent and None when invalid."""
    from memgraph.memory_ops import DEFAULT_RECALL_BUDGET

    m = _BUDGET_FLAG.search(text)
    if not m:
        return text.strip(), DEFAULT_RECALL_BUDGET
    value = m.group(1)
    rest = " ".join((text[:m.start()] + " " + text[m.end():]).split())
    if not value.isdigit() or not 1 <= int(value) <= 100_000:
        return rest, None
    return rest, int(value)


def _recall(args: list[str], stdout: TextIO) -> int:
    from memgraph.memory_ops import recall_lines

    # the /recall command passes everything as one quoted word
    term, budget = _split_budget(" ".join(args))
    if not term or budget is None:
        stdout.write(RECALL_USAGE)
        return 2
    _, graph = _project()
    out = recall_lines(graph, term, Path.cwd(), budget)
    if not out:
        stdout.write(f'Nothing found for "{term}".\n')
        return 0
    stdout.write("\n".join(out) + "\n")
    return 0


def _explain(args: list[str], stdout: TextIO) -> int:
    from memgraph.explore import explain_lines

    thing, budget = _split_budget(" ".join(args))
    if not thing or budget is None:
        stdout.write("Usage: explain <thing> [--budget N]\n")
        return 2
    _, graph = _project()
    out = explain_lines(graph, thing, Path.cwd(), budget)
    stdout.write(("\n".join(out) if out else f'Nothing found for "{thing}".') + "\n")
    return 0


def _path(args: list[str], stdout: TextIO) -> int:
    from memgraph.explore import path_lines

    rest, budget = _split_budget(" ".join(f'"{a}"' if " " in a else a for a in args))
    words = [w.strip('"') for w in _QUOTED_WORDS.findall(rest)]
    if len(words) != 2 or budget is None:
        stdout.write('Usage: path <a> <b> [--budget N]   (quote a name with spaces: path "login form" NameError)\n')
        return 2
    _, graph = _project()
    stdout.write("\n".join(path_lines(graph, words[0], words[1], Path.cwd(), budget)) + "\n")
    return 0


_QUOTED_WORDS = re.compile(r'"[^"]+"|\S+')


def _stats(stdout: TextIO) -> int:
    from memgraph.brief import build_brief

    _, graph = _project()
    sessions = graph.meta.get("sessions", {})
    stdout.write(
        f"nodes={len(graph.nodes)} edges={len(graph.edges)} "
        f"sessions={len(sessions)} brief_chars={len(build_brief(graph, project_root=Path.cwd()))}\n"
    )
    return 0


def _map(args: list[str], stdout: TextIO) -> int:
    import webbrowser

    from memgraph import savings
    from memgraph.usage import transcripts_dir_for
    from memgraph.viewer import write_map

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
    from memgraph.ingest import record_pack_size
    from memgraph.pack import SIZES, write_pack
    from memgraph.visual import IMAGE_FILE, has_story, render_visual_pack

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


_USAGE_HELP = "Usage: usage [--days N] [--by day|session|model|who|helper] [--json] [--transcripts PATH]\n"
_SAVINGS_HELP = "Usage: savings [--json] [--transcripts PATH]\n"
NO_TRANSCRIPTS = "No Claude Code transcripts found at {path}. Pass --transcripts PATH.\n"


def _opt(args: list[str], name: str) -> str | None:
    """Value after `name`, "" when it is the last word, None when absent."""
    if name not in args:
        return None
    i = args.index(name)
    return args[i + 1] if i + 1 < len(args) else ""


def _transcripts(args: list[str]) -> tuple[Path, Path]:
    from memgraph.usage import transcripts_dir_for

    mem = memory_path(Path.cwd())  # read-only lookups: never create a memory folder here
    given = _opt(args, "--transcripts")
    return (Path(given).expanduser() if given else transcripts_dir_for(Path.cwd(), mem)), mem


def _usage(args: list[str], stdout: TextIO, now: datetime) -> int:
    from memgraph.usage import (GROUPINGS, PRICES_AS_OF, collect_usage, helper_rows, helper_table, summarize, totals,
                                totals_line, usage_table)

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
    if by == "helper":
        recent = [r for r in recent if r.helper]
        rows = helper_rows(recent, tdir)
    else:
        rows = summarize(recent, by)
    total = totals(recent)
    if "--json" in args:
        stdout.write(json.dumps({"by": by, "days": days, "since": since.isoformat(), "rows": rows, "totals": total,
                                 "duplicates": result.duplicates, "prices_as_of": PRICES_AS_OF}, indent=1) + "\n")
        return 0
    stdout.write(f"Token usage, last {days} days, by {by} (exact, from transcripts):\n")
    stdout.write((helper_table(rows) if by == "helper" else usage_table(rows, by)) + "\n\n" + totals_line(total, result.duplicates) + "\n")
    return 0


def _savings(args: list[str], stdout: TextIO) -> int:
    from memgraph import savings, watch

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
        from memgraph import watch

        i = args.index("--project") if "--project" in args else -1
        project = Path(args[i + 1]) if 0 <= i < len(args) - 1 else Path.cwd()
        mem = memory_dir(project)
        watch.run_watcher(mem, project)
    except Exception:
        _log_error(mem)
    return 0


def _code(cmd: str, args: list[str], stdout: TextIO) -> int:
    from memgraph import codeindex

    root = project_root(Path.cwd())
    mem = existing_memory_dir(root)  # the cache is used when there is one; never created by a lookup
    limit = _opt(args, "--limit")
    words = [a for i, a in enumerate(args) if a != "--limit" and (i == 0 or args[i - 1] != "--limit")]
    if not words:
        usage = "outline <path>" if cmd == "outline" else "find-symbol <name> [--limit N]"
        stdout.write(f"Usage: {usage}\n")
        return 2
    try:
        if cmd == "outline":
            text = codeindex.outline(root, mem, " ".join(words))
        else:
            n = int(limit) if limit and limit.isdigit() else 20
            text = codeindex.find(root, mem, " ".join(words), limit=n)
    except ValueError as e:
        stdout.write(f"{e}\n")
        return 2
    stdout.write(text + "\n")
    return 0


def _mcp(args: list[str]) -> int:
    from memgraph.mcp_server import serve

    i = args.index("--project") if "--project" in args else -1
    project = Path(args[i + 1]) if 0 <= i < len(args) - 1 else Path.cwd()
    serve(sys.stdin.buffer, sys.stdout.buffer, project)
    return 0


HOOKS = ("hook-stop", "hook-session-start", "hook-prompt", "hook-pre-read", "hook-post-read", "hook-compact")


def main(argv: list[str], stdin: TextIO, stdout: TextIO, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd.startswith("hook-"):
        mem = None
        try:
            if cmd not in HOOKS:
                raise ValueError(f"unknown hook {cmd}")
            payload = json.loads(stdin.read())
            if not isinstance(payload, dict):
                raise ValueError("hook input is not a JSON object")
            mem = memory_dir(project_root(payload["cwd"]))  # one folder per project, even from a subfolder
            _run_hook(cmd, payload, mem, stdout, now)
        except Exception:
            _log_error(mem)
        return 0
    if cmd == "statusline":
        from memgraph.statusline import statusline_main

        return statusline_main(stdin, stdout, now)
    if cmd == "watch":
        return _watch(args)
    if cmd == "remember":
        return _remember(args, stdout, now)
    if cmd == "recall":
        return _recall(args, stdout)
    if cmd == "explain":
        return _explain(args, stdout)
    if cmd == "path":
        return _path(args, stdout)
    if cmd == "stats":
        return _stats(stdout)
    if cmd == "map":
        return _map(args, stdout)
    if cmd == "mcp":
        return _mcp(args)
    if cmd in ("outline", "find-symbol"):
        return _code(cmd, args, stdout)
    if cmd == "pack":
        return _pack(args, stdout)
    if cmd == "usage":
        return _usage(args, stdout, now)
    if cmd == "savings":
        return _savings(args, stdout)
    if cmd == "audit":
        from memgraph.audit import audit_main

        return audit_main(args, stdout, now)
    if cmd == "doctor":
        from memgraph.doctor import doctor_main

        return doctor_main(args, stdout)
    if cmd == "setup-mcp":
        from memgraph.setup_mcp import setup_main

        return setup_main(args, stdout)
    if cmd == "setup-statusline":
        from memgraph.statusline import setup_statusline_main

        return setup_statusline_main(args, stdout)
    stdout.write("Usage: cli (hook-stop | hook-session-start | hook-prompt | hook-pre-read | hook-post-read | hook-compact | statusline | setup-statusline [--dry-run] [--force] | remember <text> | recall <word> [--budget N] | explain <thing> [--budget N] | path <a> <b> [--budget N] | stats | map [--no-open] | pack [--size tiny|standard|deep] [--no-visual] | usage [--days N] [--by day|session|model|who|helper] [--json] | savings [--json] | audit [--project <dir>] [--json] | watch --project <dir> | mcp [--project <dir>] | outline <path> | find-symbol <name> [--limit N] | doctor [--project <dir>] [--json] | setup-mcp <claude|vscode|antigravity> [--project <dir>] [--dry-run] [--scope user|project|local])\n")
    return 2


if __name__ == "__main__":
    if sys.version_info < (3, 10):  # the .cmd launcher runs `py -3` directly; an old Python exits quietly
        sys.exit(0)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:], io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"), sys.stdout))
