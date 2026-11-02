"""Visual resume pack: one 1092 x 1092 PNG carrying the story of the history.

Images are charged by size (about width * height / 750 tokens), so a single
square picture is a cheap way to hand over sessions, requests, decisions and
failures. Exact paths and commands stay in resume-pack.md: text drawn here is
scrubbed of absolute paths and long command fragments. Pillow is optional and
imported lazily; without it `render_visual_pack` returns None.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from itertools import islice
from pathlib import Path

from memgraph.brief import _newest, _one_line
from memgraph.graph import Graph

IMAGE_SIZE = (1092, 1092)
IMAGE_FILE = "memory.png"
FONT_PX = 13  # body text; the minimum allowed
_FONT_CANDIDATES = (
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("/System/Library/Fonts/Helvetica.ttc", None),
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
)
MORE = "\u2026 older items in resume-pack.md"
FOOTER = "Exact paths and commands: see resume-pack.md"
_INK, _HEAD, _MUTED, _RED = (25, 25, 25), (20, 60, 130), (85, 85, 85), (185, 25, 25)
_MARGIN, _GAP, _BAR, _FOOT, _LINE = 24, 24, 52, 30, 16
_INDENT = 12
_MAX_DECISIONS = 6

_QUOTED = re.compile(r"`[^`]{41,}`|\"[^\"]{41,}\"|(?<!\w)'[^']{41,}'")
_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'`)\]>]*")
_POSIX_PATH = re.compile(r"(?<![\w.:/])/(?:[^\s/\"'`)\]>]+/)+[^\s/\"'`)\]>]*")


def scrub(text) -> str:
    """One line with absolute paths cut to their file name and long quoted commands dropped."""
    text = _one_line(text)
    text = _QUOTED.sub("\u2026", text)

    def base(m):
        return re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1] or "\u2026"

    return _POSIX_PATH.sub(base, _WIN_PATH.sub(base, text))


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
    """Sessions newest first: (title, [(task, n_files, errors)], session errors)."""
    sessions = graph.meta.get("sessions") or {}
    nodes = graph.nodes
    edges = list(graph.edges.values())
    asked: dict[str, list[dict]] = {}
    owner: dict[str, tuple[str, str]] = {}  # command/file node id -> (edge last, task id), newest wins
    edited: dict[str, set] = {}
    for e in edges:
        src, dst, rel = e.get("src"), nodes.get(e.get("dst")), e.get("rel")
        if not dst:
            continue
        if rel == "asked" and dst.get("kind") == "task":
            asked.setdefault(str(src).removeprefix("session:"), []).append(dst)
        elif rel in ("ran", "edited", "read"):
            key = (str(e.get("last") or ""), str(src))
            if dst.get("id") not in owner or key > owner[dst.get("id")]:
                owner[dst.get("id")] = key
            if rel == "edited":
                edited.setdefault(src, set()).add(dst.get("id"))
    failed: dict[str, list[dict]] = {}
    for e in edges:  # second pass: attribution never depends on edge order
        dst = nodes.get(e.get("dst"))
        if e.get("rel") == "failed" and dst:
            src = e.get("src")
            failed.setdefault(owner[src][1] if src in owner else src, []).append(dst)
    out = []
    for sid in sorted(asked, key=lambda s: ((sessions.get(s) or {}).get("last") or "", s), reverse=True):
        title = (sessions.get(sid) or {}).get("title") or sid
        tasks = []
        for t in sorted(asked[sid], key=lambda n: (n.get("first") or "", str(n.get("label", ""))), reverse=True):
            tid = t.get("id")
            tasks.append((t, len(edited.get(tid, ())), _newest(failed.get(tid, []), 1)))
        out.append((title, tasks, _newest(failed.get(f"session:{sid}", []), 1)))
    return out


def has_story(graph: Graph) -> bool:
    """True when there is anything to draw: a request or a decision."""
    return any(n.get("kind") in ("task", "decision") for n in graph.nodes.values())


# A line is (indent_px, [(text, style), ...]); style is ink, head, muted or red.
def _lines(graph: Graph, font, col_w: int):
    """Lazily yield lines: decisions first, then sessions newest first."""
    first = True
    decisions = _newest([n for n in graph.nodes.values() if n.get("kind") == "decision"], _MAX_DECISIONS)
    if decisions:
        first = False
        yield 0, [("Decisions", "head")]
        for d in decisions:
            for i, ln in enumerate(_wrap(scrub(d.get("label", "")), font, col_w - _INDENT, 2)):
                yield (0, [("\u2022 " + ln, "ink")]) if i == 0 else (_INDENT, [(ln, "ink")])
    for title, tasks, s_errors in _story(graph):
        if not first:
            yield 0, []
        first = False
        for ln in _wrap(scrub(title), font, col_w, 1):
            yield 0, [(ln, "head")]
        for task, n_files, errors in tasks:
            for i, ln in enumerate(_wrap(scrub(task.get("label", "")), font, col_w - _INDENT, 2)):
                yield (0, [("\u2022 " + ln, "ink")]) if i == 0 else (_INDENT, [(ln, "ink")])
            yield _INDENT, _meta(font, col_w - _INDENT, n_files, errors) or None
        for er in s_errors:
            yield _INDENT, [("failed: " + _short(font, scrub(er.get("label", "")), col_w - _INDENT - 40), "red")]


def _short(font, text: str, width: int) -> str:
    return (_wrap(text, font, max(40, width), 1) or [""])[0]


def _meta(font, width: int, n_files: int, errors) -> list:
    """One compact line: 'changed N files · failed: <short error>' (parts optional)."""
    segs = []
    if n_files:
        segs.append((f"changed {n_files} file{'s' if n_files != 1 else ''}", "muted"))
    if errors:
        used = sum(font.getlength(t) for t, _ in segs) + (font.getlength(" \u00b7 ") if segs else 0)
        if segs:
            segs.append((" \u00b7 ", "muted"))
        segs.append(("failed: " + _short(font, scrub(errors[0].get("label", "")), int(width - used - font.getlength("failed: "))), "red"))
    return segs


def compose(graph: Graph, font, col_w: int, cap: int) -> dict:
    """Everything drawn below the header: {"flow": lines, "footer": str}; flow is capped at 2*cap lines."""
    flow = [(i, segs or []) for i, segs in islice(_lines(graph, font, col_w), 2 * cap + 1) if segs is not None]
    if len(flow) > 2 * cap:
        flow = flow[: 2 * cap - 1] + [(0, [(MORE, "muted")])]
    return {"flow": flow, "footer": FOOTER}


def render_visual_pack(graph: Graph, path: Path, project_root: Path | None = None) -> Path | None:
    """Write the PNG to `path` atomically; None without Pillow or for an empty graph.

    Callers tell the two apart with `has_story`."""
    if not has_story(graph):
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    body, bold = _load_fonts(ImageFont)
    w, h = IMAGE_SIZE
    col_w = (w - 2 * _MARGIN - _GAP) // 2
    cap = (h - _BAR - _FOOT - _MARGIN // 2) // _LINE  # lines per column
    page = compose(graph, body, col_w, cap)

    img = Image.new("RGB", IMAGE_SIZE, "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, _BAR - 8], fill=(235, 240, 248))
    name = project_root.name if project_root is not None and project_root.name else "project"
    d.text((_MARGIN, 5), f"{scrub(name)} \u2014 session memory", font=bold, fill=_HEAD)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    d.text((_MARGIN, 5 + _LINE), f"generated {stamp}   \u2248 1,590 image tokens", font=body, fill=_MUTED)
    colors = {"ink": _INK, "head": _HEAD, "muted": _MUTED, "red": _RED}
    for n, (indent, segs) in enumerate(page["flow"]):
        col, row = divmod(n, cap)
        x = _MARGIN + col * (col_w + _GAP) + indent
        for text, style in segs:
            d.text((x, _BAR + row * _LINE), text, font=bold if style == "head" else body, fill=colors[style])
            x += body.getlength(text)
    d.line([w // 2, _BAR, w // 2, _BAR + cap * _LINE], fill=(220, 220, 220))
    d.text((_MARGIN, h - _FOOT + 6), page["footer"], font=body, fill=_MUTED)

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
