import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from memgraph import watch
from memgraph.graph import write_json_atomic
from memgraph.watch import arm, deadline, notify, pid_alive, run_watcher, ttl_minutes
from tests.helpers import graph_from_fixture

T0 = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


def write_transcript(path, last_reply: datetime):
    line = {
        "type": "assistant", "sessionId": "s1", "timestamp": last_reply.isoformat().replace("+00:00", "Z"),
        "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 50000, "output_tokens": 5}},
    }
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return path


class FakeSpawn:
    def __init__(self):
        self.calls = []

    def __call__(self, project):
        self.calls.append(project)


class FakeTime:
    """Fake clock + sleep; `on_sleep` hooks run after each sleep (to edit watch.json mid-loop)."""

    def __init__(self, start: datetime):
        self.now = start.timestamp()
        self.sleeps = 0
        self.on_sleep = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1
        for hook in list(self.on_sleep):
            hook(self)


class FakeNotify:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, title, message):
        self.calls.append((title, message))
        return self.ok


def setup_mem(tmp_path, with_graph=True):
    project = tmp_path / "proj"
    mem = project / ".claude" / "session-memory"
    mem.mkdir(parents=True)
    if with_graph:
        graph_from_fixture().save(mem / "graph.json")
    return project, mem


def write_watch(mem, dl: datetime, **extra):
    write_json_atomic(mem / "watch.json", {"deadline": dl.isoformat(), "transcript": "t", "project": "p", **extra})


def test_deadline_math():
    assert deadline(T0, 60) == T0 + timedelta(minutes=60)
    assert deadline(T0, 5) == T0 + timedelta(minutes=5)


def test_ttl_env_override_and_bad_value(monkeypatch):
    monkeypatch.delenv("SESSION_MEMORY_CACHE_TTL_MIN", raising=False)
    assert ttl_minutes() == 60
    monkeypatch.setenv("SESSION_MEMORY_CACHE_TTL_MIN", "5")
    assert ttl_minutes() == 5
    for bad in ("abc", "", "-3", "0"):
        monkeypatch.setenv("SESSION_MEMORY_CACHE_TTL_MIN", bad)
        assert ttl_minutes() == 60


def test_arm_writes_deadline_and_spawns_once(tmp_path, monkeypatch):
    monkeypatch.delenv("SESSION_MEMORY_CACHE_TTL_MIN", raising=False)
    project, mem = setup_mem(tmp_path, with_graph=False)
    tr = write_transcript(tmp_path / "t.jsonl", T0)
    spawn = FakeSpawn()
    arm(mem, tr, project, T0 + timedelta(seconds=5), spawn=spawn)
    data = json.loads((mem / "watch.json").read_text(encoding="utf-8"))
    assert datetime.fromisoformat(data["deadline"]) == T0 + timedelta(minutes=60)
    assert data["transcript"] == str(tr) and data["project"] == str(project)
    assert spawn.calls == [project]
    # a live watcher holds the lock: only the deadline is refreshed, no second spawn
    (mem / "watch.lock").write_text(str(os.getpid()), encoding="utf-8")
    write_transcript(tr, T0 + timedelta(minutes=3))
    arm(mem, tr, project, T0 + timedelta(minutes=3, seconds=5), spawn=spawn)
    data = json.loads((mem / "watch.json").read_text(encoding="utf-8"))
    assert datetime.fromisoformat(data["deadline"]) == T0 + timedelta(minutes=63)
    assert len(spawn.calls) == 1


def test_arm_skips_spawn_without_reply_or_when_already_notified(tmp_path):
    project, mem = setup_mem(tmp_path, with_graph=False)
    spawn = FakeSpawn()
    arm(mem, tmp_path / "missing.jsonl", project, T0, spawn=spawn)  # no reply time: nothing to watch
    assert spawn.calls == [] and not (mem / "watch.json").exists()
    tr = write_transcript(tmp_path / "t.jsonl", T0)
    write_watch(mem, T0 + timedelta(minutes=60), notified_for=(T0 + timedelta(minutes=60)).isoformat())
    arm(mem, tr, project, T0, spawn=spawn)  # same deadline already warned about: at most once
    assert spawn.calls == []
    assert json.loads((mem / "watch.json").read_text(encoding="utf-8"))["notified_for"]


def test_arm_spawns_even_when_warning_time_passed(tmp_path, monkeypatch):
    monkeypatch.delenv("SESSION_MEMORY_CACHE_TTL_MIN", raising=False)
    project, mem = setup_mem(tmp_path, with_graph=False)
    tr = write_transcript(tmp_path / "t.jsonl", T0)
    spawn = FakeSpawn()
    arm(mem, tr, project, T0 + timedelta(minutes=55), spawn=spawn)  # only a live watcher prevents a spawn
    arm(mem, tr, project, T0 + timedelta(minutes=90), spawn=spawn)
    assert len(spawn.calls) == 2


def test_stale_lock_replaced(tmp_path, monkeypatch):
    project, mem = setup_mem(tmp_path, with_graph=False)
    tr = write_transcript(tmp_path / "t.jsonl", T0)
    dead = 424242
    monkeypatch.setattr(watch, "pid_alive", lambda pid: pid == os.getpid())
    (mem / "watch.lock").write_text(str(dead), encoding="utf-8")
    spawn = FakeSpawn()
    arm(mem, tr, project, T0, spawn=spawn)
    assert spawn.calls == [project]
    # garbage in the lock is stale too
    (mem / "watch.lock").write_text("not a pid", encoding="utf-8")
    arm(mem, tr, project, T0, spawn=spawn)
    assert len(spawn.calls) == 2
    # the watcher itself takes over a stale lock and writes its own pid
    (mem / "watch.lock").write_text(str(dead), encoding="utf-8")
    ft = FakeTime(T0)
    seen = []
    ft.on_sleep.append(lambda _ft: seen.append((mem / "watch.lock").read_text(encoding="utf-8").strip()))
    write_watch(mem, T0 + timedelta(minutes=11))
    run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep, notify=FakeNotify())
    assert seen and seen[0] == str(os.getpid())


def test_watcher_exits_when_live_lock_held_by_other(tmp_path, monkeypatch):
    project, mem = setup_mem(tmp_path)
    write_watch(mem, T0 + timedelta(minutes=11))
    other = 424242
    monkeypatch.setattr(watch, "pid_alive", lambda pid: pid == other)
    (mem / "watch.lock").write_text(str(other), encoding="utf-8")
    ft, nt = FakeTime(T0), FakeNotify()
    assert run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep, notify=nt) == "duplicate"
    assert nt.calls == [] and (mem / "watch.lock").read_text(encoding="utf-8") == str(other)


def test_lock_stale_by_mtime_is_replaced(tmp_path, monkeypatch):
    project, mem = setup_mem(tmp_path, with_graph=False)
    tr = write_transcript(tmp_path / "t.jsonl", T0)
    monkeypatch.setattr(watch, "pid_alive", lambda pid: True)  # pid "runs" (e.g. reused), but no heartbeat
    lock = mem / "watch.lock"
    lock.write_text("424242", encoding="utf-8")
    old = lock.stat().st_mtime - 20 * 60
    os.utime(lock, (old, old))
    spawn = FakeSpawn()
    arm(mem, tr, project, T0, spawn=spawn)
    assert spawn.calls == [project]
    os.utime(lock, None)  # fresh heartbeat: live
    arm(mem, tr, project, T0, spawn=spawn)
    assert len(spawn.calls) == 1


def test_stale_lock_unlink_failure_does_not_crash_watcher(tmp_path, monkeypatch):
    project, mem = setup_mem(tmp_path)
    write_watch(mem, T0 + timedelta(minutes=11))
    (mem / "watch.lock").write_text("424242", encoding="utf-8")
    monkeypatch.setattr(watch, "pid_alive", lambda pid: False)

    def locked(path, *a, **k):
        raise PermissionError(32, "sharing violation")

    monkeypatch.setattr(watch.os, "unlink", locked)
    ft, nt = FakeTime(T0), FakeNotify()
    assert run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep, notify=nt) == "duplicate"
    assert nt.calls == []


def test_watcher_notifies_at_minus_10_and_builds_packs(tmp_path):
    project, mem = setup_mem(tmp_path)
    dl = T0 + timedelta(minutes=60)
    write_watch(mem, dl)
    ft, nt = FakeTime(T0), FakeNotify()
    notified_at = []
    orig = nt.__call__

    def spy(title, message):
        notified_at.append(ft.now)
        assert (mem / "resume-pack.md").exists()  # pack built before the user is told
        return orig(title, message)

    assert run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep, notify=spy) == "notified"
    assert len(nt.calls) == 1
    title, message = nt.calls[0]
    assert message == "Claude cache expires in about 10 min. Resume pack ready — type /clear when you come back."
    warn = (dl - timedelta(minutes=10)).timestamp()
    assert warn <= notified_at[0] < warn + 31  # polled every 30 s
    assert (mem / "resume-pack.md").exists()
    data = json.loads((mem / "watch.json").read_text(encoding="utf-8"))
    assert data["notified_for"] == data["deadline"]
    pytest.importorskip("PIL")
    assert (mem / "memory.png").exists()


def test_watcher_never_notifies_twice_for_same_deadline(tmp_path):
    project, mem = setup_mem(tmp_path)
    dl = T0 + timedelta(minutes=5)
    write_watch(mem, dl, notified_for=dl.isoformat())
    ft, nt = FakeTime(T0), FakeNotify()
    assert run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep, notify=nt) == "idle"
    assert nt.calls == []


def test_missed_deadline_builds_packs_and_says_expired(tmp_path):
    project, mem = setup_mem(tmp_path)
    dl = T0 - timedelta(minutes=1)  # e.g. the computer slept through the warning
    write_watch(mem, dl)
    ft, nt = FakeTime(T0), FakeNotify()
    assert run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep, notify=nt) == "expired"
    assert [m for _t, m in nt.calls] == [
        "Claude cache has expired. Resume pack ready — type /clear when you come back."]
    assert (mem / "resume-pack.md").exists()
    assert json.loads((mem / "watch.json").read_text(encoding="utf-8"))["notified_for"] == dl.isoformat()


def test_short_ttl_warns_at_half_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_MEMORY_CACHE_TTL_MIN", "5")
    project, mem = setup_mem(tmp_path)
    tr = write_transcript(tmp_path / "t.jsonl", T0)
    spawn = FakeSpawn()
    arm(mem, tr, project, T0 + timedelta(seconds=5), spawn=spawn)
    assert spawn.calls == [project]
    dl = T0 + timedelta(minutes=5)
    ft, nt = FakeTime(T0 + timedelta(seconds=5)), FakeNotify()
    at = []
    assert run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep,
                       notify=lambda t, m: at.append(ft.now) or nt(t, m)) == "notified"
    warn = (dl - timedelta(minutes=2)).timestamp()
    assert warn <= at[0] < warn + 31
    assert nt.calls[0][1] == "Claude cache expires in about 2 min. Resume pack ready — type /clear when you come back."


def test_warn_lead_minutes():
    from memgraph.watch import warn_lead_minutes
    assert [warn_lead_minutes(t) for t in (60, 20, 11, 5, 3, 2, 1)] == [10, 10, 5, 2, 1, 1, 1]


def test_reply_during_notify_is_not_lost(tmp_path):
    project, mem = setup_mem(tmp_path)
    dl1 = T0 + timedelta(minutes=10)
    write_watch(mem, dl1)
    dl2 = T0 + timedelta(minutes=70)
    ft, nt = FakeTime(T0), FakeNotify()
    at = []

    def notify_then_reply(title, message):
        at.append(ft.now)
        if len(at) == 1:
            write_watch(mem, dl2)  # what arm() does when a reply lands now
        return nt(title, message)

    assert run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep, notify=notify_then_reply) == "notified"
    assert len(at) == 2 and at[1] >= (dl2 - timedelta(minutes=10)).timestamp()
    data = json.loads((mem / "watch.json").read_text(encoding="utf-8"))
    assert data["deadline"] == data["notified_for"] == dl2.isoformat()


def test_mark_notified_leaves_moved_deadline_alone(tmp_path):
    from memgraph.watch import _mark_notified
    _project, mem = setup_mem(tmp_path, with_graph=False)
    dl1, dl2 = T0, T0 + timedelta(minutes=30)
    write_watch(mem, dl2)
    _mark_notified(mem, dl1.isoformat())
    assert "notified_for" not in json.loads((mem / "watch.json").read_text(encoding="utf-8"))
    _mark_notified(mem, dl2.isoformat())
    assert json.loads((mem / "watch.json").read_text(encoding="utf-8"))["notified_for"] == dl2.isoformat()


def test_new_activity_moves_deadline(tmp_path):
    project, mem = setup_mem(tmp_path)
    write_watch(mem, T0 + timedelta(minutes=60))
    ft, nt = FakeTime(T0), FakeNotify()
    later = T0 + timedelta(minutes=40)
    new_dl = later + timedelta(minutes=60)

    def reply_at_40(_ft):
        if _ft.now >= later.timestamp() and json.loads((mem / "watch.json").read_text(encoding="utf-8"))["deadline"] != new_dl.isoformat():
            write_watch(mem, new_dl)

    ft.on_sleep.append(reply_at_40)
    notified_at = []

    def spy(title, message):
        notified_at.append(ft.now)
        return nt(title, message)

    assert run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep, notify=spy) == "notified"
    assert len(nt.calls) == 1
    assert notified_at[0] >= (new_dl - timedelta(minutes=10)).timestamp()


def test_idle_exit_after_2h(tmp_path):
    project, mem = setup_mem(tmp_path)  # no watch.json at all
    ft, nt = FakeTime(T0), FakeNotify()
    assert run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep, notify=nt) == "idle"
    assert nt.calls == []
    elapsed = ft.now - T0.timestamp()
    assert 2 * 3600 <= elapsed < 2 * 3600 + 31


def test_lock_removed_on_exit(tmp_path):
    project, mem = setup_mem(tmp_path)
    write_watch(mem, T0 + timedelta(minutes=11))
    ft = FakeTime(T0)
    run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep, notify=FakeNotify())
    assert not (mem / "watch.lock").exists()

    def boom(title, message):
        raise RuntimeError("notifier exploded")

    write_watch(mem, T0 + timedelta(minutes=11))
    ft = FakeTime(T0)
    with pytest.raises(RuntimeError):
        run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep, notify=boom)
    assert not (mem / "watch.lock").exists()


def test_notify_never_raises():
    def missing(*a, **k):
        raise FileNotFoundError("no such command")

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("cmd", 10)

    def failing(*a, **k):
        return subprocess.CompletedProcess(a, 1)

    for platform in ("win32", "darwin", "linux"):
        for run in (missing, timeout, failing):
            assert notify("t", "m", platform=platform, run=run) is False


def test_notify_passes_text_out_of_band():
    seen = []

    def ok(cmd, **kw):
        seen.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0)

    evil = "x'; Remove-Item -Recurse C:\\ ; \"$(rm -rf /)\" `whoami`"
    assert notify(evil, evil, platform="win32", run=ok) is True
    cmd, kw = seen[-1]
    assert evil not in " ".join(cmd) and kw["env"]["SESSION_MEMORY_TOAST_TITLE"] == evil
    assert kw["timeout"] == 10 and "-NoProfile" in cmd and "-NonInteractive" in cmd
    assert notify(evil, evil, platform="darwin", run=ok) is True
    cmd, kw = seen[-1]
    assert cmd[-2:] == [evil, evil] and all(evil not in part for part in cmd[:-2])
    assert notify(evil, evil, platform="linux", run=ok) is True
    assert seen[-1][0][-2:] == [evil, evil]


def test_pid_alive():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(999_999_996) is False  # far above any real pid on Windows/Linux/macOS
    assert pid_alive(0) is False and pid_alive(-1) is False


def test_spawn_command_is_detached_python(monkeypatch, tmp_path):
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls.append((cmd, kw))

    monkeypatch.setattr(watch.subprocess, "Popen", FakePopen)
    from memgraph.watch import _spawn_process
    _spawn_process(tmp_path)
    cmd, kw = calls[0]
    assert cmd == [sys.executable, "-m", "memgraph.cli", "watch", "--project", str(tmp_path)]
    root = os.path.dirname(os.path.dirname(os.path.abspath(watch.__file__)))
    assert kw["cwd"] == root and kw["env"]["PYTHONPATH"].split(os.pathsep)[0] == root
    assert kw["stdin"] is kw["stdout"] is kw["stderr"] is subprocess.DEVNULL
    if sys.platform == "win32":
        assert kw["creationflags"] & 0x00000008 and kw["creationflags"] & 0x08000000 and kw["close_fds"]
    else:
        assert kw["start_new_session"] is True


def test_hook_stop_arms_watcher(tmp_path, monkeypatch):
    from tests.test_cli import hook_input, run

    spawn = FakeSpawn()
    monkeypatch.setattr(watch, "_spawn", spawn)
    monkeypatch.delenv("SESSION_MEMORY_CACHE_TTL_MIN", raising=False)
    now = datetime(2026, 11, 1, 10, 1, 0, tzinfo=timezone.utc)  # fixture's last reply is 10:00:30
    assert run(["hook-stop"], hook_input(tmp_path), now=now) == ""
    mem = tmp_path / "proj/.claude/session-memory"
    data = json.loads((mem / "watch.json").read_text(encoding="utf-8"))
    assert datetime.fromisoformat(data["deadline"]) == datetime(2026, 11, 1, 11, 0, 30, tzinfo=timezone.utc)
    assert spawn.calls == [tmp_path / "proj"]
    assert not (mem / "error.log").exists()


def test_watch_cli_runs_watcher_and_never_raises(tmp_path, monkeypatch):
    import io
    from memgraph.cli import main

    seen = []
    monkeypatch.setattr(watch, "run_watcher", lambda mem, project: seen.append((mem, project)) or "idle")
    assert main(["watch", "--project", str(tmp_path)], io.StringIO(""), io.StringIO()) == 0
    assert seen == [(tmp_path / ".claude" / "session-memory", tmp_path)]

    def boom(mem, project):
        raise RuntimeError("x")

    monkeypatch.setattr(watch, "run_watcher", boom)
    out = io.StringIO()
    assert main(["watch", "--project", str(tmp_path)], io.StringIO(""), out) == 0
    assert out.getvalue() == "" and (tmp_path / ".claude/session-memory/error.log").exists()


def test_build_packs_never_writes_an_empty_pack(tmp_path):   # I1
    project, mem = setup_mem(tmp_path, with_graph=False)
    (mem / "resume-pack.md").write_text("stale", encoding="utf-8")
    watch._build_packs(mem, project)
    assert not (mem / "resume-pack.md").exists() and not (mem / "error.log").exists()


def test_long_turn_moves_deadline_instead_of_warning(tmp_path, monkeypatch):   # I2
    monkeypatch.delenv("SESSION_MEMORY_CACHE_TTL_MIN", raising=False)
    project, mem = setup_mem(tmp_path)
    tr = write_transcript(tmp_path / "t.jsonl", T0)
    dl1 = T0 + timedelta(minutes=60)
    write_json_atomic(mem / "watch.json", {"deadline": dl1.isoformat(), "warn_min": 7,
                                           "transcript": str(tr), "project": str(project)})
    ft, nt = FakeTime(T0), FakeNotify()
    at = []

    def keep_working(t):  # the model is still busy inside one long turn: no Stop hook yet
        if ft.now < (T0 + timedelta(minutes=40)).timestamp():
            write_transcript(tr, datetime.fromtimestamp(ft.now, timezone.utc))

    ft.on_sleep.append(keep_working)
    assert run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep,
                       notify=lambda t, m: at.append(ft.now) or nt(t, m)) == "notified"
    assert len(at) == 1
    data = json.loads((mem / "watch.json").read_text(encoding="utf-8"))
    dl2 = datetime.fromisoformat(data["deadline"])
    assert dl2 > dl1 + timedelta(minutes=30) and data["warn_min"] == 7
    warn = (dl2 - timedelta(minutes=7)).timestamp()
    assert warn <= at[0] < warn + 31
    assert data["notified_for"] == data["deadline"]


def test_spawn_env_and_cwd_can_import_cli_from_a_foreign_cwd(monkeypatch, tmp_path):   # M11
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls.append((cmd, kw))

    monkeypatch.chdir(tmp_path)  # the hook runs in the user's project, not in this repo
    monkeypatch.delenv("PYTHONPATH", raising=False)
    real_popen = subprocess.Popen
    monkeypatch.setattr(watch.subprocess, "Popen", FakePopen)
    watch._spawn_process(tmp_path)
    monkeypatch.setattr(watch.subprocess, "Popen", real_popen)
    _cmd, kw = calls[0]
    for cwd in (kw["cwd"], str(tmp_path)):  # also proves PYTHONPATH alone is enough
        r = subprocess.run([sys.executable, "-c", "import memgraph.cli"], env=kw["env"], cwd=cwd,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")


def test_stop_arms_watcher_then_session_start_points_at_fresh_packs(tmp_path, monkeypatch):   # M11
    import io as _io

    from memgraph.cli import main
    from tests.test_cli import hook_input

    monkeypatch.delenv("SESSION_MEMORY_CACHE_TTL_MIN", raising=False)
    spawn = FakeSpawn()
    monkeypatch.setattr(watch, "_spawn", spawn)
    payload = hook_input(tmp_path)
    project, mem = tmp_path / "proj", tmp_path / "proj/.claude/session-memory"
    stop_at = datetime(2026, 11, 1, 10, 1, 0, tzinfo=timezone.utc)  # fixture's last reply is 10:00:30
    assert main(["hook-stop"], _io.StringIO(payload), _io.StringIO(), now=stop_at) == 0
    assert spawn.calls == [project]

    ft, nt = FakeTime(stop_at), FakeNotify()
    assert run_watcher(mem, project, clock=ft.clock, sleep=ft.sleep, notify=nt) == "notified"
    assert len(nt.calls) == 1 and (mem / "resume-pack.md").exists()

    out = _io.StringIO()
    assert main(["hook-session-start"], _io.StringIO(payload), out) == 0
    brief = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert "Full resume pack: .claude/session-memory/resume-pack.md" in brief
    assert "Add login page" in (mem / "resume-pack.md").read_text(encoding="utf-8")
    if (mem / "memory.png").exists():  # Pillow installed: the picture is as new as the pack
        assert "Visual summary: open .claude/session-memory/memory.png" in brief
    assert not (mem / "error.log").exists()


def test_extend_for_activity_keeps_watching_when_deadline_moved(tmp_path):
    project, mem = setup_mem(tmp_path)
    old, new = T0 + timedelta(minutes=60), T0 + timedelta(minutes=120)
    write_json_atomic(mem / "watch.json", {"deadline": new.isoformat(), "transcript": "x"})
    assert watch._extend_for_activity(mem, old.isoformat(), old) is True
