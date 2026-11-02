"""Exact token usage from Claude Code's local transcripts (0 model tokens, nothing leaves the machine).

Every number comes from the `usage` field Claude Code records on each model reply.
Claude Code writes one reply several times while streaming, so replies are
de-duplicated by (message.id, requestId). Synthetic lines (usage-limit notices,
API errors) are not replies and are skipped.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from memgraph.ingest import load_state
from memgraph.transcript import is_synthetic, local_time, parse_ts

# USD per million tokens. Source: Anthropic price list, cached on PRICES_AS_OF.
PRICES_AS_OF = "2026-10-06"
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "claude-fable-5-1": {"input": 10.00, "output": 50.00, "cache_read": 0.25},
    "claude-fable-5": {"input": 10.00, "output": 50.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    "claude-opus-4-7": {"input": 5.00, "output": 25.00},
    "claude-opus-4-6": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}
CACHE_READ_FACTOR = 0.1  # cache read price when not listed: 0.1 x input
WRITE_5M_FACTOR = 1.25
WRITE_1H_FACTOR = 2.0
WRITE_UNKNOWN_FACTOR = 1.25  # assumption: a write without a TTL breakdown is a 5-minute write
UNKNOWN_TTL_NOTE = "cache writes without a TTL breakdown are priced as 5-minute writes (1.25 x input)"

TOKEN_FIELDS = ("input", "cache_write_5m", "cache_write_1h", "cache_write_unknown", "cache_read", "output")
GROUPINGS = ("day", "session", "model", "who")
_DATE_SUFFIX = re.compile(r"-\d{6,}")


@dataclass(frozen=True)
class Reply:
    ts: datetime
    session: str
    model: str
    helper: bool
    input: int
    cache_write_5m: int
    cache_write_1h: int
    cache_write_unknown: int
    cache_read: int
    output: int

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h + self.cache_write_unknown

    @property
    def context(self) -> int:
        """Tokens the model read for this reply: input + cache writes + cache reads."""
        return self.input + self.cache_write + self.cache_read


@dataclass
class UsageResult:
    replies: list[Reply] = field(default_factory=list)
    duplicates: int = 0


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _reply(obj: dict, helper: bool) -> Reply | None:
    """A Reply for a real assistant line with a usage dict, else None."""
    if obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict) or is_synthetic(obj):
        return None
    ts = parse_ts(obj.get("timestamp"))
    if ts is None:
        return None
    u = msg["usage"]
    split = u.get("cache_creation")
    w5 = w1 = unknown = 0
    if isinstance(split, dict) and ("ephemeral_5m_input_tokens" in split or "ephemeral_1h_input_tokens" in split):
        w5, w1 = _int(split.get("ephemeral_5m_input_tokens")), _int(split.get("ephemeral_1h_input_tokens"))
    else:
        unknown = _int(u.get("cache_creation_input_tokens"))
    return Reply(ts=ts, session=str(obj.get("sessionId") or ""), model=str(msg.get("model") or ""),
                 helper=helper or obj.get("isSidechain") is True,
                 input=_int(u.get("input_tokens")), cache_write_5m=w5, cache_write_1h=w1,
                 cache_write_unknown=unknown, cache_read=_int(u.get("cache_read_input_tokens")),
                 output=_int(u.get("output_tokens")))


def _files(root: Path) -> list[tuple[Path, bool]]:
    out: list[tuple[Path, bool]] = []
    for pattern, helper in (("*.jsonl", False), ("*/subagents/*.jsonl", True)):
        try:
            out += [(p, helper) for p in sorted(root.glob(pattern)) if p.is_file()]
        except OSError:
            continue
    return out


def collect_usage(transcripts_dir: Path) -> UsageResult:
    """Every real reply in `<dir>/*.jsonl` and `<dir>/*/subagents/*.jsonl`, oldest first.

    The first line of each (message.id, requestId) wins; later copies are counted in
    `.duplicates`. Never raises on odd lines or unreadable files."""
    result = UsageResult()
    seen: set[tuple[str, str]] = set()
    for path, helper in _files(Path(transcripts_dir)):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().split("\n")
        except OSError:
            continue
        for raw in lines:
            if '"usage"' not in raw:
                continue  # fast path: only lines that can carry usage are parsed
            try:
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    continue
                reply = _reply(obj, helper)
                if reply is None:
                    continue
                msg = obj["message"]
                mid, rid = msg.get("id"), obj.get("requestId")
                if mid or rid:
                    key = (str(mid), str(rid))
                    if key in seen:
                        result.duplicates += 1
                        continue
                    seen.add(key)
                result.replies.append(reply)
            except Exception:
                continue
    result.replies.sort(key=lambda r: r.ts)
    return result


def price_for(model: str) -> dict[str, float] | None:
    """Prices for `model` (USD per million tokens), matched by the longest known prefix.

    A known id may carry a date suffix ("-20251001") or a bracket tag ("[1m]"); any
    other suffix (e.g. a newer version "-1") is a different model, so None."""
    if not isinstance(model, str) or not model:
        return None
    base = model.split("[", 1)[0]
    for key in sorted(PRICES, key=len, reverse=True):
        rest = base[len(key):] if base.startswith(key) else None
        if rest is not None and (rest == "" or _DATE_SUFFIX.fullmatch(rest)):
            p = PRICES[key]
            return {"input": p["input"], "output": p["output"],
                    "cache_read": p.get("cache_read", CACHE_READ_FACTOR * p["input"])}
    return None


def reply_cost(reply: Reply) -> float | None:
    """Dollar cost of one reply at PRICES_AS_OF prices; None for an unknown model."""
    p = price_for(reply.model)
    if p is None:
        return None
    inp = p["input"]
    return (reply.input * inp
            + reply.cache_write_5m * inp * WRITE_5M_FACTOR
            + reply.cache_write_1h * inp * WRITE_1H_FACTOR
            + reply.cache_write_unknown * inp * WRITE_UNKNOWN_FACTOR
            + reply.cache_read * p["cache_read"]
            + reply.output * p["output"]) / 1e6


def _sum(replies: list[Reply]) -> dict:
    row = {f: sum(getattr(r, f) for r in replies) for f in TOKEN_FIELDS}
    row["cache_write"] = row["cache_write_5m"] + row["cache_write_1h"] + row["cache_write_unknown"]
    row["replies"] = len(replies)
    known, unknown, any_known = 0.0, set(), False
    for r in replies:
        c = reply_cost(r)
        if c is None:
            unknown.add(r.model or "(no model)")
        else:
            known += c
            any_known = True
    row["cost"] = known if any_known or not replies else None
    row["cost_partial"] = bool(unknown) and row["cost"] is not None
    row["unknown_models"] = sorted(unknown)
    return row


def _key(reply: Reply, by: str) -> str:
    if by == "day":
        return local_time(reply.ts).strftime("%Y-%m-%d")
    if by == "session":
        return reply.session
    if by == "model":
        return reply.model or "(no model)"
    return "helper" if reply.helper else "main"


def summarize(replies: list[Reply], by: str = "day", since: datetime | None = None) -> list[dict]:
    """One row per group (days in local time, oldest first; others largest first).

    Each row has the token columns, `replies`, `cost` (sum of the known-model part;
    None when no model in the row is known) and `cost_partial`."""
    if by not in GROUPINGS:
        raise ValueError(f"by must be one of {', '.join(GROUPINGS)}")
    groups: dict[str, list[Reply]] = {}
    for r in replies:
        if since is None or r.ts >= since:
            groups.setdefault(_key(r, by), []).append(r)
    rows = [{"key": k, **_sum(v)} for k, v in groups.items()]
    if by == "day":
        rows.sort(key=lambda r: r["key"])
    else:
        rows.sort(key=lambda r: (-(r["input"] + r["cache_write"] + r["cache_read"] + r["output"]), r["key"]))
    return rows


def totals(replies: list[Reply]) -> dict:
    """Token sums, reply count, cost and cache hit rate = cache_read / (input + cache writes + cache_read)."""
    row = _sum(replies)
    read_total = row["input"] + row["cache_write"] + row["cache_read"]
    row["cache_hit_rate"] = row["cache_read"] / read_total if read_total else 0.0
    return row


def project_slug(project: Path) -> str:
    """Claude Code's folder name for a project: the absolute path, every char that is not
    an ASCII letter or digit -> "-"."""
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in str(Path(project).resolve()))


def transcripts_dir_for(project: Path, mem_dir: Path | None) -> Path:
    """Where this project's transcripts live: recorded by the hooks, else derived from the path."""
    if mem_dir is not None:
        for name in ("state.json", "guard.json"):
            recorded = load_state(Path(mem_dir) / name).get("transcripts_dir")
            if isinstance(recorded, str) and recorded:
                return Path(recorded)
    return Path.home() / ".claude" / "projects" / project_slug(project)


# ---------------------------------------------------------------- formatting

def fmt_cost(cost: float | None, partial: bool = False) -> str:
    if cost is None:
        return "—"
    return f"${cost:,.2f}" + (" (partial)" if partial else "")


def usage_table(rows: list[dict], by: str) -> str:
    lines = [f"| {by} | replies | input | cache write | cache read | output | cost |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['key']} | {r['replies']:,} | {r['input']:,} | {r['cache_write']:,} | "
                     f"{r['cache_read']:,} | {r['output']:,} | {fmt_cost(r['cost'], r['cost_partial'])} |")
    return "\n".join(lines)


def totals_line(t: dict, duplicates: int) -> str:
    text = (f"Total: {t['replies']:,} replies · input {t['input']:,} · cache write {t['cache_write']:,} · "
            f"cache read {t['cache_read']:,} · output {t['output']:,} · cache hit {100 * t['cache_hit_rate']:.1f}% · "
            f"cost {fmt_cost(t['cost'], t['cost_partial'])} (prices as of {PRICES_AS_OF}) · "
            f"duplicates skipped {duplicates:,}")
    notes = []
    if t.get("cache_write_unknown"):
        notes.append(UNKNOWN_TTL_NOTE)
    if t.get("unknown_models"):
        notes.append("no price for: " + ", ".join(t["unknown_models"]))
    return text + "".join(f"\nNote: {n}" for n in notes)
