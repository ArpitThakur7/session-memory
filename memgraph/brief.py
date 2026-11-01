"""Build the short startup brief that replaces re-sending an old conversation."""
from __future__ import annotations

from datetime import datetime

from memgraph.graph import Graph

TITLE = "# Project memory (auto-loaded by session-memory)"
FOOTER = "For older details run /recall <word>. Do not re-read files listed here unless needed."

_MAX_TODOS = 10
_MAX_DECISIONS = 10
_MAX_FILES = 15
_MAX_ERRORS = 5


def _one_line(text) -> str:
    return " ".join(str(text).split())


def _fmt_ts(ts) -> str:
    ts = str(ts or "")
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def _newest(nodes, limit: int) -> list[dict]:
    """Newest first by `last`; ties broken by label so output is deterministic."""
    return sorted(nodes, key=lambda n: (n.get("last") or "", str(n.get("label", ""))),
                  reverse=True)[:limit]


def _request_line(last_prompt: dict, text: str) -> str:
    stamp = _fmt_ts(last_prompt.get("ts"))
    return f"Last request ({stamp}): {text}" if stamp else f"Last request: {text}"


def build_brief(graph: Graph, budget_chars: int = 12000) -> str:
    """Return a deterministic markdown brief of at most `budget_chars` characters.

    Returns "" for an empty graph. When over budget, whole lines are dropped from
    the bottom of the lowest-priority section first (errors, files, decisions,
    next steps); the title, last request and footer are always kept, with the
    last-request text cut if even those do not fit.
    """
    nodes = list(graph.nodes.values())
    by_kind = lambda kind: [n for n in nodes if n.get("kind") == kind]  # noqa: E731
    last_prompt = graph.meta.get("last_prompt")
    if not last_prompt and not any(by_kind(k) for k in ("decision", "file", "error")):
        return ""

    # Section bodies, highest priority first: (heading, item lines).
    todos = (graph.meta.get("last_todos") or {}).get("items") or []
    next_steps = [f"- [{_one_line(t.get('status', ''))}] {_one_line(t.get('content', ''))}"
                  for t in todos if t.get("status") != "completed"][:_MAX_TODOS]
    decisions = [f"- {_one_line(n.get('label', ''))}"
                 for n in _newest(by_kind("decision"), _MAX_DECISIONS)]
    edited = {e.get("dst") for e in graph.edges.values() if e.get("rel") == "edited"}
    files = [f"- {_one_line(n.get('label', ''))}"
             for n in _newest([n for n in by_kind("file") if n.get("id") in edited], _MAX_FILES)]
    errors = [f"- {_one_line(n.get('label', ''))}"
              for n in _newest(by_kind("error"), _MAX_ERRORS)]
    sections = [
        ("## Next steps", next_steps),
        ("## Decisions", decisions),
        ("## Recently changed files", files),
        ("## Recent errors", errors),
    ]

    request_text = _one_line(last_prompt.get("text", "")) if last_prompt else None

    def render() -> str:
        lines = [TITLE]
        if request_text is not None:
            lines.append(_request_line(last_prompt, request_text))
        for heading, items in sections:
            if items:
                lines += ["", heading, *items]
        lines += ["", FOOTER]
        return "\n".join(lines)

    out = render()
    for _heading, items in reversed(sections):  # lowest priority first
        while items and len(out) > budget_chars:
            items.pop()
            out = render()

    if len(out) > budget_chars and request_text is not None:
        overflow = len(out) - budget_chars
        request_text = request_text[: max(0, len(request_text) - overflow)].rstrip()
        out = render()
    return out[:budget_chars] if len(out) > budget_chars else out
