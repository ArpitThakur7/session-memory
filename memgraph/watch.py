"""Cache watcher: 10 minutes before the prompt cache expires, build resume packs and tell the user.

`arm` (called from the Stop hook) records the deadline in watch.json and starts one detached
watcher per project. The watcher polls watch.json every 30 s, so a newer reply simply moves the
deadline. It warns at most once per deadline, then exits; with no deadline for 2 h it exits too.
It never calls a model and never touches the chat: only local files and a desktop notification.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memgraph.graph import Graph, write_json_atomic
from memgraph.guard import last_activity
from memgraph.ingest import load_state
from memgraph.pack import write_pack
from memgraph.transcript import parse_ts
from memgraph.visual import IMAGE_FILE, has_story, render_visual_pack

WATCH_FILE = "watch.json"
LOCK_FILE = "watch.lock"
POLL_SECONDS = 30
MAX_LEAD_MINUTES = 10
IDLE_EXIT_SECONDS = 2 * 3600
LOCK_STALE_SECONDS = 10 * 60  # the watcher touches its lock every poll; older means it died or slept
NOTIFY_TIMEOUT = 10
TITLE = "Session memory"
MESSAGE = "Claude cache expires in about {lead} min. Resume pack ready — type /clear when you come back."
EXPIRED_MESSAGE = "Claude cache has expired. Resume pack ready — type /clear when you come back."

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000

_POWERSHELL_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
# Text arrives through environment variables only; nothing user-controlled is interpolated here.
_TOAST_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; try { "
    "[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]; "
    "[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]; "
    "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
    "$t = $x.GetElementsByTagName('text'); "
    "[void]$t.Item(0).AppendChild($x.CreateTextNode($env:SESSION_MEMORY_TOAST_TITLE)); "
    "[void]$t.Item(1).AppendChild($x.CreateTextNode($env:SESSION_MEMORY_TOAST_MESSAGE)); "
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:SESSION_MEMORY_TOAST_APPID)"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($x)) "
    "} catch { exit 1 }"
)


def ttl_minutes() -> int:
    """Cache lifetime in minutes: env SESSION_MEMORY_CACHE_TTL_MIN, default (and on bad values) 60."""
    try:
        value = int(os.environ.get("SESSION_MEMORY_CACHE_TTL_MIN", ""))
    except ValueError:
        return 60
    return value if value > 0 else 60


def deadline(last_reply: datetime, ttl: int) -> datetime:
    return last_reply + timedelta(minutes=ttl)


def warn_lead_minutes(ttl: int) -> int:
    """How long before the deadline to warn: min(10, ttl // 2) minutes, at least 1."""
    return max(1, min(MAX_LEAD_MINUTES, ttl // 2))


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def pid_alive(pid: int) -> bool:
    """True if a process with this pid is running. Never raises."""
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if sys.platform == "win32":
            return _pid_alive_windows(pid)
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # exists, owned by someone else
    except Exception:
        return False


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _read_lock(lock: Path) -> int | None:
    try:
        return int(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _lock_live(lock: Path) -> bool:
    """A lock counts only if its pid runs and its owner touched it recently."""
    pid = _read_lock(lock)
    if pid is None or not pid_alive(pid):
        return False
    try:
        return time.time() - lock.stat().st_mtime < LOCK_STALE_SECONDS
    except OSError:
        return False


def _acquire(lock: Path) -> bool:
    for _ in range(3):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _read_lock(lock) != os.getpid() and _lock_live(lock):
                return False
            try:
                os.unlink(lock)  # stale: its watcher is gone
            except OSError:
                pass  # gone already, or held open (Windows sharing violation): retry, then give up
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


def _owns(lock: Path) -> bool:
    return _read_lock(lock) == os.getpid()


def _release(lock: Path) -> None:
    try:
        if _owns(lock):
            lock.unlink()
    except OSError:
        pass


def _spawn_process(project: Path) -> None:
    """Start `python -m memgraph.cli watch --project <project>` fully detached, no window, no pipes."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    kwargs: dict = {
        "cwd": root,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "memgraph.cli", "watch", "--project", str(project)], **kwargs)


_spawn = _spawn_process


def arm(mem_dir: Path, transcript_path: Path, project: Path, now: datetime, spawn=None) -> None:
    """Record the cache deadline for the newest reply and make sure one watcher is running."""
    mem_dir = Path(mem_dir)
    last, _context = last_activity(Path(transcript_path))
    if last is None:
        return
    ttl = ttl_minutes()
    dl = deadline(_utc(last), ttl)
    data = {"deadline": dl.isoformat(), "warn_min": warn_lead_minutes(ttl),
            "transcript": str(transcript_path), "project": str(project)}
    notified_for = load_state(mem_dir / WATCH_FILE).get("notified_for")
    if isinstance(notified_for, str):
        data["notified_for"] = notified_for  # at most one warning per deadline, across re-arms
    write_json_atomic(mem_dir / WATCH_FILE, data)
    if notified_for == data["deadline"]:
        return  # this deadline was already announced; nothing left to watch
    if _lock_live(mem_dir / LOCK_FILE):
        return  # the running watcher picks up the new deadline itself
    (spawn or _spawn)(Path(project))


def _mark_notified(mem_dir: Path, deadline_text: str) -> None:
    """Record the announced deadline, unless a reply has meanwhile moved it (then leave the file alone)."""
    data = load_state(mem_dir / WATCH_FILE)
    if data.get("deadline") != deadline_text:
        return
    data["notified_for"] = deadline_text
    write_json_atomic(mem_dir / WATCH_FILE, data)


def _pending(mem_dir: Path) -> tuple[str, datetime, int] | None:
    """(deadline text, deadline, warning lead in minutes) if watch.json holds an unannounced deadline."""
    data = load_state(mem_dir / WATCH_FILE)
    text = data.get("deadline")
    dl = parse_ts(text) if isinstance(text, str) else None
    if dl is None or data.get("notified_for") == text:
        return None
    lead = data.get("warn_min")
    if not isinstance(lead, int) or isinstance(lead, bool) or not 1 <= lead <= MAX_LEAD_MINUTES:
        lead = MAX_LEAD_MINUTES
    return text, _utc(dl), lead


def _log(mem_dir: Path) -> None:
    try:
        with open(mem_dir / "error.log", "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()}\n{traceback.format_exc()}\n")
    except Exception:
        pass


def _build_packs(mem_dir: Path, project: Path) -> None:
    try:
        graph = Graph.load(mem_dir / "graph.json")
        write_pack(graph, mem_dir, "standard", project)
        target = mem_dir / IMAGE_FILE
        if has_story(graph):
            render_visual_pack(graph, target, project)
        else:
            target.unlink(missing_ok=True)
    except Exception:
        _log(mem_dir)  # still warn: the session-start brief works without a pack


def run_watcher(mem_dir: Path, project: Path, clock=time.time, sleep=time.sleep, notify=None) -> str:
    """Poll watch.json until it is time to warn. Returns why it stopped:
    "notified", "expired" (woke after the deadline, e.g. after sleep: packs built, told it expired),
    "idle" (no pending deadline for 2 h), "duplicate" (another watcher holds the lock) or
    "superseded" (it lost the lock). A reply that lands while it warns keeps it watching."""
    mem_dir, project = Path(mem_dir), Path(project)
    notify = notify or globals()["notify"]
    lock = mem_dir / LOCK_FILE
    if not _acquire(lock):
        return "duplicate"
    try:
        idle_since = clock()
        while True:
            if not _owns(lock):
                return "superseded"
            try:
                os.utime(lock)  # heartbeat
            except OSError:
                pass
            now = clock()
            pending = _pending(mem_dir)
            if pending is not None:
                idle_since = now
                text, dl, lead = pending
                due = dl.timestamp()
                if now >= due - lead * 60:
                    outcome = "expired" if now >= due else "notified"
                    _build_packs(mem_dir, project)
                    _mark_notified(mem_dir, text)  # before notifying: a crash must not cause a repeat
                    notify(TITLE, EXPIRED_MESSAGE if outcome == "expired" else MESSAGE.format(lead=lead))
                    if _pending(mem_dir) is not None:
                        continue  # a reply moved the deadline while we warned: keep watching
                    return outcome
            elif now - idle_since >= IDLE_EXIT_SECONDS:
                return "idle"
            sleep(POLL_SECONDS)
    finally:
        _release(lock)


def notify(title: str, message: str, *, platform: str | None = None, run=None) -> bool:
    """Show a desktop notification; False if it could not be shown. Never raises, never blocks > 10 s."""
    platform = platform or sys.platform
    run = run or subprocess.run
    try:
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": NOTIFY_TIMEOUT,
        }
        if platform == "win32":
            cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                   "-Command", _TOAST_SCRIPT]
            kwargs["env"] = dict(
                os.environ,
                SESSION_MEMORY_TOAST_TITLE=title,
                SESSION_MEMORY_TOAST_MESSAGE=message,
                SESSION_MEMORY_TOAST_APPID=_POWERSHELL_AUMID,
            )
            if sys.platform == "win32":
                kwargs["creationflags"] = _CREATE_NO_WINDOW
        elif platform == "darwin":
            cmd = ["osascript", "-e", "on run argv",
                   "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                   "-e", "end run", title, message]
        else:
            cmd = ["notify-send", "--", title, message]
        return run(cmd, **kwargs).returncode == 0
    except Exception:
        return False
