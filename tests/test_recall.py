from memgraph.graph import Graph
from memgraph.memory_ops import recall_lines


def kinds(lines):
    return [line.split("]", 1)[0].lstrip("[") for line in lines]


def test_kind_order_decision_task_error_file_command():
    g = Graph()
    g.add_node("command", "c", "grep context notes.txt", "2026-11-05T10:00:00Z")
    g.add_node("file", "f", "docs/context.md", "2026-11-05T09:00:00Z")
    g.add_node("error", "e", "context window exceeded while reading", "2026-11-05T08:00:00Z")
    g.add_node("task", "t", "trim the context before each chat", "2026-11-01T08:00:00Z")
    g.add_node("decision", "d", "Keep the context brief under 1500 chars", "2026-10-01T08:00:00Z")
    assert kinds(recall_lines(g, "context")) == ["decision", "task", "error", "file", "command"]


def test_requests_beat_many_commands_and_commands_are_capped():
    g = Graph()
    for i, word in enumerate("alpha beta gamma delta eps zeta eta theta".split()):
        g.add_node("command", f"c{i}", f"grep -n context {word}.py", f"2026-11-05T1{i}:00:00Z")
    g.add_node("task", "t1", "why is the context so big", "2026-11-01T08:00:00Z")
    g.add_node("task", "t2", "shrink context on resume", "2026-11-02T08:00:00Z")
    lines = recall_lines(g, "context")
    assert kinds(lines)[:2] == ["task", "task"]
    assert kinds(lines).count("command") <= 3


def test_near_identical_commands_are_grouped():
    g = Graph()
    for i in range(6):
        g.add_node("command", f"c{i}", f'python -m pytest -q tests/test_context_{i}.py -k "case {i}"', f"2026-11-05T0{i}:00:00Z")
    lines = recall_lines(g, "context")
    assert len(lines) == 1 and "(+5 similar)" in lines[0]
    assert "test_context_5" in lines[0]  # the newest one represents the group


def test_rare_command_ranks_above_frequent_generic_one():
    g = Graph()
    for i, word in enumerate("one two three four five six seven eight nine ten".split()):
        g.add_node("command", f"g{i}", f"grep {word} src/{word}.py", "2026-11-04T08:00:00Z")
    g.add_node("command", "gc", "grep context src/app.py", "2026-11-05T12:00:00Z")  # newer, but grep is everywhere
    g.add_node("command", "mk", "make context-report", "2026-11-01T08:00:00Z")
    lines = recall_lines(g, "context")
    assert lines[0].startswith("[command] make context-report")


def test_dedupe_by_displayed_text():
    g = Graph()
    g.add_node("task", "a", "fix the context   limit", "2026-11-01T08:00:00Z")
    g.add_node("task", "b", "Fix the context limit", "2026-11-02T08:00:00Z")
    g.add_node("error", "e1", "context error on line 12", "2026-11-02T08:00:00Z")
    g.add_node("error", "e2", "context error on line 40", "2026-11-03T08:00:00Z")
    lines = recall_lines(g, "context")
    assert kinds(lines) == ["task", "error"]
    assert "line 40" in lines[1]  # newest copy shown


def test_whole_word_match_beats_substring_within_a_kind():
    g = Graph()
    g.add_node("task", "a", "update contextual help text", "2026-11-05T08:00:00Z")
    g.add_node("task", "b", "measure context per chat", "2026-11-01T08:00:00Z")
    lines = recall_lines(g, "context")
    assert lines[0].startswith("[task] measure context")


def test_session_nodes_are_not_listed_and_paths_are_project_relative(tmp_path):
    g = Graph()
    g.add_node("session", "s-context", "s-context", "2026-11-05T08:00:00Z")
    g.add_node("file", str(tmp_path / "src" / "context.py").replace("\\", "/"),
               str(tmp_path / "src" / "context.py").replace("\\", "/"), "2026-11-05T08:00:00Z")
    lines = recall_lines(g, "context", root=tmp_path)
    assert kinds(lines) == ["file"] and "] src/context.py" in lines[0]
