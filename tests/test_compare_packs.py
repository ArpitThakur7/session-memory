from bench.compare_packs import compare, format_table, questions
from tests.helpers import copy_fixture


def test_compare_packs_table_and_questions(tmp_path):
    d = copy_fixture(tmp_path)
    rows = compare(d)
    assert len(rows) == 1 and rows[0]["history_tokens"] == 42010
    assert 0 < rows[0]["tiny"] <= rows[0]["standard"] <= rows[0]["deep"]
    assert rows[0]["visual"] in (1590, None)
    table = format_table(rows)
    assert table.splitlines()[0].startswith("| session |") and "tiny" in table and "42010" in table
    qs = questions(d)
    assert len(qs) == 10
    text = "\n".join(qs)
    assert "Add login page to the app" in text and "src/login.py" in text and "FAILED test_login" in text


def test_missing_dir(tmp_path):
    assert compare(tmp_path / "nope") == []
