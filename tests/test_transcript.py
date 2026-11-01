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


def _assistant_line(model="claude-opus", usage=None, api_error=False, text="hi"):
    obj = {"type": "assistant", "sessionId": "s", "timestamp": "2026-11-01T12:00:00.000Z",
           "message": {"model": model, "content": [{"type": "text", "text": text}]}}
    if usage is not None:
        obj["message"]["usage"] = usage
    if api_error:
        obj["isApiErrorMessage"] = True
    return json.dumps(obj)


ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
REAL_USAGE = {"input_tokens": 5, "cache_read_input_tokens": 90000, "cache_creation_input_tokens": 100}


def test_synthetic_assistant_lines_emit_no_usage():                    # C1
    for line in (
        _assistant_line(model="<synthetic>", usage=REAL_USAGE),
        _assistant_line(usage=REAL_USAGE, api_error=True),
        _assistant_line(usage=ZERO_USAGE),
    ):
        assert [e for e in parse_line(line) if e.kind == "usage"] == []


def test_is_synthetic():                                               # C1
    from memgraph.transcript import is_synthetic
    assert is_synthetic(json.loads(_assistant_line(model="<synthetic>")))
    assert is_synthetic(json.loads(_assistant_line(api_error=True)))
    assert is_synthetic(json.loads(_assistant_line(usage=ZERO_USAGE)))
    assert not is_synthetic(json.loads(_assistant_line(usage=REAL_USAGE)))
    assert not is_synthetic(json.loads(_assistant_line()))  # no usage at all: a normal streamed line
