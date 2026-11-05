"""Shared recall/remember operations used by the CLI and the MCP server."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from memgraph.graph import EXTRACTED, INFERRED, Graph, _digest, node_confs
from memgraph.redact import redact
from memgraph.search import recall

RECALL_MAX_CHARS = 2000
# A node seen in a transcript turn outranks one linked only by a guess; one with no links sits between.
CONF_WEIGHTS = {EXTRACTED: 1.0, INFERRED: 0.8}
REMEMBER_MAX_CHARS = 1000
TOO_LONG = "Text too long ({n:,} characters); the limit is 1,000. Save a shorter note."
BUSY = "Not saved: project memory is busy (another update is running). Try again."


def recall_lines(graph: Graph, query: str, root: Path | None = None) -> list[str]:
    """Ranked lines `[kind] label (last YYYY-MM-DD, extracted|inferred)`, clipped to RECALL_MAX_CHARS in total."""
    confs = node_confs(graph)
    lines = []
    for e in recall(graph, query, root, weight=lambda n: CONF_WEIGHTS.get(confs.get(n.get("id")), 0.9)):
        node = e["node"]
        similar = f" (+{e['similar']} similar)" if e["similar"] else ""
        tag = confs.get(node.get("id"))
        tag = f", {tag}" if tag else ""
        lines.append(f"[{node.get('kind')}] {e['shown']}{similar} (last {str(node.get('last') or '')[:10]}{tag})")
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
