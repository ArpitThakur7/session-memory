"""Status line: one cheap line from Claude Code's stdin JSON; setup-statusline with a temp HOME only."""
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import memgraph.statusline as sl
from memgraph.cli import main

NOW = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("SESSION_MEMORY_WARN_TOKENS", "SESSION_MEMORY_STATUSLINE"):
        monkeypatch.delenv(name, raising=False)


def sample(tmp_path, **over):
    data = {
        "model": {"id": "claude-opus", "display_name": "Opus"},
        "session_id": "s1",
        "workspace": {"current_dir": str(tmp_path), "project_dir": str(tmp_path)},
        "context_window": {"total_input_tokens": 142_000, "context_window_size": 200_000,
                           "used_percentage": 71, "current_usage": {"input_tokens": 2000,
                                                                    "cache_creation_input_tokens": 1000,
                                                                    "cache_read_input_tokens": 139_000}},
        "prompt_cache": {"warm": True, "ttl": "1h", "expires_at": int((NOW + timedelta(minutes=38)).timestamp())},
    }
    data.update(over)
    return data


def line(data, now=NOW):
    out = io.StringIO()
    rc = main(["statusline"], io.StringIO(data if isinstance(data, str) else json.dumps(data)), out, now=now)
    assert rc == 0
    text = out.getvalue()
    assert text.count("\n") <= 1
    return text.rstrip("\n")


def plain(text):
    return ANSI.sub("", text)


def mem(tmp_path):
    d = tmp_path / ".claude" / "session-memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_full_line(tmp_path):
    (mem(tmp_path) / "summary.json").write_text(json.dumps({"measured_saved_tokens": 1_234_567}), encoding="utf-8")
    assert line(sample(tmp_path)) == "Opus · chat 142k (71%) · cache 38m · saved 1.2M"


def test_missing_everything_does_not_crash(tmp_path):
    assert line({}) == ""
    assert line("garbage") == ""
    assert line({"model": "x", "context_window": None, "prompt_cache": 5}) == ""


def test_missing_cache_and_summary_sections_are_dropped(tmp_path):
    data = sample(tmp_path)
    del data["prompt_cache"]
    assert plain(line(data)) == "Opus · chat 142k (71%)"


def test_percentage_computed_when_absent(tmp_path):
    data = sample(tmp_path)
    data["context_window"] = {"total_input_tokens": 50_000, "context_window_size": 200_000}
    assert "chat 50k (25%)" in line(data)


def test_current_usage_null_after_compact_uses_totals(tmp_path):
    data = sample(tmp_path)
    data["context_window"]["current_usage"] = None
    data["context_window"]["used_percentage"] = None
    assert "chat 142k (71%)" in line(data)


def test_warning_yellow_then_red(tmp_path, monkeypatch):
    data = sample(tmp_path)
    data["context_window"].update(total_input_tokens=162_000, used_percentage=81, current_usage=None)
    text = line(data)
    assert "\x1b[33m" in text and "chat 162k — /compact or /clear soon" in plain(text)
    data["context_window"].update(total_input_tokens=230_000, used_percentage=None)
    assert "\x1b[31m" in line(data)
    monkeypatch.setenv("SESSION_MEMORY_WARN_TOKENS", "300000")
    assert "\x1b[" not in line(data)


def test_big_windows_warn_by_percentage(tmp_path):
    data = sample(tmp_path)
    data["context_window"].update(total_input_tokens=230_000, context_window_size=1_000_000, used_percentage=23,
                                  current_usage=None)
    assert plain(line(data)).startswith("Opus · chat 230k (23%)") and "\x1b[" not in line(data)
    data["context_window"].update(total_input_tokens=760_000, used_percentage=76)
    text = line(data)
    assert "\x1b[33m" in text and "chat 760k — /compact or /clear soon" in plain(text)
    data["context_window"].update(total_input_tokens=920_000, used_percentage=92)
    assert "\x1b[31m" in line(data)


def test_before_first_reply_shows_dash(tmp_path):
    data = sample(tmp_path)
    del data["prompt_cache"]
    data["context_window"] = {"total_input_tokens": 0, "context_window_size": 200_000, "used_percentage": 0,
                              "current_usage": None}
    assert plain(line(data)) == "Opus · chat —"


def test_cache_cold_when_expired_or_not_warm(tmp_path):
    data = sample(tmp_path)
    data["prompt_cache"]["expires_at"] = int((NOW - timedelta(minutes=1)).timestamp())
    assert "cache cold" in line(data)
    data["prompt_cache"] = {"warm": False, "expires_at": None}
    assert "cache cold" in line(data)


def test_cache_falls_back_to_watch_json(tmp_path):
    data = sample(tmp_path)
    del data["prompt_cache"]
    (mem(tmp_path) / "watch.json").write_text(json.dumps({"deadline": (NOW + timedelta(minutes=12)).isoformat()}),
                                              encoding="utf-8")
    assert "cache 12m" in line(data)
    (mem(tmp_path) / "watch.json").write_text(json.dumps({"deadline": (NOW - timedelta(minutes=2)).isoformat()}),
                                              encoding="utf-8")
    assert "cache cold" in line(data)


def test_hours_shown_for_long_ttl(tmp_path):
    data = sample(tmp_path)
    data["prompt_cache"]["expires_at"] = int((NOW + timedelta(minutes=95)).timestamp())
    assert "cache 1h35m" in line(data)


def test_saved_formatting():
    assert sl.fmt_tokens(950) == "950"
    assert sl.fmt_tokens(12_400) == "12k"
    assert sl.fmt_tokens(1_234_567) == "1.2M"


def test_line_at_most_120_visible_chars(tmp_path):
    data = sample(tmp_path, model={"display_name": "M" * 300})
    (mem(tmp_path) / "summary.json").write_text(json.dumps({"measured_saved_tokens": 10**12}), encoding="utf-8")
    data["context_window"]["total_input_tokens"] = 10**12
    assert len(plain(line(data))) <= 120


def test_env_off_prints_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_MEMORY_STATUSLINE", "0")
    assert line(sample(tmp_path)) == ""


def test_status_line_does_not_create_memory_folder(tmp_path):
    line(sample(tmp_path))
    assert not (tmp_path / ".claude").exists()


# ---- summary.json written by hook-stop ----

def test_hook_stop_writes_summary(tmp_path):
    from tests.helpers import copy_fixture

    proj = copy_fixture(tmp_path)
    transcript = next(proj.glob("*.jsonl"))
    payload = {"session_id": "s1", "transcript_path": str(transcript), "cwd": str(proj)}
    main(["hook-stop"], io.StringIO(json.dumps(payload)), io.StringIO(), now=NOW)
    summary = json.loads((proj / ".claude" / "session-memory" / "summary.json").read_text(encoding="utf-8"))
    assert summary["measured_saved_tokens"] == 0
    assert "updated" in summary


# ---- setup-statusline ----

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


def setup(*args):
    out = io.StringIO()
    rc = main(["setup-statusline", *args], io.StringIO(""), out)
    return rc, out.getvalue()


def settings(home):
    return home / ".claude" / "settings.json"


def backups(home):
    return list((home / ".claude").glob("settings.json.*.bak"))


def test_setup_creates_settings(home):
    rc, text = setup()
    assert rc == 0, text
    data = json.loads(settings(home).read_text(encoding="utf-8"))
    entry = data["statusLine"]
    assert entry["type"] == "command" and entry["padding"] == 0
    shim_dir = home / ".claude" / "session-memory"
    name = "statusline.cmd" if os.name == "nt" else "statusline.sh"
    assert entry["command"] == '"' + (shim_dir / name).as_posix() + '"'
    assert {p.name for p in shim_dir.iterdir()} == {"statusline.py", "statusline.sh", "statusline.cmd"}
    assert backups(home) == []


def test_setup_keeps_other_keys_and_backs_up(home):
    settings(home).parent.mkdir()
    settings(home).write_text(json.dumps({"model": "opus", "hooks": {"Stop": []}, "env": {"A": "1"}}), encoding="utf-8")
    rc, text = setup()
    assert rc == 0, text
    data = json.loads(settings(home).read_text(encoding="utf-8"))
    assert data["model"] == "opus" and data["hooks"] == {"Stop": []} and data["env"] == {"A": "1"}
    assert "statusLine" in data
    assert len(backups(home)) == 1
    assert "+" in text and "statusLine" in text  # printed diff


def test_setup_dry_run_changes_nothing(home):
    settings(home).parent.mkdir()
    settings(home).write_text('{"model": "opus"}', encoding="utf-8")
    rc, text = setup("--dry-run")
    assert rc == 0 and "Dry run" in text and "statusLine" in text and "statusline.py" in text
    assert settings(home).read_text(encoding="utf-8") == '{"model": "opus"}'
    assert not (home / ".claude" / "session-memory").exists()
    assert backups(home) == []


def test_setup_refuses_other_statusline_unless_force(home):
    settings(home).parent.mkdir()
    settings(home).write_text(json.dumps({"statusLine": {"type": "command", "command": "~/mine.sh"}}),
                              encoding="utf-8")
    rc, text = setup()
    assert rc == 2 and "--force" in text
    assert "mine.sh" in settings(home).read_text(encoding="utf-8")
    rc, text = setup("--force")
    assert rc == 0
    assert "statusline" in json.loads(settings(home).read_text(encoding="utf-8"))["statusLine"]["command"]
    assert len(backups(home)) == 1


def test_setup_is_idempotent(home):
    setup()
    rc, text = setup()
    assert rc == 0 and "Already set up" in text
    assert backups(home) == []


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]", '{"statusLine": "x"}'])
def test_setup_refuses_invalid_json(home, bad):
    settings(home).parent.mkdir()
    settings(home).write_text(bad, encoding="utf-8")
    rc, text = setup()
    assert rc == 2 and "Not changed" in text
    assert settings(home).read_text(encoding="utf-8") == bad


def test_setup_bad_args(home):
    rc, text = setup("--bogus")
    assert rc == 2 and "Usage" in text


# ---- doctor ----

def test_doctor_status_line_check(home, tmp_path, monkeypatch):
    import memgraph.doctor as doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    proj = tmp_path / "p"
    proj.mkdir()
    by = {c["name"]: c for c in doctor.run_checks(proj, home)}
    assert by["statusline"]["status"] == "skip" and "setup-statusline" in by["statusline"]["fix"]
    setup()
    by = {c["name"]: c for c in doctor.run_checks(proj, home)}
    assert by["statusline"]["status"] == "ok"
    (home / ".claude" / "session-memory" / "statusline.py").unlink()
    by = {c["name"]: c for c in doctor.run_checks(proj, home)}
    assert by["statusline"]["status"] == "fail" and "setup-statusline" in by["statusline"]["fix"]
    settings(home).write_text(json.dumps({"statusLine": {"type": "command", "command": "other"}}), encoding="utf-8")
    by = {c["name"]: c for c in doctor.run_checks(proj, home)}
    assert by["statusline"]["status"] == "skip" and "--force" in by["statusline"]["fix"]


def test_old_launcher_entry_is_ours_and_upgraded(home):
    settings(home).parent.mkdir()
    old = {"type": "command", "command": '"C:/x/session-memory/bin/run-python.cmd" statusline', "padding": 0}
    settings(home).write_text(json.dumps({"statusLine": old}), encoding="utf-8")
    rc, text = setup()
    assert rc == 0, text
    assert "statusline." in json.loads(settings(home).read_text(encoding="utf-8"))["statusLine"]["command"]


def _fake_install(base, label):
    pkg = base / "memgraph"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cli.py").write_text(f"def main(argv, stdin, stdout, now=None):\n    stdout.write({label!r} + ' ' + "
                                f"' '.join(argv) + ' ' + stdin.read())\n    return 0\n", encoding="utf-8")
    return base


def _run_shim(shim, env):
    import subprocess
    import sys

    r = subprocess.run([sys.executable, str(shim)], input="{}", capture_output=True, text=True, env=env,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_shim_runs_newest_plugin_install_or_recorded_checkout(tmp_path):
    import shutil
    import time

    cfg = tmp_path / "cfg"
    old = _fake_install(cfg / "plugins" / "cache" / "market" / "session-memory" / "0.2.0", "old")
    new = _fake_install(cfg / "plugins" / "cache" / "market" / "session-memory" / "0.3.0", "new")
    t0 = time.time()
    os.utime(old, (t0 - 100, t0 - 100))
    os.utime(new, (t0, t0))
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "CLAUDE_CONFIG_DIR")}
    shim = sl.write_shim(tmp_path / "shim", recorded=old, cfg=cfg)  # recorded inside the cache: may be stale
    assert _run_shim(shim, env) == "new statusline {}"
    checkout = _fake_install(tmp_path / "checkout", "checkout")
    shim = sl.write_shim(tmp_path / "shim", recorded=checkout, cfg=cfg)  # a development checkout wins
    assert _run_shim(shim, env) == "checkout statusline {}"
    shutil.rmtree(cfg / "plugins")
    shutil.rmtree(checkout)
    assert _run_shim(shim, env) == ""  # nothing installed: prints nothing, never fails


def test_summary_recomputed_only_when_events_change(tmp_path, monkeypatch):
    from memgraph import savings

    m = mem(tmp_path)
    calls = []
    real = savings.measure_savings
    monkeypatch.setattr(savings, "measure_savings", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert savings.refresh_summary(tmp_path, m, 60, NOW) is True
    assert savings.refresh_summary(tmp_path, m, 60, NOW + timedelta(minutes=1)) is False
    for kind in ("guard_block", "read_denied", "big_read_steer", "jit_context", "read_retry"):
        savings.append_event(m, {"type": kind})
    assert savings.refresh_summary(tmp_path, m, 60, NOW + timedelta(minutes=2)) is False  # no fresh start
    savings.append_event(m, {"type": "fresh_start", "session": "s9"})
    assert savings.refresh_summary(tmp_path, m, 60, NOW + timedelta(minutes=3)) is True
    savings.append_event(m, {"type": "read_denied"})
    assert savings.refresh_summary(tmp_path, m, 60, NOW + timedelta(minutes=4)) is False
    assert len(calls) == 2


def test_prompt_cache_without_expires_at_falls_back_to_watch_json(tmp_path):
    data = sample(tmp_path)
    data["prompt_cache"] = {"hit_ratio": 0.9}
    (mem(tmp_path) / "watch.json").write_text(json.dumps({"deadline": (NOW + timedelta(minutes=7)).isoformat()}),
                                              encoding="utf-8")
    assert "cache 7m" in line(data)


def test_memory_folder_from_project_dir_first(tmp_path, monkeypatch):
    sub = tmp_path / "sub"
    mem(sub)  # a stray folder from before must not win
    (mem(tmp_path) / "summary.json").write_text(json.dumps({"measured_saved_tokens": 5000}), encoding="utf-8")
    data = sample(tmp_path)
    data["workspace"] = {"current_dir": str(sub), "project_dir": str(tmp_path)}
    assert "saved 5k" in line(data)
    data["workspace"] = {"current_dir": str(sub)}
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert "saved 5k" in line(data)


def test_setup_honours_claude_config_dir(home, tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    rc, text = setup()
    assert rc == 0, text
    assert "statusLine" in json.loads((cfg / "settings.json").read_text(encoding="utf-8"))
    assert not settings(home).exists()
