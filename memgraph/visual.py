"""Visual resume pack: one 1092 x 1092 PNG carrying the story of the history.

Images are charged by size (about width * height / 750 tokens), so a single
square picture is a cheap way to hand over sessions, requests, decisions and
failures. Exact paths and commands stay in resume-pack.md. Pillow is optional
and imported lazily; without it `render_visual_pack` returns None.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from itertools import islice
from pathlib import Path

from memgraph.brief import _newest, _one_line
from memgraph.graph import Graph

IMAGE_SIZE = (1092, 1092)
IMAGE_FILE = "memory.png"
FONT_PX = 14  # body text; never below 13
_FONT_CANDIDATES = (
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("/System/Library/Fonts/Helvetica.ttc", None),
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
)
_MORE = "\u2026 older items in resume-pack.md"
_FOOTER = "Exact paths and commands: see resume-pack.md"
_INK, _HEAD, _MUTED, _RED = (25, 25, 25), (20, 60, 130), (85, 85, 85), (185, 25, 25)
_MARGIN, _GAP, _BAR, _FOOT, _LINE = 24, 24, 56, 34, 19


def _load_fonts(ImageFont):
    """(body, bold) fonts: first candidate that loads, else Pillow's default at 13+ px."""
    for regular, bold in _FONT_CANDIDATES:
        try:
            body = ImageFont.truetype(regular, FONT_PX)
        except OSError:
            continue
        try:
            return body, ImageFont.truetype(bold, FONT_PX) if bold else body
        except OSError:
            return body, body
    default = ImageFont.load_default(size=FONT_PX)
    return default, default


def _wrap(text: str, font, width: int, max_lines: int) -> list[str]:
    """Greedy word wrap to `width` px, at most `max_lines`, ending in an ellipsis when cut."""
    lines: list[str] = []
    cur = ""
    for word in _one_line(text).split(" "):
        while font.getlength(word) > width:  # break a word longer than a line
            k = max(1, int(len(word) * width / font.getlength(word)))
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:k])
            word = word[k:]
        trial = f"{cur} {word}".strip()
        if cur and font.getlength(trial) > width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and font.getlength(last + "\u2026") > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "\u2026"
    return lines


def _story(graph: Graph):
    """Sessions newest first: (title, [(task, n_files, decisions, errors)], session errors)."""
    sessions = graph.meta.get("sessions") or {}
    nodes = graph.nodes
    asked: dict[str, list[dict]] = {}
    task_of: dict[str, str] = {}  # command node id -> task id
    edited: dict[str, set] = {}
    decided: dict[str, list[dict]] = {}
    failed: dict[str, list[dict]] = {}
    for e in graph.edges.values():
        src, dst, rel = e.get("src"), nodes.get(e.get("dst")), e.get("rel")
        if not dst:
            continue
        if rel == "asked" and dst.get("kind") == "task":
            asked.setdefault(str(src).removeprefix("session:"), []).append(dst)
        elif rel == "ran":
            task_of[dst.get("id")] = src
        elif rel == "edited":
            edited.setdefault(src, set()).add(dst.get("id"))
        elif rel == "decided" and dst.get("kind") == "decision":
            decided.setdefault(src, []).append(dst)
        elif rel == "failed":
            failed.setdefault(task_of.get(src, src), []).append(dst)
    out = []
    for sid in sorted(asked, key=lambda s: ((sessions.get(s) or {}).get("last") or "", s), reverse=True):
        title = (sessions.get(sid) or {}).get("title") or sid
        tasks = []
        for t in sorted(asked[sid], key=lambda n: (n.get("first") or "", str(n.get("label", ""))), reverse=True):
            tid = t.get("id")
            tasks.append((t, len(edited.get(tid, ())), _newest(decided.get(tid, []), 2),
                          _newest(failed.get(tid, []), 1)))
        out.append((title, tasks, _newest(failed.get(f"session:{sid}", []), 1)))
    return out


def _lines(graph: Graph, font, col_w: int):
    """Lazily yield (indent, text, style) lines, newest session first."""
    first = True
    for title, tasks, s_errors in _story(graph):
        if not first:
            yield 0, "", "ink"
        first = False
        for ln in _wrap(title, font, col_w, 2):
            yield 0, ln, "head"
        for task, n_files, decisions, errors in tasks:
            for i, ln in enumerate(_wrap(str(task.get("label", "")), font, col_w - 14, 3)):
                yield (0, "\u2022 " + ln, "ink") if i == 0 else (14, ln, "ink")
            if n_files:
                yield 14, f"changed {n_files} file{'s' if n_files != 1 else ''}", "muted"
            for d in decisions:
                for ln in _wrap("decided: " + str(d.get("label", "")), font, col_w - 14, 2):
                    yield 14, ln, "muted"
            for er in errors:
                for ln in _wrap("failed: " + str(er.get("label", "")), font, col_w - 14, 1):
                    yield 14, ln, "red"
        for er in s_errors:
            for ln in _wrap("failed: " + str(er.get("label", "")), font, col_w - 14, 1):
                yield 14, ln, "red"


def render_visual_pack(graph: Graph, path: Path, project_root: Path | None = None) -> Path | None:
    """Write the PNG to `path` atomically; None without Pillow or for an empty graph."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    body, bold = _load_fonts(ImageFont)
    w, h = IMAGE_SIZE
    col_w = (w - 2 * _MARGIN - _GAP) // 2
    cap = (h - _BAR - _FOOT - _MARGIN // 2) // _LINE  # lines per column
    flow = list(islice(_lines(graph, body, col_w), 2 * cap + 1))
    if not any(text for _i, text, _s in flow):
        return None
    if len(flow) > 2 * cap:
        flow = flow[: 2 * cap - 1] + [(0, _MORE, "muted")]

    img = Image.new("RGB", IMAGE_SIZE, "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, _BAR - 8], fill=(235, 240, 248))
    name = project_root.name if project_root is not None and project_root.name else "project"
    d.text((_MARGIN, 6), f"{name} \u2014 session memory", font=bold, fill=_HEAD)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    d.text((_MARGIN, 6 + _LINE), f"generated {stamp}   \u2248 1,590 image tokens", font=body, fill=_MUTED)
    colors = {"ink": _INK, "head": _HEAD, "muted": _MUTED, "red": _RED}
    for n, (indent, text, style) in enumerate(flow):
        col, row = divmod(n, cap)
        if text:
            d.text((_MARGIN + col * (col_w + _GAP) + indent, _BAR + row * _LINE), text,
                   font=bold if style == "head" else body, fill=colors[style])
    d.line([w // 2, _BAR, w // 2, _BAR + cap * _LINE], fill=(220, 220, 220))
    d.text((_MARGIN, h - _FOOT + 6), _FOOTER, font=body, fill=_MUTED)

    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(fd)
    try:
        img.save(tmp, "PNG")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
