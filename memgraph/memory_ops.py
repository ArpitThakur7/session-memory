"""Shared recall/remember operations used by the CLI and the MCP server."""
from __future__ import annotations

from datetime import datetime

from memgraph.graph import Graph, _digest
from memgraph.redact import redact

RECALL_MAX_CHARS = 2000
REMEMBER_MAX_CHARS = 1000
TOO_LONG = "Text too long ({n:,} characters); the limit is 1,000. Save a shorter note."
BUSY = "Not saved: project memory is busy (another update is running). Try again."


def recall_lines(graph: Graph, query: str) -> list[str]:
    """Search lines `[kind] label (last YYYY-MM-DD)`, clipped to RECALL_MAX_CHARS in total."""
    lines = []
    for node in graph.search(query, 20):
        label = " ".join(str(node.get("label", "")).split())
        lines.append(f"[{node.get('kind')}] {label} (last {str(node.get('last') or '')[:10]})")
    out, used = [], 0
    for line in lines:
        room = RECALL_MAX_CHARS - used - 1
        if room <= 0:
            break
        out.append(line[:room])
        used += len(out[-1]) + 1
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
