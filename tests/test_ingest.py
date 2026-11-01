import json

import memgraph.ingest as ingest_mod
from memgraph.graph import Graph
from memgraph.ingest import ingest_dir, load_state, memory_dir, save_state
from tests.helpers import FIXTURE, PROMPT_LINE, copy_fixture


def _complete_part(f):
    """Bytes of the fixture up to and including its last newline."""
    raw = f.read_bytes()
    return raw[: raw.rindex(b"\n") + 1]


def test_ingest_fixture_counts_six_events(tmp_path):
    d = copy_fixture(tmp_path)
    assert ingest_dir(d, Graph(), {}) == 6


def test_second_run_reads_nothing_new(tmp_path):
    d = copy_fixture(tmp_path); g, s = Graph(), {}
    ingest_dir(d, g, s)
    assert ingest_dir(d, g, s) == 0


def test_offset_stops_at_last_newline(tmp_path):
    d = copy_fixture(tmp_path); s = {}
    ingest_dir(d, Graph(), s)
    assert s["offsets"]["session_a.jsonl"] == len(_complete_part(d / "session_a.jsonl"))


def test_cut_off_line_is_read_once_completed(tmp_path):       # Review Focus 1
    d = copy_fixture(tmp_path); g, s = Graph(), {}
    ingest_dir(d, g, s)
    f = d / "session_a.jsonl"
    f.write_bytes(_complete_part(f) + PROMPT_LINE.encode("utf-8") + b"\n")
    assert ingest_dir(d, g, s) == 1


def test_crlf_lines_parse(tmp_path):
    d = tmp_path / "proj"; d.mkdir()
    (d / "a.jsonl").write_bytes(PROMPT_LINE.encode("utf-8") + b"\r\n")
    assert ingest_dir(d, Graph(), {}) == 1


def test_rewritten_file_restarts(tmp_path):
    d = copy_fixture(tmp_path); g, s = Graph(), {}
    ingest_dir(d, g, s); (d / "session_a.jsonl").write_text(PROMPT_LINE + "\n", encoding="utf-8")
    assert ingest_dir(d, g, s) == 1


def test_only_top_level_jsonl_files_are_read(tmp_path):
    d = copy_fixture(tmp_path)
    (d / "sub").mkdir(); (d / "sub" / "deep.jsonl").write_text(PROMPT_LINE + "\n", encoding="utf-8")
    (d / "notes.txt").write_text(PROMPT_LINE + "\n", encoding="utf-8")
    s = {}
    assert ingest_dir(d, Graph(), s) == 6
    assert list(s["offsets"]) == ["session_a.jsonl"]


def test_missing_directory_is_a_noop(tmp_path):
    assert ingest_dir(tmp_path / "nope", Graph(), {}) == 0


def test_raising_line_is_skipped_and_offset_advances(tmp_path, monkeypatch):   # R5
    d = tmp_path / "proj"; d.mkdir()
    bad = PROMPT_LINE.replace("Add logout", "BOOM")
    (d / "a.jsonl").write_bytes("\n".join([PROMPT_LINE, bad, PROMPT_LINE]).encode("utf-8") + b"\n")
    real = ingest_mod.parse_line

    def flaky(line):
        if "BOOM" in line:
            raise ValueError("odd shape")
        return real(line)

    monkeypatch.setattr(ingest_mod, "parse_line", flaky)
    g, s = Graph(), {}
    assert ingest_dir(d, g, s) == 2
    assert s["offsets"]["a.jsonl"] == (d / "a.jsonl").stat().st_size
    assert ingest_dir(d, g, s) == 0


def test_memory_dir_location(tmp_path):
    assert memory_dir(tmp_path) == tmp_path / ".claude" / "session-memory" and memory_dir(tmp_path).is_dir()


def test_state_roundtrip_and_bad_files(tmp_path):
    p = tmp_path / "mem" / "state.json"
    assert load_state(p) == {}
    save_state(p, {"offsets": {"a.jsonl": 5}})
    assert load_state(p) == {"offsets": {"a.jsonl": 5}}
    p.write_text("{not json", encoding="utf-8")
    assert load_state(p) == {}
    p.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert load_state(p) == {}
    assert not list(p.parent.glob("*.tmp"))



def _prompt(session, ts, text):
    return json.dumps({"type": "user", "sessionId": session, "timestamp": ts,
                       "message": {"role": "user", "content": text}})


def test_newer_session_wins_even_when_its_file_sorts_first(tmp_path):    # I1
    import os
    d = tmp_path / "proj"; d.mkdir()
    (d / "a.jsonl").write_text(_prompt("new", "2026-11-01T12:00:00.000Z", "newer request") + "\n", encoding="utf-8")
    (d / "b.jsonl").write_text(_prompt("old", "2026-11-01T10:00:00.000Z", "older request") + "\n", encoding="utf-8")
    os.utime(d / "b.jsonl", (1_000_000, 1_000_000)); os.utime(d / "a.jsonl", (2_000_000, 2_000_000))
    g = Graph()
    ingest_dir(d, g, {})
    assert g.meta["last_prompt"]["text"] == "newer request"


def test_files_are_ingested_oldest_mtime_first(tmp_path, monkeypatch):     # I1
    import os
    d = tmp_path / "proj"; d.mkdir()
    for name in ("a.jsonl", "b.jsonl", "c.jsonl"):
        (d / name).write_text(_prompt(name, "2026-11-01T12:00:00.000Z", name) + "\n", encoding="utf-8")
    os.utime(d / "a.jsonl", (3_000_000, 3_000_000))
    os.utime(d / "b.jsonl", (1_000_000, 1_000_000))
    os.utime(d / "c.jsonl", (1_000_000, 1_000_000))   # same mtime as b: name breaks the tie
    seen = []
    real = ingest_mod.parse_line

    def spy(line):
        seen.append(json.loads(line)["sessionId"])
        return real(line)

    monkeypatch.setattr(ingest_mod, "parse_line", spy)
    ingest_dir(d, Graph(), {})
    assert seen == ["b.jsonl", "c.jsonl", "a.jsonl"]


def test_event_that_fails_to_apply_is_skipped(tmp_path, monkeypatch):    # M3
    d = tmp_path / "proj"; d.mkdir()
    bad = PROMPT_LINE.replace("Add logout", "BOOM")
    (d / "a.jsonl").write_bytes("\n".join([PROMPT_LINE, bad, PROMPT_LINE]).encode("utf-8") + b"\n")
    real = Graph.apply

    def flaky(self, event):
        if event.data.get("text") == "BOOM":
            raise KeyError("odd event")
        return real(self, event)

    monkeypatch.setattr(Graph, "apply", flaky)
    s = {}
    assert ingest_dir(d, Graph(), s) == 2
    assert s["offsets"]["a.jsonl"] == (d / "a.jsonl").stat().st_size
