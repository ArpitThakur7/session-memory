from pathlib import Path

FIXTURE: Path = Path(__file__).parent / "fixtures" / "session_a.jsonl"
LINES: list[str] = FIXTURE.read_text(encoding="utf-8").splitlines()
SUBAGENT_FIXTURE: Path = FIXTURE.parent / "s1" / "subagents" / "agent-a1.jsonl"


def graph_from_fixture():
    """Build a Graph by applying every event of the Task 1 fixture."""
    from memgraph.graph import Graph
    from memgraph.transcript import parse_lines

    g = Graph()
    for ev in parse_lines(LINES):
        g.apply(ev)
    return g

PROMPT_LINE: str = (
    '{"type":"user","sessionId":"s1","timestamp":"2026-11-01T11:00:00.000Z",'
    '"message":{"role":"user","content":"Add logout"}}'
)


def copy_fixture(tmp_path: Path) -> Path:
    """Copy the fixture byte-for-byte into tmp_path/'proj' and return that directory."""
    import shutil

    d = tmp_path / "proj"
    d.mkdir()
    shutil.copyfile(FIXTURE, d / FIXTURE.name)
    return d


def copy_subagent_fixture(tmp_path: Path) -> Path:
    """copy_fixture plus the helper-agent transcript at proj/s1/subagents/agent-a1.jsonl."""
    import shutil

    d = copy_fixture(tmp_path)
    (d / "s1" / "subagents").mkdir(parents=True)
    shutil.copyfile(SUBAGENT_FIXTURE, d / "s1" / "subagents" / SUBAGENT_FIXTURE.name)
    return d
