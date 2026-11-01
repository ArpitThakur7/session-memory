import json

from memgraph.transcript import parse_line, parse_lines
from tests.helpers import FIXTURE, LINES


def test_fixture_kinds_in_order():
    events = parse_lines(FIXTURE.read_text(encoding="utf-8").splitlines())
    assert [e.kind for e in events] == ["prompt", "edit", "usage", "command", "error", "todo"]


def test_prompt_text_and_session():
    e = parse_line(LINES[0])[0]
    assert (e.data["text"], e.session, e.ts) == ("Add login page to the app", "s1", "2026-11-01T10:00:00.000Z")


def test_usage_context_sums_three_fields():
    assert [e for e in parse_line(LINES[1]) if e.kind == "usage"][0].data["context"] == 42010


def test_error_keeps_tool_use_id():
    e = parse_line(LINES[3])[0]
    assert e.data == {"text": "FAILED test_login - AssertionError", "tool_use_id": "t2"}


def test_todo_items():
    assert parse_line(LINES[4])[0].data["items"][1] == {"content": "Add logout", "status": "pending"}


def test_ignored_lines():
    assert parse_line(LINES[5]) == [] and parse_line(LINES[6]) == [] and parse_line(LINES[7]) == []


def test_long_text_truncated_to_200():
    line = json.dumps({"type": "user", "sessionId": "s", "timestamp": "t", "message": {"content": "x" * 500}})
    assert len(parse_line(line)[0].data["text"]) == 200
