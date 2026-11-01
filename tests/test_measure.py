from bench.measure import format_table, measure
from tests.helpers import copy_fixture


def test_measure_fixture(tmp_path):
    rows = measure(copy_fixture(tmp_path))
    assert rows[0]["history_tokens"] == 42010 and rows[0]["brief_tokens"] < 3000 and rows[0]["saved_pct"] > 90


def test_row_fields_and_skip_transcripts_without_usage(tmp_path):
    d = copy_fixture(tmp_path)
    (d / "empty.jsonl").write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n', encoding="utf-8")
    rows = measure(d)
    assert [r["session"] for r in rows] == ["session_a.jsonl"]
    assert set(rows[0]) == {"session", "history_tokens", "brief_tokens", "saved_pct"}


def test_missing_dir_gives_no_rows(tmp_path):
    assert measure(tmp_path / "nope") == []


def test_table_is_markdown(tmp_path):
    out = format_table(measure(copy_fixture(tmp_path)))
    assert out.splitlines()[0].startswith("| session |") and "42010" in out



def test_zero_history_is_a_row_only_none_is_skipped(tmp_path, monkeypatch):   # M6
    import bench.measure as measure_mod
    from memgraph.graph import Graph
    d = tmp_path / "proj"; d.mkdir()
    (d / "zero.jsonl").write_text("{}\n", encoding="utf-8")
    (d / "none.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(measure_mod, "_read_transcript",
                        lambda path: (Graph(), 0 if path.name == "zero.jsonl" else None))
    rows = measure(d)
    assert [(r["session"], r["history_tokens"]) for r in rows] == [("zero.jsonl", 0)]
