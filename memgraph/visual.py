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
from itertools import islice
from pathlib import Path

from memgraph.brief import DATA_NOTE, _newest, _one_line
from memgraph.graph import Graph
from memgraph.pack import built_line
from memgraph.relevance import is_project_file, project_errors
from memgraph.transcript import clean_prompt, fmt_local

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
_MAX_STEPS = 10
_BALANCE_WINDOW = 4  # lines a column split may move to land between blocks

_QUOTED = re.compile(r"`[^`]{41,}`|\"[^\"]{41,}\"|(?<!\w)'[^']{41,}'")
_URL = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9+.-]*)://(\S+)")
_WIN_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'`)\]>]*")
_POSIX_PATH = re.compile(r"(?<![\w.:/])/(?:[^\s/\"'`)\]>]+/)+[^\s/\"'`)\]>]*")


def scrub(text) -> str:
    """One line with absolute paths cut to their file name and long quoted commands dropped."""
    text = _one_line(text)
    text = _QUOTED.sub("\u2026", text)

    def base(m):
        return re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1] or "\u2026"

    def url(m):  # a URL shows only its host (a file name for file://), never its path
        rest = m.group(2).rstrip(".,;)")
        if m.group(1).lower() == "file":
            return re.split(r"[\\/]", rest.rstrip("\\/"))[-1] or "…"
        return re.split(r"[/?#]", rest)[0].rsplit("@", 1)[-1]

    text = _URL.sub(url, text)
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


def _story(graph: Graph, project_root: Path | None = None):
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
            if rel == "edited" and is_project_file(dst.get("label", ""), project_root):
                edited.setdefault(src, set()).add(dst.get("id"))
    failed: dict[str, list[dict]] = {}
    for e in edges:  # second pass: attribution never depends on edge order
        dst = nodes.get(e.get("dst"))
        if e.get("rel") == "failed" and dst:
            src = e.get("src")
            failed.setdefault(owner[src][1] if src in owner else src, []).append(dst)
    shown = {id(n) for n in project_errors(graph, [n for g in failed.values() for n in g], project_root)}
    failed = {k: [n for n in v if id(n) in shown] for k, v in failed.items()}
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
def _lines(graph: Graph, font, col_w: int, request_lines: int = 2, project_root: Path | None = None):
    """Lazily yield lines: decisions first, then sessions newest first.

    A request takes up to `request_lines` lines; a None line is skipped."""
    first = True
    decisions = _newest([n for n in graph.nodes.values() if n.get("kind") == "decision"], _MAX_DECISIONS)
    if decisions:
        first = False
        yield 0, [("Decisions", "head")]
        for d in decisions:
            for i, ln in enumerate(_wrap(scrub(d.get("label", "")), font, col_w - _INDENT, 2)):
                yield (0, [("\u2022 " + ln, "ink")]) if i == 0 else (_INDENT, [(ln, "ink")])
    for title, tasks, s_errors in _story(graph, project_root):
        if not first:
            yield 0, []
        first = False
        for ln in _wrap(scrub(title), font, col_w, 1):
            yield 0, [(ln, "head")]
        for task, n_files, errors in tasks:
            label = scrub(clean_prompt(task.get("label", "")))
            for i, ln in enumerate(_wrap(label, font, col_w - _INDENT, request_lines)):
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


def _next_steps(graph: Graph, font, col_w: int) -> list[list]:
    """Heading plus one item per open step of the latest plan (each item a list of lines)."""
    last = graph.meta.get("last_todos") or {}
    todos = [t for t in last.get("items") or [] if isinstance(t, dict) and t.get("status") != "completed"]
    if not todos:
        return []
    stamp = fmt_local(last.get("ts"))
    head = f"Next steps (as of {stamp})" if stamp else "Next steps"
    items = [[(0, [(head, "head")])]]
    for t in todos[:_MAX_STEPS]:
        prefix = "now: " if t.get("status") == "in_progress" else ""
        text = scrub(prefix + str(t.get("content") or ""))
        items.append([(0, [("• " + ln, "ink")]) if i == 0 else (_INDENT, [(ln, "ink")])
                      for i, ln in enumerate(_wrap(text, font, col_w - _INDENT, 2))])
    return items


def _files_touched(graph: Graph, font, col_w: int, project_root: Path | None = None) -> list[list]:
    """Heading plus lines of file names (never paths), most-touched first."""
    files = sorted((n for n in graph.nodes.values() if n.get("kind") == "file" and is_project_file(n.get("label", ""), project_root)),
                   key=lambda n: (str(n.get("last") or ""), str(n.get("label", ""))), reverse=True)
    files.sort(key=lambda n: n.get("count") or 0, reverse=True)  # stable: newest first among equals
    names: list[str] = []
    for n in files:
        name = re.split(r"[\\/]", scrub(n.get("label", "")).rstrip("\\/"))[-1]
        if name and name not in names:
            names.append(name)
    if not names:
        return []
    items, cur, sep = [[(0, [("Files touched", "head")])]], "", " · "
    for name in names:
        name = _short(font, name, col_w)
        if cur and font.getlength(cur + sep + name) > col_w:
            items.append([(0, [(cur, "muted")])])
            cur = name
        else:
            cur = f"{cur}{sep}{name}" if cur else name
    items.append([(0, [(cur, "muted")])])
    return items


def _fit(items: list[list], room: int) -> list:
    """A blank line, the heading and as many whole items as fit in `room` lines; [] if none fits."""
    if len(items) < 2:
        return []
    out = [(0, [])] + items[0]
    for item in items[1:]:
        if len(out) + len(item) > room:
            break
        out += item
    return out if len(out) > 1 + len(items[0]) else []


def _columns(flow: list, cap: int) -> list[list]:
    """Split the flow (at most 2*cap lines) into two columns, as even as possible.

    Near the middle, the split prefers a blank line between blocks (dropped, so no
    column starts blank), then a line that starts an item, so a wrapped item stays whole."""
    n = len(flow)
    mid = (n + 1) // 2
    near = sorted((k for k in range(1, n) if abs(k - mid) <= _BALANCE_WINDOW), key=lambda k: abs(k - mid))
    for k in near:
        if not flow[k][1] and k <= cap and n - k - 1 <= cap:
            return [flow[:k], flow[k + 1:]]
    for k in near:
        if flow[k][0] == 0 and k <= cap and n - k <= cap:
            return [flow[:k], flow[k:]]
    return [flow[:mid], flow[mid:]]


def compose(graph: Graph, font, col_w: int, cap: int, project_root: Path | None = None) -> dict:
    """Everything drawn below the header: {"flow": lines, "columns": [left, right], "footer": str}.

    The story fills at most 2*cap lines (cut with the MORE marker). Spare lines go,
    in order, to the open next steps, the most-touched file names, and then to
    letting each request use 4 lines instead of 2. The lines are then balanced
    across both columns."""
    limit = 2 * cap

    def story(request_lines: int) -> list:
        lines = (ln for ln in _lines(graph, font, col_w, request_lines, project_root) if ln[1] is not None)
        return list(islice(lines, limit + 1))

    flow = story(2)
    if len(flow) > limit:
        flow = flow[: limit - 1] + [(0, [(MORE, "muted")])]
        return {"flow": flow, "columns": [flow[:cap], flow[cap:]], "footer": FOOTER}
    extras: list = []
    for items in (_next_steps(graph, font, col_w), _files_touched(graph, font, col_w, project_root)):
        extras += _fit(items, limit - len(flow) - len(extras))
    longer = story(4)
    if len(longer) + len(extras) <= limit:
        flow = longer
    flow = flow + extras
    return {"flow": flow, "columns": _columns(flow, cap), "footer": FOOTER}


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
    page = compose(graph, body, col_w, cap, project_root)

    img = Image.new("RGB", IMAGE_SIZE, "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, _BAR - 8], fill=(235, 240, 248))
    name = project_root.name if project_root is not None and project_root.name else "project"
    d.text((_MARGIN, 5), f"{scrub(name)} \u2014 session memory", font=bold, fill=_HEAD)
    d.text((_MARGIN, 5 + _LINE), f"{built_line()}   \u2248 1,590 image tokens   {DATA_NOTE}", font=body, fill=_MUTED)
    colors = {"ink": _INK, "head": _HEAD, "muted": _MUTED, "red": _RED}
    for col, lines in enumerate(page["columns"]):
        for row, (indent, segs) in enumerate(lines):
            x = _MARGIN + col * (col_w + _GAP) + indent
            for text, style in segs:
                font = bold if style == "head" else body
                d.text((x, _BAR + row * _LINE), text, font=font, fill=colors[style])
                x += font.getlength(text)
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
