from pathlib import Path

FIXTURE: Path = Path(__file__).parent / "fixtures" / "session_a.jsonl"
LINES: list[str] = FIXTURE.read_text(encoding="utf-8").splitlines()
