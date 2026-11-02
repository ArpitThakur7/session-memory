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

from memgraph.transcript import fmt_local, parse_ts
from memgraph.usage import (PRICES_AS_OF, WRITE_1H_FACTOR, WRITE_5M_FACTOR, WRITE_UNKNOWN_FACTOR,
                            Reply, UsageResult, collect_usage, price_for)

EVENTS_FILE = "events.jsonl"
DEFAULT_BRIEF_TOKENS = 500  # estimate when no brief size was ever recorded


def append_event(mem_dir: Path, event: dict) -> None:
    """Append one JSON line to events.jsonl with a single O_APPEND write (no lock needed)."""
    obj = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}
    data = (json.dumps(obj, ensure_ascii=True) + "\n").encode("ascii")
    fd = os.open(Path(mem_dir) / EVENTS_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


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


def _write_usd(model: str, tokens: float, factor: float) -> float | None:
    p = price_for(model)
    return None if p is None else tokens * p["input"] * factor / 1e6


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

    Dollar values use PRICES_AS_OF prices of the reply's model; a saved or rebuilt
    token is priced as a cache write (what re-sending it to a cold cache costs)."""
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
        fresh.append({"t": ev.get("t"), "session": sid, "prev_session": ev.get("prev_session"),
                      "prev_context": prev_context, "actual": reply.context, "saved": saved,
                      "saved_usd": _write_usd(reply.model, saved, WRITE_5M_FACTOR)})

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
        "cold_restarts": len(cold),
        "guard_blocks": sum(1 for e in events if e.get("type") == "guard_block"),
    }
    return {"fresh_starts": fresh, "cold_restarts": cold, "totals": totals,
            "ttl_minutes": ttl_minutes, "brief_tokens_estimate": estimate, "prices_as_of": PRICES_AS_OF}


def _usd(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def savings_totals_line(res: dict) -> str:
    t = res["totals"]
    return (f"Measured saved: {t['measured_saved_tokens']:,} tokens ({_usd(t['measured_saved_usd'])}) "
            f"over {t['fresh_starts']} fresh starts · Missed (estimate): {t['missed_estimate_tokens']:,} tokens "
            f"({_usd(t['missed_estimate_usd'])}) over {t['cold_restarts']} cold restarts · "
            f"guard blocks {t['guard_blocks']} (prices as of {res['prices_as_of']})")


def format_savings(res: dict) -> str:
    out = ["Measured savings (fresh starts with a brief; both sizes read from transcripts)"]
    if res["fresh_starts"]:
        out += ["| time | old chat context | new chat first reply | saved | saved $ |", "|---|---:|---:|---:|---:|"]
        for r in res["fresh_starts"]:
            out.append(f"| {fmt_local(r['t'])} | {r['prev_context']:,} | {r['actual']:,} | {r['saved']:,} | "
                       f"{_usd(r['saved_usd'])} |")
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
            "Dollars price saved or rebuilt tokens as cache writes of the reply's model."]
    return "\n".join(out)
