"""Incremental ingest of transcript JSONL files into the memory graph."""
from __future__ import annotations

from pathlib import Path

from memgraph.graph import Graph, write_json_atomic
from memgraph.store import load_state, memory_dir  # noqa: F401  (re-exported)
from memgraph.transcript import SIDECHAIN_KINDS, parse_line


def save_state(path: Path, state: dict) -> None:
    write_json_atomic(path, state)


def pack_size(mem_dir: Path) -> str:
    """Size of the last resume pack built (state.json "pack_size"); "standard" if unknown."""
    from memgraph.pack import SIZES

    size = load_state(Path(mem_dir) / "state.json").get("pack_size")
    return size if isinstance(size, str) and size in SIZES else "standard"


def record_pack_size(mem_dir: Path, size: str) -> None:
    """Remember the size just built, under graph.lock (state.json is shared with ingest)."""
    from memgraph.graph import GraphBusy, graph_lock

    path = Path(mem_dir) / "state.json"
    try:
        with graph_lock(Path(mem_dir) / "graph.json"):
            state = load_state(path)
            if state.get("pack_size") != size:
                state["pack_size"] = size
                save_state(path, state)
    except GraphBusy:
        pass


def ingest_dir(transcripts_dir: Path, graph: Graph, state: dict) -> int:
    """Apply events from new complete lines of every *.jsonl in `transcripts_dir`,
    then of helper-agent transcripts at `<session-id>/subagents/agent-*.jsonl`.

    Per-file byte offsets live in state["offsets"][relative path] (a top-level
    file's name, or "<session-id>/subagents/agent-x.jsonl"). Only bytes up to the
    last newline are consumed, so a line still being written is picked up later.
    A helper agent's lines carry its parent's sessionId; only their edit, read,
    command and error events are applied, so they land on the parent's current task.
    Returns the number of events applied.
    """
    offsets = state.setdefault("offsets", {})
    root = Path(transcripts_dir)
    applied = 0
    files = [(p, False) for p in _oldest_first(root)]
    files += [(p, True) for p in _oldest_first(root, "*/subagents/agent-*.jsonl")]
    for path, sidechain in files:
        key = path.relative_to(root).as_posix()
        read = _new_complete_lines(path, offsets.get(key, 0))
        if read is None:
            continue
        lines, offsets[key] = read
        for line in lines:
            try:
                events = parse_line(line)
            except Exception:
                continue  # an odd line must not stall this file forever
            for event in events:
                if sidechain and event.kind not in SIDECHAIN_KINDS:
                    continue
                try:
                    graph.apply(event)
                except Exception:
                    continue  # nor may one odd event
                applied += 1
    return applied


def _new_complete_lines(path: Path, offset) -> tuple[list[str], int] | None:
    """(non-empty lines written since `offset`, new offset); None when the file can't be read.

    Stops at the last newline, so a line still being written is left for next time.
    An offset past the end (the file was rewritten or truncated) starts over at 0."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, 2)
            if not isinstance(offset, int) or offset < 0 or offset > size:
                offset = 0
            fh.seek(offset)
            chunk = fh.read()
    except OSError:
        return None
    end = chunk.rfind(b"\n") + 1
    lines = [raw.decode("utf-8", errors="replace").strip() for raw in chunk[:end].splitlines()]
    return [line for line in lines if line], offset + end


def _oldest_first(transcripts_dir: Path, pattern: str = "*.jsonl") -> list[Path]:
    """Files matching `pattern` (default: top-level *.jsonl) ordered by modification time, oldest first (name breaks ties).

    Newer sessions are then applied last, so "latest" values in the graph come
    from the session the user worked in most recently.
    """
    keyed = []
    try:
        for p in Path(transcripts_dir).glob(pattern):
            try:
                if p.is_file():
                    keyed.append((p.stat().st_mtime, p.as_posix(), p))
            except OSError:
                continue
    except OSError:
        return []
    return [p for _mtime, _name, p in sorted(keyed)]
