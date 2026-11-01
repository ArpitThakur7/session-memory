from pathlib import Path

FIXTURE: Path = Path(__file__).parent / "fixtures" / "session_a.jsonl"
LINES: list[str] = FIXTURE.read_text(encoding="utf-8").splitlines()


def graph_from_fixture():
    """Build a Graph by applying every event of the Task 1 fixture."""
    from memgraph.graph import Graph
    from memgraph.transcript import parse_lines

    g = Graph()
    for ev in parse_lines(LINES):
        g.apply(ev)
    return g
