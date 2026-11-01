from memgraph.brief import build_brief
from memgraph.graph import Graph
from tests.helpers import graph_from_fixture


def test_empty_graph_gives_empty_brief():
    assert build_brief(Graph()) == ""


def test_fixture_brief_contents():
    b = build_brief(graph_from_fixture())
    assert "Last request" in b and "Add login page to the app" in b
    assert "- [in_progress] Fix login test" in b and "- [pending] Add logout" in b
    assert "Create login form" not in b
    assert "src/login.py" in b and "FAILED test_login" in b and "/recall" in b


def test_decisions_section():
    g = graph_from_fixture()
    g.add_node("decision", "d1", "Use SQLite because the app is single-user", "2026-11-01T11:00:00Z")
    assert "- Use SQLite because the app is single-user" in build_brief(g)


def test_budget_enforced_and_low_priority_dropped_first():
    g = graph_from_fixture()
    for i in range(400):
        g.add_node("error", f"e{i}", "E" * 100, f"2026-11-01T12:{i % 60:02d}:00Z")
        g.add_node("file", f"f{i}.py", f"f{i}.py" + "x" * 80, "2026-11-01T12:00:00Z")
        g.add_edge("task:x", "edited", f"file:f{i}.py", "2026-11-01T12:00:00Z")
    b = build_brief(g, budget_chars=1500)
    assert len(b) <= 1500 and "Next steps" in b and "/recall" in b


def test_layout_order_and_timestamp_format():
    g = graph_from_fixture()
    g.add_node("decision", "d1", "Use SQLite", "2026-11-01T11:00:00Z")
    lines = build_brief(g).splitlines()
    assert lines[0] == "# Project memory (auto-loaded by session-memory)"
    assert lines[1].startswith("Last request (2026-11-01 ")
    heads = [ln for ln in lines if ln.startswith("## ")]
    assert heads == ["## Next steps (as of 2026-11-01 10:00)", "## Decisions", "## Recently changed files", "## Recent errors"]
    assert lines[-1] == "For older details run /recall <word>. Do not re-read files listed here unless needed."


def test_caps_and_newest_first():
    g = Graph()
    g.meta["last_prompt"] = {"text": "go", "ts": "", "session": "s"}
    g.meta["last_todos"] = {"session": "s", "ts": "", "items": [
        {"content": f"t{i}", "status": "pending"} for i in range(15)]}
    for i in range(20):
        ts = f"2026-11-01T10:{i:02d}:00Z"
        g.add_node("decision", f"d{i}", f"dec{i:02d}", ts)
        g.add_node("file", f"f{i}", f"file{i:02d}", ts)
        g.add_edge("task:x", "edited", f"file:f{i}", ts)
        g.add_node("error", f"e{i}", f"err{i:02d}", ts)
    b = build_brief(g)
    assert "(unknown)" not in b and "Last request: go" in b  # empty ts: no parenthetical
    assert b.count("- [pending]") == 10 and "t9" in b and "t10" not in b
    assert b.count("- dec") == 10 and "dec19" in b and "dec10" in b and "dec09" not in b
    assert b.count("- file") == 15 and "file19" in b and "file05" in b and "file04" not in b
    assert b.count("- err") == 5 and "err19" in b and "err15" in b and "err14" not in b
    assert b.index("dec19") < b.index("dec18")


def test_only_edited_files_listed():
    g = graph_from_fixture()
    g.add_node("file", "only_read.py", "only_read.py", "2026-11-01T12:00:00Z")
    g.add_edge("task:x", "read", "file:only_read.py", "2026-11-01T12:00:00Z")
    assert "only_read.py" not in build_brief(g)


def test_trim_order_errors_then_files_then_decisions_then_todos():
    g = graph_from_fixture()
    g.add_node("decision", "d1", "Use SQLite", "2026-11-01T11:00:00Z")
    full = build_brief(g)
    sizes = []
    for marker in ("## Recent errors", "## Recently changed files", "## Decisions", "## Next steps"):
        # smallest budget that still keeps this section
        for n in range(len(full), 0, -1):
            if marker not in build_brief(g, budget_chars=n):
                break
        sizes.append(n)
    assert sizes == sorted(sizes, reverse=True)
    assert build_brief(g, budget_chars=len(full)) == full


def test_last_request_cut_when_header_and_footer_alone_exceed_budget():
    g = graph_from_fixture()
    g.meta["last_prompt"]["text"] = "Q" * 5000
    b = build_brief(g, budget_chars=400)
    assert len(b) <= 400 and "/recall" in b and b.startswith("# Project memory")
    assert "Last request" in b and "Q" in b


def test_deterministic_and_non_ascii_preserved():
    g = graph_from_fixture()
    g.add_node("decision", "d1", "Utiliser café — naïve 日本語", "2026-11-01T11:00:00Z")
    assert build_brief(g) == build_brief(g)
    assert "- Utiliser café — naïve 日本語" in build_brief(g)


def test_unparseable_timestamp_left_as_is():
    g = Graph()
    g.meta["last_prompt"] = {"text": "hi", "ts": "yesterday", "session": "s"}
    g.add_node("decision", "d", "x", "")
    assert "Last request (yesterday): hi" in build_brief(g)



def test_next_steps_heading_shows_todo_time():                      # I2
    assert "## Next steps (as of 2026-11-01 10:00)" in build_brief(graph_from_fixture()).splitlines()


def test_finished_steps_from_plan_deltas_are_not_listed():           # I2
    from memgraph.transcript import Event
    g = graph_from_fixture()
    g.apply(Event("todo_delta", "2026-11-01T10:10:00Z", "s1",
                  {"advance": True, "done": [], "start": None, "complete_all": False}))
    b = build_brief(g)
    assert "Fix login test" not in b and "- [in_progress] Add logout" in b
    assert "## Next steps (as of 2026-11-01 10:10)" in b
