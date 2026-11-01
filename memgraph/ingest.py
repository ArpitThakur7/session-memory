"""Incremental ingest of transcript JSONL files into the memory graph."""
from __future__ import annotations

import json
from pathlib import Path

from memgraph.graph import Graph, write_json_atomic
from memgraph.transcript import parse_line


def memory_dir(cwd: Path) -> Path:
    """The per-project memory directory, created if missing."""
    d = Path(cwd) / ".claude" / "session-memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state(path: Path) -> dict:
    """Load ingest state; a missing or corrupt file yields {}."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict) -> None:
    write_json_atomic(path, state)


def ingest_dir(transcripts_dir: Path, graph: Graph, state: dict) -> int:
    """Apply events from new complete lines of every *.jsonl in `transcripts_dir`.

    Per-file byte offsets live in state["offsets"][filename]. Only bytes up to the
    last newline are consumed, so a line still being written is picked up later.
    Returns the number of events applied.
    """
    offsets = state.setdefault("offsets", {})
    applied = 0
    for path in _oldest_first(transcripts_dir):
        try:
            with open(path, "rb") as fh:
                size = fh.seek(0, 2)
                offset = offsets.get(path.name, 0)
                if not isinstance(offset, int) or offset < 0 or offset > size:
                    offset = 0  # rewritten or truncated file: start over
                fh.seek(offset)
                chunk = fh.read()
        except OSError:
            continue
        end = chunk.rfind(b"\n") + 1
        for raw in chunk[:end].splitlines():
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                events = parse_line(line)
            except Exception:
                continue  # an odd line must not stall this file forever
            for event in events:
                try:
                    graph.apply(event)
                except Exception:
                    continue  # nor may one odd event
                applied += 1
        offsets[path.name] = offset + end
    return applied


def _oldest_first(transcripts_dir: Path) -> list[Path]:
    """Top-level *.jsonl files ordered by modification time, oldest first (name breaks ties).

    Newer sessions are then applied last, so "latest" values in the graph come
    from the session the user worked in most recently.
    """
    keyed = []
    try:
        for p in Path(transcripts_dir).glob("*.jsonl"):
            try:
                if p.is_file():
                    keyed.append((p.stat().st_mtime, p.name, p))
            except OSError:
                continue
    except OSError:
        return []
    return [p for _mtime, _name, p in sorted(keyed)]
