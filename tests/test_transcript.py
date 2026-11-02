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



def _tool_line(name, inp):
    return json.dumps({"type": "assistant", "sessionId": "s", "timestamp": "2026-11-01T12:00:00.000Z",
                       "message": {"content": [{"type": "tool_use", "id": "u1", "name": name, "input": inp}]}})


def _deltas(line):
    return [e.data for e in parse_line(line) if e.kind == "todo_delta"]


def test_update_plan_without_items_gives_todo_delta():                 # I2
    plan = "mcp__ccd_turn__update_plan"
    assert _deltas(_tool_line(plan, {"advance": True})) == [
        {"advance": True, "done": [], "start": None, "complete_all": False}]
    assert _deltas(_tool_line(plan, {"done": [2, "x", 3], "start": 4})) == [
        {"advance": False, "done": [2, 3], "start": 4, "complete_all": False}]
    assert _deltas(_tool_line(plan, {"items": [{"content": "a", "status": "pending"}], "scope": "first_task"})) == []


def test_wrap_up_completes_every_step():                               # I2
    line = _tool_line("mcp__ccd_turn__wrap_up", {"recap": "Done.", "ask": "Ship?"})
    assert _deltas(line) == [{"advance": False, "done": [], "start": None, "complete_all": True}]



def _user(content, **extra):
    obj = {"type": "user", "sessionId": "s", "timestamp": "2026-11-01T12:00:00.000Z",
           "message": {"role": "user", "content": content}}
    obj.update(extra)
    return json.dumps(obj)


def test_interruptions_and_compact_summaries_are_not_prompts():       # I3
    assert parse_line(_user("[Request interrupted by user]")) == []
    assert parse_line(_user([{"type": "text", "text": "[Request interrupted by user for tool use]"}])) == []
    assert parse_line(_user("This session is being continued from a previous one...", isCompactSummary=True)) == []
    assert [e.kind for e in parse_line(_user("real request"))] == ["prompt"]


def test_tool_rejections_are_not_errors():                             # I3
    def result(text):
        return _user([{"type": "tool_result", "tool_use_id": "t", "is_error": True, "content": text}])

    assert parse_line(result("The user doesn't want to proceed with this tool use. The tool use was rejected")) == []
    assert parse_line(result("Rejected. STOP what you are doing and wait for the user")) == []
    assert [e.kind for e in parse_line(result("FAILED test_x"))] == ["error"]


def test_harness_tool_use_errors_are_not_errors():
    def result(text):
        return _user([{"type": "tool_result", "tool_use_id": "t", "is_error": True, "content": text}])

    assert parse_line(result("<tool_use_error>String to replace not found in file.</tool_use_error>")) == []
    assert parse_line(result("  <tool_use_error>File has not been read yet</tool_use_error>")) == []
    assert [e.kind for e in parse_line(result("Exit code 1: <tool_use_error> later"))] == ["error"]


def test_session_titles():                                             # I3
    custom = parse_line(json.dumps({"type": "custom-title", "customTitle": "Login work", "sessionId": "s"}))
    ai = parse_line(json.dumps({"type": "ai-title", "aiTitle": "Fix the login", "sessionId": "s"}))
    assert [(e.kind, e.session, e.data) for e in custom] == [("title", "s", {"text": "Login work", "source": "custom"})]
    assert [(e.kind, e.session, e.data) for e in ai] == [("title", "s", {"text": "Fix the login", "source": "ai"})]
    assert parse_line(json.dumps({"type": "ai-title", "aiTitle": "  ", "sessionId": "s"})) == []


def _replies(line):
    return [e.data["text"] for e in parse_line(line) if e.kind == "reply"]


def test_reply_is_last_text_block_of_a_real_assistant_line():         # I3
    obj = {"type": "assistant", "sessionId": "s", "timestamp": "2026-11-01T12:00:00.000Z",
           "message": {"model": "claude-opus", "content": [
               {"type": "text", "text": "first"}, {"type": "tool_use", "id": "x", "name": "Grep", "input": {}},
               {"type": "text", "text": "Tests pass; next is the logout page."}]}}
    assert _replies(json.dumps(obj)) == ["Tests pass; next is the logout page."]
    obj["message"]["content"] = [{"type": "text", "text": "R" * 900}]
    assert _replies(json.dumps(obj)) == ["R" * 600]
    obj["message"]["content"] = [{"type": "text", "text": "  \n"}]
    assert _replies(json.dumps(obj)) == []
    assert _replies(_assistant_line(model="<synthetic>", text="No response requested.")) == []


def test_wrap_up_recap_and_ask_are_a_reply():                         # I3
    line = _tool_line("mcp__ccd_turn__wrap_up", {"recap": "Login works.", "ask": "Commit it?"})
    assert _replies(line) == ["Login works. Commit it?"]
    assert _replies(_tool_line("mcp__ccd_turn__wrap_up", {"recap": "Only recap."})) == ["Only recap."]


def test_sidechain_lines_keep_only_edit_read_command_error():
    from tests.helpers import SUBAGENT_FIXTURE
    kinds = [e.kind for e in parse_lines(SUBAGENT_FIXTURE.read_text(encoding="utf-8").splitlines())]
    assert kinds == ["read", "edit", "command", "error"]


def _prompts(content):
    return [e.data["text"] for e in parse_line(_user(content)) if e.kind == "prompt"]


def test_pasted_content_only_prompt_becomes_pasted_preview():
    inner = "line one of the paste\n" + "x" * 300
    assert _prompts(f'<pasted_content id="a331">\n{inner}\n</pasted_content>') == \
        ["[pasted] " + " ".join(inner.split())[:100]]
    assert _prompts([{"type": "text", "text": '<pasted_content id="a1">short paste</pasted_content>'}]) == \
        ["[pasted] short paste"]


def test_system_reminders_and_leftover_tags_are_stripped_from_prompts():
    assert _prompts("<system-reminder>context here</system-reminder>\nFix the login bug") == ["Fix the login bug"]
    assert _prompts("Fix it <system-reminder>secret\nstuff</system-reminder> now") == ["Fix it  now"]
    assert _prompts('Review <pasted_content id="b2">long text</pasted_content> please') == ["Review [pasted] please"]
    assert _prompts('Use <user_note kind="x">the cache</user_note> layer') == ["Use the cache layer"]
    assert _prompts("Keep List<String> and a < b > c") == ["Keep List<String> and a < b > c"]
    assert _prompts("<system-reminder>only a reminder</system-reminder>") == []
    assert _prompts("<command-name>/clear</command-name>") == []


def test_clean_prompt_handles_cut_off_markup():
    from memgraph.transcript import clean_prompt
    assert clean_prompt('<pasted_content id="z">abc def') == "[pasted] abc def"
    assert clean_prompt("Do it <system-reminder>never closed") == "Do it"
    assert clean_prompt("[pasted] already clean") == "[pasted] already clean"
