"""Compare the size of each old chat with the resume packs that could replace it.

Usage: python bench/compare_packs.py <transcripts_dir> [--questions]

Read-only. Tokens are estimated as characters // 4 for the text packs; the
history size is the real context Claude Code recorded on the newest reply.
The picture costs 1092 x 1092 / 750 = 1,590 tokens when it can be drawn
(needs Pillow), otherwise "-".

--questions prints a 10-question checklist built from the memory graph of all
transcripts in the folder, with the expected answers, so you can ask a fresh
chat the same questions with each pack and score it by hand. The answers come
from your own transcripts: keep that output on your machine.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

if __name__ == "__main__":  # allow `python bench/compare_packs.py` from any folder
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.measure import _read_transcript
from memgraph.brief import _newest
from memgraph.graph import Graph
from memgraph.pack import SIZES, build_pack
from memgraph.transcript import parse_line
from memgraph.visual import IMAGE_SIZE, has_story, render_visual_pack

VISUAL_TOKENS = round(IMAGE_SIZE[0] * IMAGE_SIZE[1] / 750)  # 1,590


def _files(transcripts_dir: Path) -> list[Path]:
    try:
        return sorted(p for p in Path(transcripts_dir).glob("*.jsonl") if p.is_file())
    except OSError:
        return []


def _visual_tokens(graph: Graph) -> int | None:
    if not has_story(graph):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        return VISUAL_TOKENS if render_visual_pack(graph, Path(tmp) / "memory.png") else None


def compare(transcripts_dir: Path) -> list[dict]:
    """One row per transcript with usage data, largest history first."""
    rows = []
    for path in _files(transcripts_dir):
        try:
            graph, history = _read_transcript(path)
        except OSError:
            continue
        if history is None:
            continue
        row = {"session": path.name, "history_tokens": history}
        for size in SIZES:
            row[size] = len(build_pack(graph, size)) // 4
        row["visual"] = _visual_tokens(graph)
        rows.append(row)
    rows.sort(key=lambda r: r["history_tokens"], reverse=True)
    return rows


def format_table(rows: list[dict]) -> str:
    lines = [
        "| session | history tokens | tiny | standard | deep | visual |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        visual = "-" if r["visual"] is None else r["visual"]
        lines.append(f"| {r['session']} | {r['history_tokens']} | {r['tiny']} | {r['standard']} "
                     f"| {r['deep']} | {visual} |")
    return "\n".join(lines)


def _graph_of(transcripts_dir: Path) -> Graph:
    graph = Graph()
    for path in _files(transcripts_dir):
        try:
            data = path.read_bytes()
        except OSError:
            continue
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
    return graph


def questions(transcripts_dir: Path) -> list[str]:
    """Ten "question -> expected answer" lines generated from the graph."""
    g = _graph_of(transcripts_dir)
    by_kind = lambda k: [n for n in g.nodes.values() if n.get("kind") == k]  # noqa: E731
    none = "(none recorded)"

    def label(nodes) -> str:
        return " ".join(str(nodes[0].get("label", "")).split()) if nodes else none

    prompt = g.meta.get("last_prompt") or {}
    recent = [r for r in g.meta.get("recent_prompts") or [] if isinstance(r, dict)]
    edited = {e.get("dst") for e in g.edges.values() if e.get("rel") == "edited"}
    changed = _newest([n for n in by_kind("file") if n.get("id") in edited])
    top_file = max(changed, key=lambda n: n.get("count", 0)) if changed else None
    todos = (g.meta.get("last_todos") or {}).get("items") or []
    open_todos = [t for t in todos if isinstance(t, dict) and t.get("status") != "completed"]
    sessions = g.meta.get("sessions") or {}
    items = [
        ("What was my last request?", " ".join(str(prompt.get("text", "")).split()) or none),
        ("Which file did I change most often?", label([top_file] if top_file else [])),
        ("Which file did I change most recently?", label(changed)),
        ("What is the newest decision we made?", label(_newest(by_kind("decision")))),
        ("What is the newest error we hit?", label(_newest(by_kind("error")))),
        ("What was the newest command that ran?", label(_newest(by_kind("command")))),
        ("How many sessions are in this project?", str(len(sessions))),
        ("What is the first open next step?",
         " ".join(str(open_todos[0].get("content", "")).split()) if open_todos else none),
        ("How many next steps are still open?", str(len(open_todos))),
        ("What did I ask before my last request?",
         " ".join(str(recent[1].get("text", "")).split()) if len(recent) > 1 else none),
    ]
    return [f"{i}. {q} -> {a}" for i, (q, a) in enumerate(items, 1)]


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--questions"]
    if len(args) != 1:
        print("usage: python bench/compare_packs.py <transcripts_dir> [--questions]", file=sys.stderr)
        return 2
    folder = Path(args[0]).expanduser()
    rows = compare(folder)
    print(format_table(rows) if rows else "No transcripts with usage data found.")
    if "--questions" in argv[1:]:
        print("\nQuestions (expected answers come from your graph; keep this output local):")
        print("\n".join(questions(folder)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
