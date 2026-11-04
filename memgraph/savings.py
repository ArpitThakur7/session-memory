"""Measured savings from the plugin's fresh starts, and estimated missed savings from cold restarts.

Two strictly separate kinds of number:
- measured: a fresh start (startup or /clear with a brief) recorded the old chat's
  context size; the new chat's first real reply recorded what it actually read.
  saved = old context - new first-reply context, both read from transcripts.
- estimate: a main chat resumed after its cache expired rebuilt the cache (a real,
  measured cost); what a fresh start would have cost instead is estimated from the
  brief's size (characters // 4), so "could have saved" is an estimate.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memgraph.store import EVENTS_FILE, append_event  # noqa: F401  (re-exported)
from memgraph.transcript import fmt_local, parse_ts
from memgraph.usage import (PRICES_AS_OF, WRITE_1H_FACTOR, WRITE_5M_FACTOR, WRITE_UNKNOWN_FACTOR,
                            Reply, UsageResult, collect_usage, price_for, reply_cost)

DEFAULT_BRIEF_TOKENS = 500  # estimate when no brief size was ever recorded


def read_events(mem_dir: Path) -> list[dict]:
    """Every whole event in events.jsonl; partial or odd lines are skipped."""
    try:
        text = (Path(mem_dir) / EVENTS_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for raw in text.split("\n"):
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("type"), str):
            out.append(obj)
    return out


def _resume_usd(last: Reply | None, tokens: int, event_ts: datetime | None, ttl_minutes: int):
    """(cache state, dollars) of re-reading `tokens` in the old chat instead of starting fresh.

    Cold (more than ttl minutes after its last reply): a cache write at the old model's
    price, with the TTL split of that last reply's writes (none: 1.25 x input).
    Warm: a cache read at the old model's price. Unknown when either time is missing."""
    if last is None or event_ts is None:
        return "unknown", None
    p = price_for(last.model)
    if event_ts - last.ts > timedelta(minutes=ttl_minutes):
        writes = last.cache_write
        factor = ((last.cache_write_5m * WRITE_5M_FACTOR + last.cache_write_1h * WRITE_1H_FACTOR
                   + last.cache_write_unknown * WRITE_UNKNOWN_FACTOR) / writes) if writes else WRITE_UNKNOWN_FACTOR
        return "cold", None if p is None else tokens * p["input"] * factor / 1e6
    return "warm", None if p is None else tokens * p["cache_read"] / 1e6


def _rebuild_usd(reply: Reply) -> float | None:
    p = price_for(reply.model)
    if p is None:
        return None
    return (reply.cache_write_5m * WRITE_5M_FACTOR + reply.cache_write_1h * WRITE_1H_FACTOR
            + reply.cache_write_unknown * WRITE_UNKNOWN_FACTOR) * p["input"] / 1e6


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def measure_savings(transcripts_dir: Path, mem_dir: Path, ttl_minutes: int,
                    usage: UsageResult | None = None) -> dict:
    """Measured fresh-start savings and estimated missed savings from cold restarts.

    Dollar values use PRICES_AS_OF prices. A fresh start's saving is what resuming the
    old chat would have cost (cache read if its cache was still warm, cache write if
    cold) minus the new chat's first reply; it can be negative. Rebuilds are priced
    as the reply's cache writes."""
    usage = usage if usage is not None else collect_usage(transcripts_dir)
    events = read_events(mem_dir)
    first_reply: dict[str, Reply] = {}
    by_session: dict[str, list[Reply]] = {}
    for r in usage.replies:  # oldest first
        if r.helper:
            continue
        first_reply.setdefault(r.session, r)
        by_session.setdefault(r.session, []).append(r)

    fresh, seen, pending = [], set(), 0
    brief_chars = None
    for ev in events:
        if ev.get("type") != "fresh_start":
            continue
        if _int(ev.get("brief_chars")) is not None:
            brief_chars = ev["brief_chars"]
        sid, prev_context = ev.get("session"), _int(ev.get("prev_context"))
        if not isinstance(sid, str) or sid in seen or prev_context is None:
            continue
        seen.add(sid)
        reply = first_reply.get(sid)
        if reply is None:
            pending += 1  # the new chat has no reply yet
            continue
        saved = max(0, prev_context - reply.context)
        event_ts = parse_ts(ev.get("t"))
        prev_sid = ev.get("prev_session")
        older = [r for r in by_session.get(prev_sid, []) if event_ts is None or r.ts <= event_ts] \
            if isinstance(prev_sid, str) else []
        state, would = _resume_usd(older[-1] if older else None, prev_context, event_ts, ttl_minutes)
        actual_usd = reply_cost(reply)
        fresh.append({"t": ev.get("t"), "session": sid, "prev_session": prev_sid,
                      "prev_context": prev_context, "actual": reply.context, "saved": saved,
                      "cache_state": state, "would_have_usd": would, "actual_usd": actual_usd,
                      "saved_usd": None if would is None or actual_usd is None else would - actual_usd})

    estimate = brief_chars // 4 if brief_chars is not None else DEFAULT_BRIEF_TOKENS
    cold = []
    for sid, replies in by_session.items():
        for prev, nxt in zip(replies, replies[1:]):
            idle = nxt.ts - prev.ts
            if idle <= timedelta(minutes=ttl_minutes) or nxt.cache_write == 0:
                continue
            could = max(0, nxt.cache_write - estimate)
            usd = _rebuild_usd(nxt)
            cold.append({"t": nxt.ts.isoformat(), "session": sid, "idle_minutes": int(idle.total_seconds() // 60),
                         "rebuild_tokens": nxt.cache_write, "rebuild_usd": usd,
                         "with_plugin_estimate": estimate, "could_have_saved_estimate": could,
                         "could_have_saved_estimate_usd": None if usd is None else usd * could / nxt.cache_write})
    cold.sort(key=lambda r: r["t"])

    def usd_sum(rows, key):
        vals = [r[key] for r in rows if r[key] is not None]
        return sum(vals) if vals or not rows else None

    totals = {
        "measured_saved_tokens": sum(r["saved"] for r in fresh),
        "measured_saved_usd": usd_sum(fresh, "saved_usd"),
        "missed_estimate_tokens": sum(r["could_have_saved_estimate"] for r in cold),
        "missed_estimate_usd": usd_sum(cold, "could_have_saved_estimate_usd"),
        "fresh_starts": len(fresh),
        "fresh_starts_pending": pending,
        **{f"fresh_starts_{s}": sum(1 for r in fresh if r["cache_state"] == s) for s in ("cold", "warm", "unknown")},
        "cold_restarts": len(cold),
        "guard_blocks": sum(1 for e in events if e.get("type") == "guard_block"),
    }
    return {"fresh_starts": fresh, "cold_restarts": cold, "totals": totals,
            "ttl_minutes": ttl_minutes, "brief_tokens_estimate": estimate, "prices_as_of": PRICES_AS_OF}


SUMMARY_FILE = "summary.json"
SUMMARY_PENDING_REFRESH_SECONDS = 600


_FRESH_MARK = b'"fresh_start"'


def _fresh_signature(mem_dir: Path, old: dict) -> list[int] | None:
    """[fresh_start events seen, bytes of events.jsonl scanned]; only the new tail is read.

    Read, steer, related-memory and guard events never change the summary, so they must not
    trigger a transcript scan; only a new fresh_start does."""
    path = Path(mem_dir) / EVENTS_FILE
    try:
        size = path.stat().st_size
    except OSError:
        return [0, 0]  # no events yet
    prev = old.get("fresh_events")
    count, start = 0, 0
    if (isinstance(prev, list) and len(prev) == 2 and all(isinstance(v, int) and not isinstance(v, bool) for v in prev)
            and 0 <= prev[1] <= size):
        count, start = prev
    if start < size:
        begin = max(0, start - (len(_FRESH_MARK) - 1))  # a mark cut in two at the old end is still counted once
        try:
            with open(path, "rb") as fh:
                fh.seek(begin)
                count += fh.read(size - begin).count(_FRESH_MARK)
        except OSError:
            return None
    return [count, size]


def refresh_summary(transcripts_dir: Path, mem_dir: Path, ttl_minutes: int, now: datetime) -> bool:
    """Write summary.json (measured-saved totals) for the status line, which must never scan transcripts.

    Recomputed only when a fresh_start event was logged since the last summary, or every 10 minutes
    while a fresh start still waits for its first reply. Returns True when it was recomputed."""
    from memgraph.graph import write_json_atomic
    from memgraph.ingest import load_state

    mem_dir = Path(mem_dir)
    old = load_state(mem_dir / SUMMARY_FILE)
    sig = _fresh_signature(mem_dir, old)
    old_sig = old.get("fresh_events")
    updated = parse_ts(old.get("updated")) if isinstance(old.get("updated"), str) else None
    same = isinstance(old_sig, list) and sig is not None and old_sig[:1] == sig[:1]
    if updated is not None and same:
        if sig != old_sig:
            write_json_atomic(mem_dir / SUMMARY_FILE, dict(old, fresh_events=sig))  # remember the scanned tail
        pending = _int(old.get("fresh_starts_pending")) or 0
        if not pending or (now - updated).total_seconds() < SUMMARY_PENDING_REFRESH_SECONDS:
            return False
    t = measure_savings(transcripts_dir, mem_dir, ttl_minutes)["totals"]
    write_json_atomic(mem_dir / SUMMARY_FILE, {
        "updated": now.isoformat(timespec="seconds"), "fresh_events": sig,
        "measured_saved_tokens": t["measured_saved_tokens"], "measured_saved_usd": t["measured_saved_usd"],
        "fresh_starts": t["fresh_starts"], "fresh_starts_pending": t["fresh_starts_pending"]})
    return True


def _usd(value: float | None) -> str:
    if value is None:
        return "—"
    return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"


SAVED_USD_LABEL = "saved (priced at old chat's cache state)"


def savings_totals_line(res: dict) -> str:
    t = res["totals"]
    return (f"Measured saved: {t['measured_saved_tokens']:,} tokens ({SAVED_USD_LABEL}: "
            f"{_usd(t['measured_saved_usd'])}) over {t['fresh_starts']} fresh starts "
            f"({t.get('fresh_starts_cold', 0)} cold, {t.get('fresh_starts_warm', 0)} warm, "
            f"{t.get('fresh_starts_unknown', 0)} unknown) · Missed (estimate): {t['missed_estimate_tokens']:,} tokens "
            f"({_usd(t['missed_estimate_usd'])}) over {t['cold_restarts']} cold restarts · "
            f"guard blocks {t['guard_blocks']} (prices as of {res['prices_as_of']})")


def format_savings(res: dict) -> str:
    out = ["Measured savings (fresh starts with a brief; both sizes read from transcripts)"]
    if res["fresh_starts"]:
        out += [f"| time | old chat cache | old chat context | new chat first reply | saved | {SAVED_USD_LABEL} |",
                "|---|---|---:|---:|---:|---:|"]
        for r in res["fresh_starts"]:
            out.append(f"| {fmt_local(r['t'])} | {r['cache_state']} | {r['prev_context']:,} | {r['actual']:,} | "
                       f"{r['saved']:,} | {_usd(r['saved_usd'])} |")
    else:
        out.append("No fresh starts recorded yet (they are logged from now on).")
    if res["totals"].get("fresh_starts_pending"):
        out.append(f"{res['totals']['fresh_starts_pending']} fresh start(s) waiting for their first reply.")
    out += ["", f"Missed savings (estimate): chats resumed after more than {res['ttl_minutes']} idle minutes "
                f"rebuilt their cache (measured); a fresh start is estimated at {res['brief_tokens_estimate']:,} tokens"]
    if res["cold_restarts"]:
        out += ["| time | idle min | rebuild tokens | rebuild $ | with plugin (estimate) | could have saved (estimate) |",
                "|---|---:|---:|---:|---:|---:|"]
        for r in res["cold_restarts"]:
            out.append(f"| {fmt_local(r['t'])} | {r['idle_minutes']:,} | {r['rebuild_tokens']:,} | "
                       f"{_usd(r['rebuild_usd'])} | {r['with_plugin_estimate']:,} | "
                       f"{r['could_have_saved_estimate']:,} |")
    else:
        out.append("No cold restarts found.")
    out += ["", savings_totals_line(res),
            "Fresh-start dollars: what resuming the old chat would have cost (a cache read when its cache was "
            "warm, a cache write when cold, at the old chat's model price) minus the new chat's first reply. "
            "Rebuild dollars: the reply's cache writes."]
    return "\n".join(out)


_MAP_DAYS = 14
_MAP_ROWS = 20
TAPE_CAP = 5000


def tape_view(usage: UsageResult, sav: dict, ttl_minutes: int, cap: int = TAPE_CAP) -> dict:
    """The map's session tape: numbers only, never transcript text.

    ticks: one [time ISO, context tokens, cache-write tokens, dollars or None] per real
    main-chat reply (helpers left out), oldest first, the most recent `cap` kept.
    restarts / fresh_starts: from measure_savings, sessions in the map's "session:<id>" form.
    replies_by_session: main-chat reply count per session (all replies, not only the kept ones)."""
    main = [r for r in usage.replies if not r.helper]
    kept = main[-cap:] if cap > 0 else []
    ticks = [[r.ts.isoformat(), r.context, r.cache_write, reply_cost(r)] for r in kept]
    restarts = [{"t": r["t"], "session": f"session:{r['session']}", "idle_minutes": r["idle_minutes"],
                 "rebuild_tokens": r["rebuild_tokens"], "rebuild_usd": r["rebuild_usd"]}
                for r in sav.get("cold_restarts", [])]
    fresh = [{"t": f["t"], "session": f"session:{f['session']}", "saved": f["saved"],
              "cache_state": f["cache_state"]}
             for f in sav.get("fresh_starts", []) if isinstance(f.get("t"), str)]
    per_session: dict[str, int] = {}
    for r in main:
        key = f"session:{r.session}"
        per_session[key] = per_session.get(key, 0) + 1
    return {"available": True, "ttl_minutes": ttl_minutes, "ticks": ticks,
            "truncated": len(main) - len(kept), "restarts": restarts, "fresh_starts": fresh,
            "replies_by_session": per_session}


def token_view(transcripts_dir: Path | None, mem_dir: Path, now: datetime | None = None,
               ttl_minutes: int | None = None) -> dict:
    """The map's "Tokens" data: last 14 days by day (local dates, empty days included),
    by model and by who, their totals, and the savings totals with the latest rows."""
    from memgraph.transcript import local_time
    from memgraph.usage import helper_rows, summarize, totals

    if transcripts_dir is None or not Path(transcripts_dir).is_dir():
        return {"available": False, "reason": "No Claude Code transcripts found for this project."}
    usage = collect_usage(transcripts_dir)
    if not usage.replies:
        return {"available": False, "reason": "No replies with token usage found in this project's transcripts."}
    now = now or datetime.now(timezone.utc)
    today = local_time(now).date()
    first_day = today - timedelta(days=_MAP_DAYS - 1)
    start = local_time(now).replace(year=first_day.year, month=first_day.month, day=first_day.day,
                                    hour=0, minute=0, second=0, microsecond=0)
    recent = [r for r in usage.replies if r.ts >= start]
    by_day = {r["key"]: r for r in summarize(recent, "day")}
    days = []
    for i in range(_MAP_DAYS):
        key = (first_day + timedelta(days=i)).isoformat()
        days.append(by_day.get(key) or {"key": key, **totals([]), "cost": 0.0})
    if ttl_minutes is None:
        from memgraph.watch import ttl_minutes as default_ttl
        ttl_minutes = default_ttl()
    sav = measure_savings(transcripts_dir, mem_dir, ttl_minutes, usage)
    return {
        "available": True,
        "prices_as_of": PRICES_AS_OF,
        "since": start.isoformat(),
        "duplicates": usage.duplicates,
        "totals": totals(recent),
        "days": days,
        "models": summarize(recent, "model"),
        "who": summarize(recent, "who"),
        "helpers": helper_rows([r for r in recent if r.helper], transcripts_dir, limit=10),
        "savings": {"totals": sav["totals"], "fresh_starts": sav["fresh_starts"][-_MAP_ROWS:],
                    "cold_restarts": sav["cold_restarts"][-_MAP_ROWS:]},
        "tape": tape_view(usage, sav, ttl_minutes),
    }
