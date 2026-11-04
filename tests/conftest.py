from datetime import timezone

import pytest


@pytest.fixture(autouse=True)
def _utc_display_zone(monkeypatch):
    """Displayed times use the machine's zone; pin it to UTC so expectations hold on any machine."""
    import memgraph.transcript as transcript

    monkeypatch.setattr(transcript, "_LOCAL_TZ", timezone.utc)


@pytest.fixture(autouse=True)
def _no_real_watcher(monkeypatch):
    """Safety net: no test may start a real watcher process or show a real notification."""
    import memgraph.watch as watch

    def refuse_spawn(project):
        pass  # never start a real watcher (hook-stop tests arm with old fixture deadlines)

    def refuse_notify(title, message):
        raise AssertionError("test tried to show a real notification")

    monkeypatch.setattr(watch, "_spawn", refuse_spawn)
    monkeypatch.setattr(watch, "notify", refuse_notify)


@pytest.fixture(autouse=True)
def _no_claude_dirs(monkeypatch):
    """Running inside Claude Code must not point tests at the real project or config folder."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
