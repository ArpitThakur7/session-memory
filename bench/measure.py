"""Compare the size of each old chat with the size of the brief that would replace it.

Usage: python bench/measure.py <transcripts_dir>

Read-only: transcripts are never modified and nothing is written to the project.
Tokens are estimated as characters // 4 for the brief; the history size is the
real context size Claude Code recorded on the newest reply.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":  # allow `python bench/measure.py` from any folder
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memgraph.brief import build_brief
from memgraph.graph import Graph
from memgraph.transcript import parse_line


def _read_transcript(path: Path) -> tuple[Graph, int | None]:
    """Graph of one transcript, plus its newest usage context (None if it has none)."""
    graph = Graph()
    context: int | None = None
    with open(path, "rb") as fh:
        data = fh.read()
    for raw in data[: data.rfind(b"\n") + 1].splitlines():
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            events = parse_line(line)
        except Exception:
            continue
        for event in events:
            graph.apply(event)
            if event.kind == "usage" and isinstance(event.data.get("context"), int):
                context = event.data["context"]
    return graph, context


def measure(transcripts_dir: Path) -> list[dict]:
    """One row per transcript that has usage data, largest history first."""
    rows = []
    try:
        files = sorted(p for p in Path(transcripts_dir).glob("*.jsonl") if p.is_file())
    except OSError:
        return []
    for path in files:
        try:
            graph, history = _read_transcript(path)
        except OSError:
            continue
        if not history:
            continue
        brief = len(build_brief(graph)) // 4
        rows.append({
            "session": path.name,
            "history_tokens": history,
            "brief_tokens": brief,
            "saved_pct": round(100 * (1 - brief / max(history, 1)), 1),
        })
    rows.sort(key=lambda r: r["history_tokens"], reverse=True)
    return rows


def format_table(rows: list[dict]) -> str:
    lines = [
        "| session | history tokens | brief tokens | saved % |",
        "|---|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(f"| {r['session']} | {r['history_tokens']} | {r['brief_tokens']} | {r['saved_pct']} |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python bench/measure.py <transcripts_dir>", file=sys.stderr)
        return 2
    rows = measure(Path(argv[1]).expanduser())
    if not rows:
        print("No transcripts with usage data found.")
        return 0
    print(format_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
