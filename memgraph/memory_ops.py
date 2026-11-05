"""Shared recall/remember operations used by the CLI and the MCP server."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from memgraph.graph import EXTRACTED, INFERRED, Graph, _digest, node_confs
from memgraph.redact import redact
from memgraph.search import recall

DEFAULT_RECALL_BUDGET = 600  # tokens; about 2,400 characters
CHARS_PER_TOKEN = 4
# A node seen in a transcript turn outranks one linked only by a guess; one with no links sits between.
CONF_WEIGHTS = {EXTRACTED: 1.0, INFERRED: 0.8}
REMEMBER_MAX_CHARS = 1000
TOO_LONG = "Text too long ({n:,} characters); the limit is 1,000. Save a shorter note."
BUSY = "Not saved: project memory is busy (another update is running). Try again."


def recall_lines(graph: Graph, query: str, root: Path | None = None, budget: int = DEFAULT_RECALL_BUDGET) -> list[str]:
    """Ranked lines `[kind] label (last YYYY-MM-DD, extracted|inferred)`, at most `budget` tokens in total."""
    confs = node_confs(graph)
    lines = []
    for e in recall(graph, query, root, weight=lambda n: CONF_WEIGHTS.get(confs.get(n.get("id")), 0.9)):
        node = e["node"]
        similar = f" (+{e['similar']} similar)" if e["similar"] else ""
        tag = confs.get(node.get("id"))
        tag = f", {tag}" if tag else ""
        lines.append(f"[{node.get('kind')}] {e['shown']}{similar} (last {str(node.get('last') or '')[:10]}{tag})")
    return clip_to_budget(lines, budget, "narrow the word")


def clip_to_budget(lines: list[str], budget: int = DEFAULT_RECALL_BUDGET, hint: str = "narrow the word") -> list[str]:
    """Whole lines up to `budget` tokens (about 4 characters each), then `… N more — <hint>`.

    The note's room is kept free whenever lines are left out; a first line longer than the
    whole budget is clipped rather than dropped, so something is always shown.
    """
    cap = max(1, int(budget)) * CHARS_PER_TOKEN
    out, used = [], 0
    for i, line in enumerate(lines):
        left = len(lines) - i - 1
        note = len(f"\n… {left} more — {hint}") if left else 0
        if used + (1 if out else 0) + len(line) + note <= cap:
            used += (1 if out else 0) + len(line)
            out.append(line)
            continue
        if not out:  # nothing fits: show a clipped first line and the count when there is room
            more = f"… {len(lines) - 1} more — {hint}"
            room = cap - (len(more) + 1 if len(lines) > 1 else 0)
            if room >= 20:
                return [line[:room], more] if len(lines) > 1 else [line[:room]]
            return [line[:cap]]
        out.append(f"… {len(lines) - len(out)} more — {hint}")
        return out
    return out


def remember(graph: Graph, text: str, now: datetime) -> None:
    """Add a decision node (linked to the current task if known). Caller saves the graph."""
    ts = now.isoformat()
    text = redact(text)
    decision = graph.add_node("decision", _digest(text), text, ts)
    last_prompt = graph.meta.get("last_prompt")
    if isinstance(last_prompt, dict):
        task = graph.meta.get("current_task", {}).get(last_prompt.get("session"))
        if task:
            graph.add_edge(task, "decided", decision, ts)
