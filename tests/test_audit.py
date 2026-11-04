import io
import json
from datetime import datetime, timezone
from pathlib import Path

from memgraph import audit, cli
from memgraph.usage import collect_usage, helper_rows, helper_table, summarize

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def reply(ts, mid, session, inp, write, read, model="claude-opus-5-5", sidechain=False):
    obj = {"type": "assistant", "sessionId": session, "timestamp": ts, "requestId": "r" + mid,
           "message": {"id": mid, "model": model, "content": [],
                       "usage": {"input_tokens": inp, "cache_creation_input_tokens": write,
                                 "cache_read_input_tokens": read, "output_tokens": 10}}}
    if sidechain:
        obj["isSidechain"] = True
    return json.dumps(obj)


def make_transcripts(tmp_path):
    d = tmp_path / "t"
    (d / "a" / "subagents").mkdir(parents=True)
    (d / "a.jsonl").write_text("\n".join([
        reply("2026-11-01T10:00:00Z", "1", "a", 10, 9000, 0),
        reply("2026-11-01T10:05:00Z", "2", "a", 5, 100, 9010)]) + "\n", encoding="utf-8")
    (d / "b.jsonl").write_text(reply("2026-11-02T10:00:00Z", "3", "b", 20, 0, 12000) + "\n", encoding="utf-8")
    (d / "c.jsonl").write_text(reply("2026-11-03T10:00:00Z", "4", "c", 30, 3000, 0) + "\n", encoding="utf-8")
    sub = d / "a" / "subagents"
    (sub / "agent-x1.jsonl").write_text("\n".join([
        reply("2026-11-01T10:01:00Z", "5", "a", 100, 0, 1000, model="claude-haiku-4-5", sidechain=True),
        reply("2026-11-01T10:02:00Z", "6", "a", 100, 0, 1000, model="claude-haiku-4-5", sidechain=True)]) + "\n",
        encoding="utf-8")
    (sub / "agent-x1.meta.json").write_text(json.dumps({"description": "Explore the | thing", "agentType": "Explore"}),
                                            encoding="utf-8")
    (sub / "agent-x2.jsonl").write_text(reply("2026-11-01T10:03:00Z", "7", "a", 1000, 0, 100000, sidechain=True) + "\n",
                                        encoding="utf-8")
    return d


def test_start_cost_median_min_max(tmp_path):
    s = audit.start_cost(collect_usage(make_transcripts(tmp_path)).replies)
    assert s == {"sessions": 3, "median": 9010, "min": 3030, "max": 12020}


def test_start_cost_empty():
    assert audit.start_cost([])["sessions"] == 0


def test_helper_rows_sorted_by_cost_with_descriptions(tmp_path):
    d = make_transcripts(tmp_path)
    rows = helper_rows(collect_usage(d).replies, d)
    assert [r["key"] for r in rows] == ["agent-x2", "agent-x1"]
    assert rows[0]["description"] == "agent-x2"  # no meta file: the agent id
    assert rows[1]["description"] == "Explore the | thing" and rows[1]["replies"] == 2
    assert rows[1]["model"] == "claude-haiku-4-5"
    table = helper_table(rows)
    assert "Explore the / thing" in table and table.count("\n") == 3
    assert [r["key"] for r in summarize(collect_usage(d).replies, "helper")] == ["agent-x2", "agent-x1"]


def test_usage_by_helper_cli(tmp_path):
    d = make_transcripts(tmp_path)
    out = io.StringIO()
    assert cli.main(["usage", "--by", "helper", "--days", "30", "--transcripts", str(d)], io.StringIO(), out, NOW) == 0
    text = out.getvalue()
    assert "| helper | model | replies | tokens | cost |" in text and "Explore the / thing" in text
    assert "Total: 3 replies" in text


def test_claude_md_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("u" * 400, encoding="utf-8")
    proj = home / "work" / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("p" * 80, encoding="utf-8")
    (proj / ".claude" / "CLAUDE.md").write_text("q" * 40, encoding="utf-8")
    (proj / "CLAUDE.local.md").write_text("l" * 8, encoding="utf-8")
    (home / "work" / "CLAUDE.md").write_text("w" * 4000, encoding="utf-8")
    files = audit.claude_md_files(proj, home)
    assert sorted(f["tokens"] for f in files) == [2, 10, 20, 100, 1000]
    other = tmp_path / "cfg"
    other.mkdir()
    (other / "CLAUDE.md").write_text("c" * 60, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    files = audit.claude_md_files(proj, home)
    assert files[0]["path"] == str(other / "CLAUDE.md") and files[0]["tokens"] == 15


MCP = "Checking MCP server health…\n\nplugin:github:github: https://x/mcp/ (HTTP) - X Failed to connect\n" \
      "local: npx thing - OK Connected (12 tools)\n"
PLUGINS = ("Installed plugins:\n\n  ❯ big@m\n    Version: 1.0\n    Scope: user\n    Status: ✔ enabled\n\n"
           "  ❯ off@m\n    Version: 1.0\n    Status: ✘ disabled\n\n"
           "  ❯ small@m\n    Status: ✔ enabled\n")


def details(tok, skills=2):
    return f"x\n  Skills ({skills})  a, b\n\nProjected token cost\n  Always-on:   ~{tok} tok   added to every session\n"


def fake_claude(calls):
    answers = {("mcp", "list"): (0, MCP), ("plugin", "list"): (0, PLUGINS),
               ("plugin", "details", "big@m"): (0, details("3.2k")),
               ("plugin", "details", "small@m"): (0, details("40", 0)),
               ("plugin", "details", "off@m"): (0, details("9k"))}

    def ask(project, args):
        calls.append(tuple(args))
        return answers.get(tuple(args), (1, ""))
    return ask


def test_parsers():
    assert audit.parse_mcp_list(MCP) == [
        {"name": "plugin:github:github", "tools": None, "line": "https://x/mcp/ (HTTP) - X Failed to connect"},
        {"name": "local", "tools": 12, "line": "npx thing - OK Connected (12 tools)"}]
    assert audit.parse_plugin_list(PLUGINS) == ["big@m", "small@m"]
    assert audit.parse_plugin_details(details("3.2k")) == {"tokens": 3200, "skills": 2}
    assert audit.parse_plugin_details("no cost here") == {"tokens": None, "skills": None}


def test_run_audit_with_fake_claude(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(audit, "_ask", fake_claude(calls))
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("u" * 2000, encoding="utf-8")
    proj = tmp_path / "proj"
    mem = proj / ".claude" / "session-memory"
    mem.mkdir(parents=True)
    events = [{"t": "2026-11-03T10:00:00+00:00", "type": "read_denied"},
              {"t": "2026-11-03T10:00:00+00:00", "type": "big_read_steer", "lines": 900, "outline_chars": 700},
              {"t": "2026-11-03T10:00:00+00:00", "type": "jit_context", "chars": 300, "hits": 2},
              {"t": "2026-10-01T10:00:00+00:00", "type": "read_denied"}]
    (mem / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    res = audit.run_audit(proj, make_transcripts(tmp_path), home, NOW)
    assert res["start"]["median"] == 9010
    assert res["this_week"] == {"read_denied": 1, "big_read_steers": 1, "steer_outline_chars": 700,
                                "steer_file_lines": 900, "related_memory": 1, "related_memory_chars": 300}
    assert [p["name"] for p in res["plugins"]] == ["big@m", "small@m"] and res["plugins"][0]["tokens"] == 3200
    assert ("plugin", "details", "off@m") not in calls
    assert [s["item"] for s in res["suggestions"]] == ["big@m", str(home / ".claude" / "CLAUDE.md")]
    assert res["suggestions"][0]["text"] == ("Turn off plugin big@m (about 3,200 tokens in every chat): "
                                             "claude plugin disable big@m")
    text = audit.format_audit(res)
    assert "starts at about 9,010 tokens before your first message is answered (measured from 3 chats" in text
    assert "(estimate)" in text and "MCP server local: 12 tools" in text


def test_plugin_cap(monkeypatch, tmp_path):
    ids = "\n".join(f"  ❯ p{i}@m\n    Status: enabled" for i in range(25))

    def ask(project, args):
        if args[:2] == ["plugin", "list"]:
            return 0, ids
        return (0, details("500")) if args[0] == "plugin" else (0, "")
    monkeypatch.setattr(audit, "_ask", ask)
    res = audit.check_claude(tmp_path)
    assert len(res["plugins"]) == 20 and res["plugins_skipped"] == 5


def test_missing_claude_never_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_ask", lambda project, args: None)
    out = io.StringIO()
    proj = tmp_path / "p"
    proj.mkdir()
    assert cli.main(["audit", "--project", str(proj), "--transcripts", str(tmp_path / "none")],
                    io.StringIO(), out, NOW) == 0
    text = out.getvalue()
    assert "no measured start yet" in text and "MCP servers: not checked" in text and "Plugins: not checked" in text
    out = io.StringIO()
    assert cli.main(["audit", "--project", str(proj), "--json", "--transcripts", str(tmp_path / "none")],
                    io.StringIO(), out, NOW) == 0
    data = json.loads(out.getvalue())
    assert data["mcp"] is None and data["plugins"] is None and data["start"]["sessions"] == 0


def test_audit_bad_args():
    out = io.StringIO()
    assert cli.main(["audit", "--bogus"], io.StringIO(), out, NOW) == 2
    assert out.getvalue().startswith("Usage: audit")


def test_map_data_and_template_include_helpers(tmp_path):
    from memgraph.savings import token_view

    d = make_transcripts(tmp_path)
    tv = token_view(d, tmp_path / "mem", now=NOW)
    assert [h["description"] for h in tv["helpers"]] == ["agent-x2", "Explore the | thing"]
    html = (Path(audit.__file__).parent / "viewer_template.html").read_text(encoding="utf-8")
    assert "split('Helpers'" in html and "Helpers (top 10 by cost)" in html


def test_mcp_usage_text_lists_helpers(tmp_path):
    from memgraph.mcp_server import _usage_text

    d = make_transcripts(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "state.json").write_text(json.dumps({"transcripts_dir": str(d)}), encoding="utf-8")
    text = _usage_text({"days": 3650}, mem, tmp_path)
    assert "Top helpers by cost:" in text and "Explore the | thing" in text
