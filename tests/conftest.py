import pytest


@pytest.fixture(autouse=True)
def _no_real_watcher(monkeypatch):
    """Safety net: no test may start a real watcher process or show a real notification."""
    import memgraph.watch as watch

    def refuse_spawn(project):
        raise AssertionError("test tried to spawn a real watcher")

    def refuse_notify(title, message):
        raise AssertionError("test tried to show a real notification")

    monkeypatch.setattr(watch, "_spawn", refuse_spawn)
    monkeypatch.setattr(watch, "notify", refuse_notify)
