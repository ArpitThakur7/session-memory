"""`explain <thing>` and `path <a> <b>`: one node with its links, or the shortest chain between two.

Stdlib only, 0 model tokens. Labels come from the (already redacted) graph; absolute paths
outside the project are hidden. Session nodes link to nearly everything, so they are listed
last and a path goes through one only when no other chain exists. Idea from graphify
(Apache-2.0/MIT); no code copied.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

from memgraph.graph import EXTRACTED, edge_conf
from memgraph.memory_ops import DEFAULT_RECALL_BUDGET, clip_to_budget
from memgraph.search import RECALL_KIND_WEIGHTS, _scrub, recall

_LABEL = 120
_IN_NAMES = {"asked": "asked by", "edited": "edited by", "read": "read by", "ran": "run by",
             "decided": "decided in", "mentions": "mentioned by"}
HUB_KINDS = frozenset({"session"})


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _LABEL else text[:_LABEL - 3] + "..."


def _label(graph, node: dict, root: Path | None) -> str:
    if node.get("kind") == "session":
        sid = str(node.get("label") or "")
        title = ((graph.meta.get("sessions") or {}).get(sid) or {}).get("title")
        return _short(title) if isinstance(title, str) and title.strip() else f"chat {sid[:8]}"
    label = str(node.get("label") or "")
    shown = _scrub(label, root) if root is not None else " ".join(label.split())
    return _short(shown or label)


def _tagged(graph, node: dict, root: Path | None) -> str:
    return f"[{node.get('kind')}] {_label(graph, node, root)}"


def resolve(graph, thing: str, root: Path | None = None) -> dict | None:
    """The node `thing` names best: an exact label, then a label ending in it as a path part,
    then the label it covers most of, each tie broken by /recall's ranking."""
    needle = " ".join(str(thing).split()).casefold()
    if not needle:
        return None
    exact = graph.nodes.get(str(thing).strip())
    if isinstance(exact, dict):
        return exact
    best, best_key = None, None
    for rank, e in enumerate(recall(graph, thing, root)):
        label = " ".join(str(e["node"].get("label") or "").split()).casefold()
        shown = e["shown"].casefold()
        if needle in (label, shown):
            tier = 0
        elif label.endswith("/" + needle) or shown.endswith("/" + needle):
            tier = 1
        elif needle in shown:
            tier = 2 if len(needle) * 2 >= len(shown) else 3
        else:
            tier = 4
        key = (tier, rank)
        if best_key is None or key < best_key:
            best, best_key = e["node"], key
    return best


def _neighbours(graph, nid: str) -> list[tuple[dict, str, bool, dict]]:
    """(edge, other id, outgoing?, other node) for every link of `nid`."""
    out = []
    for e in graph.edges.values():
        if not isinstance(e, dict):
            continue
        if e.get("src") == nid and e.get("dst") in graph.nodes:
            out.append((e, e["dst"], True, graph.nodes[e["dst"]]))
        elif e.get("dst") == nid and e.get("src") in graph.nodes:
            out.append((e, e["src"], False, graph.nodes[e["src"]]))
    return out


def explain_lines(graph, thing: str, root: Path | None = None, budget: int = DEFAULT_RECALL_BUDGET) -> list[str]:
    """The node, then its links (extracted first, sessions last), within `budget` tokens; [] when nothing matches."""
    node = resolve(graph, thing, root)
    if node is None:
        return []
    first, last = str(node.get("first") or "")[:10], str(node.get("last") or "")[:10]
    seen = node.get("count") if isinstance(node.get("count"), int) else 1
    when = f"first {first}, last {last}" if first and first != last else f"last {last}" if last else ""
    head = f"{_tagged(graph, node, root)} ({', '.join(x for x in (when, f'seen {seen}×') if x)})"
    links = []
    for e, _other, outgoing, other in _neighbours(graph, node.get("id")):
        rel = str(e.get("rel") or "")
        conf = edge_conf(e)
        text = (f"- {rel} → " if outgoing else f"- {_IN_NAMES.get(rel, rel)} ← ") + f"{_tagged(graph, other, root)} ({conf})"
        key = (other.get("kind") in HUB_KINDS, conf != EXTRACTED, -RECALL_KIND_WEIGHTS.get(other.get("kind"), 0),
               -(e.get("count") or 1), _desc(e.get("last")), text)
        links.append((key, text))
    links.sort()
    lines = [head] + ([f"Links ({len(links)}):"] + [t for _k, t in links] if links else ["No links."])
    return clip_to_budget(lines, budget, "narrow the word")


def _desc(text) -> tuple:
    return tuple(-ord(c) for c in str(text or "")) + (1,)


def _bfs(graph, start: str, goal: str, through_hubs: bool) -> list[tuple[str, dict, bool]] | None:
    """Shortest chain as [(node id, edge into it, edge points forward?)]; None when unlinked."""
    adj: dict[str, list[tuple[str, dict, bool]]] = {}
    for e in graph.edges.values():
        if not isinstance(e, dict):
            continue
        s, d = e.get("src"), e.get("dst")
        if s in graph.nodes and d in graph.nodes and s != d:
            adj.setdefault(s, []).append((d, e, True))
            adj.setdefault(d, []).append((s, e, False))
    prev: dict[str, tuple[str, dict, bool] | None] = {start: None}
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        if cur == goal:
            break
        if cur != start and not through_hubs and graph.nodes[cur].get("kind") in HUB_KINDS:
            continue  # reachable, but never a stepping stone
        for nxt, e, fwd in sorted(adj.get(cur, ()), key=lambda t: (edge_conf(t[1]) != EXTRACTED, t[0])):
            if nxt not in prev:
                prev[nxt] = (cur, e, fwd)
                queue.append(nxt)
    if goal not in prev:
        return None
    chain, cur = [], goal
    while prev[cur] is not None:
        before, e, fwd = prev[cur]
        chain.append((cur, e, fwd))
        cur = before
    return list(reversed(chain))


def path_lines(graph, a: str, b: str, root: Path | None = None, budget: int = DEFAULT_RECALL_BUDGET) -> list[str]:
    """Shortest hop chain between what `a` and `b` name, each hop with its relation and tag."""
    na = resolve(graph, a, root)
    if na is None:
        return [f'Nothing found for "{a}".']
    nb = resolve(graph, b, root)
    if nb is None:
        return [f'Nothing found for "{b}".']
    start, goal = na["id"], nb["id"]
    if start == goal:
        return [f"Both name the same note: {_tagged(graph, na, root)}"]
    chain, via_hub = _bfs(graph, start, goal, through_hubs=False), False
    if chain is None:
        chain, via_hub = _bfs(graph, start, goal, through_hubs=True), True
    if chain is None:
        return [f'No link between "{_label(graph, na, root)}" and "{_label(graph, nb, root)}".']
    hops = len(chain)
    note = ", via a session: these share no closer link" if via_hub else ""
    lines = [f"Path ({hops} hop{'s' if hops != 1 else ''}{note}):", _tagged(graph, na, root)]
    for nid, e, fwd in chain:
        rel = str(e.get("rel") or "")
        lines.append(f"  —{rel}→ ({edge_conf(e)})" if fwd else f"  ←{rel}— ({edge_conf(e)})")
        lines.append(_tagged(graph, graph.nodes[nid], root))
    return clip_to_budget(lines, budget, "the path is longer than the budget")
