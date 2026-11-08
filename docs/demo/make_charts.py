"""Draw the README charts (plain SVG, no libraries) from numbers measured on the author's machine.

    python docs/demo/make_charts.py

The numbers come from `run-python savings --json` and `bench/measure.py`, run on 2026-11-08.
Only totals are used; no chat text.
"""
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "images"

INK, MUTED, GRID = "#1f2328", "#59636e", "#d1d9e0"
RED, GREEN, BLUE = "#d1242f", "#1a7f37", "#0969da"
FONT = "font-family='-apple-system,Segoe UI,Helvetica,Arial,sans-serif'"

# Coming back to a big chat after its cache expired: tokens Claude re-read at full price.
COLD = [("Nov 2", 399_849, 3.20), ("Nov 2", 558_717, 4.47), ("Nov 3", 697_854, 5.58),
        ("Nov 4", 777_486, 6.22), ("Nov 5", 840_843, 6.73)]
FRESH_START = 538  # tokens a /clear with the brief adds instead (measured brief size)

# This session: the old chat when it was left vs the new chat's first reply.
OLD_CHAT, NEW_CHAT, FIXED_SETUP = 909_312, 75_629, 73_391

# bench/measure.py on 9 sessions: (label, whole chat tokens, brief tokens).
BENCH = [("A", 86_270, 933), ("B1", 549_671, 810), ("B2", 326_085, 775), ("B3", 17_981, 246),
         ("B4", 70_705, 174), ("C", 188_500, 361), ("D1", 707_427, 845), ("D2", 407_675, 712),
         ("D3", 84_554, 161)]


def _svg(w: int, h: int, body: list[str], title: str, subtitle: str) -> str:
    return "\n".join([
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' viewBox='0 0 {w} {h}' {FONT}>",
        f"<rect width='{w}' height='{h}' rx='12' fill='#ffffff' stroke='{GRID}'/>",
        f"<text x='24' y='36' font-size='18' font-weight='600' fill='{INK}'>{title}</text>",
        f"<text x='24' y='58' font-size='13' fill='{MUTED}'>{subtitle}</text>",
        *body, "</svg>", ""])


def cold_restarts() -> str:
    w, left, top, bar_h, gap = 860, 90, 84, 26, 18
    scale = (w - left - 230) / max(t for _, t, _ in COLD)
    body = []
    for i, (day, tokens, usd) in enumerate(COLD):
        y = top + i * (bar_h + gap)
        body += [
            f"<text x='{left - 12}' y='{y + 18}' font-size='13' text-anchor='end' fill='{MUTED}'>{day}</text>",
            f"<rect x='{left}' y='{y}' width='{tokens * scale:.0f}' height='{bar_h}' rx='4' fill='{RED}'/>",
            f"<text x='{left + tokens * scale + 8:.0f}' y='{y + 18}' font-size='13' fill='{INK}'>"
            f"{tokens:,} tokens · ${usd:.2f}</text>",
        ]
    y = top + len(COLD) * (bar_h + gap) + 6
    total_t = sum(t for _, t, _ in COLD)
    total_usd = sum(u for _, _, u in COLD)
    body += [
        f"<rect x='{left}' y='{y}' width='3' height='{bar_h}' rx='1' fill='{GREEN}'/>",
        f"<text x='{left - 12}' y='{y + 18}' font-size='13' text-anchor='end' fill='{MUTED}'>/clear</text>",
        f"<text x='{left + 12}' y='{y + 18}' font-size='13' fill='{GREEN}' font-weight='600'>"
        f"about {FRESH_START} tokens each time with the memory brief</text>",
        f"<text x='24' y='{y + 62}' font-size='13' fill='{INK}'>Five returns cost {total_t:,} tokens (${total_usd:.2f}). "
        f"Five fresh starts would have cost about {FRESH_START * 5:,} tokens.</text>",
    ]
    return _svg(w, y + 84, body, "Coming back to a big chat after its cache expired",
                "Tokens re-read at full price, measured from one person's Claude Code transcripts (Nov 2–5, 2026)")


def fresh_start() -> str:
    w, left, top, bar_h = 860, 160, 92, 34
    scale = (w - left - 200) / OLD_CHAT
    brief = NEW_CHAT - FIXED_SETUP
    body = [
        f"<text x='{left - 12}' y='{top + 22}' font-size='13' text-anchor='end' fill='{MUTED}'>Keep old chat</text>",
        f"<rect x='{left}' y='{top}' width='{OLD_CHAT * scale:.0f}' height='{bar_h}' rx='4' fill='{RED}'/>",
        f"<text x='{left + OLD_CHAT * scale + 8:.0f}' y='{top + 22}' font-size='13' fill='{INK}'>{OLD_CHAT:,}</text>",
        f"<text x='{left - 12}' y='{top + 74}' font-size='13' text-anchor='end' fill='{MUTED}'>/clear + brief</text>",
        f"<rect x='{left}' y='{top + 52}' width='{FIXED_SETUP * scale:.0f}' height='{bar_h}' rx='4' fill='{BLUE}'/>",
        f"<rect x='{left + FIXED_SETUP * scale:.0f}' y='{top + 52}' width='4' height='{bar_h}' fill='{GREEN}'/>",
        f"<text x='{left + NEW_CHAT * scale + 10:.0f}' y='{top + 74}' font-size='13' fill='{INK}'>{NEW_CHAT:,}</text>",
        f"<rect x='{left}' y='{top + 112}' width='12' height='12' fill='{BLUE}'/>",
        f"<text x='{left + 18}' y='{top + 122}' font-size='12' fill='{MUTED}'>Claude Code's own setup "
        f"(instructions, tools, plugins): about {FIXED_SETUP:,}, paid by every new chat</text>",
        f"<rect x='{left}' y='{top + 132}' width='12' height='12' fill='{GREEN}'/>",
        f"<text x='{left + 18}' y='{top + 142}' font-size='12' fill='{MUTED}'>the memory brief and related notes: "
        f"about {brief:,}</text>",
        f"<text x='24' y='{top + 180}' font-size='13' fill='{INK}'>Every message after the switch sent "
        f"{OLD_CHAT - NEW_CHAT:,} fewer tokens ({(OLD_CHAT - NEW_CHAT) / OLD_CHAT:.0%} less).</text>",
    ]
    return _svg(w, top + 204, body, "Context per message: old chat vs a fresh start",
                "Measured on 2026-11-05 when this project's own 909k-token chat was replaced by /clear")


def benchmark() -> str:
    w, left, top, bar_h, gap = 860, 60, 84, 20, 12
    scale = (w - left - 260) / max(h for _, h, _ in BENCH)
    body = []
    for i, (label, history, brief) in enumerate(BENCH):
        y = top + i * (bar_h + gap)
        body += [
            f"<text x='{left - 10}' y='{y + 15}' font-size='12' text-anchor='end' fill='{MUTED}'>{label}</text>",
            f"<rect x='{left}' y='{y}' width='{history * scale:.0f}' height='{bar_h}' rx='3' fill='{GRID}'/>",
            f"<rect x='{left}' y='{y}' width='{max(3, brief * scale):.0f}' height='{bar_h}' rx='1' fill='{GREEN}'/>",
            f"<text x='{left + history * scale + 8:.0f}' y='{y + 15}' font-size='12' fill='{INK}'>"
            f"{history:,} → {brief:,} ({1 - brief / history:.1%} smaller)</text>",
        ]
    y = top + len(BENCH) * (bar_h + gap) + 10
    body += [
        f"<rect x='{left}' y='{y}' width='12' height='12' fill='{GRID}'/>",
        f"<text x='{left + 18}' y='{y + 10}' font-size='12' fill='{MUTED}'>whole chat a resume re-sends</text>",
        f"<rect x='{left + 230}' y='{y}' width='12' height='12' fill='{GREEN}'/>",
        f"<text x='{left + 248}' y='{y + 10}' font-size='12' fill='{MUTED}'>startup brief that replaces it</text>",
    ]
    return _svg(w, y + 34, body, "Whole chat vs startup brief, 9 real sessions (tokens)",
                "bench/measure.py on the author's transcripts from four projects (names replaced by letters)")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, svg in (("chart-cold-restarts.svg", cold_restarts()), ("chart-fresh-start.svg", fresh_start()),
                      ("chart-benchmark.svg", benchmark()), ("how-it-works.svg", how_it_works())):
        (OUT / name).write_text(svg, encoding="utf-8", newline="\n")
        print(OUT / name)



def how_it_works() -> str:
    """Boxes and arrows: transcripts -> memory graph -> what you get."""
    def box(x, y, w, h, fill, title, line2=""):
        return [f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='10' fill='{fill}' stroke='{GRID}'/>",
                f"<text x='{x + w / 2}' y='{y + (h / 2 if not line2 else h / 2 - 6)}' font-size='14' font-weight='600' "
                f"text-anchor='middle' dominant-baseline='middle' fill='{INK}'>{title}</text>",
                *([f"<text x='{x + w / 2}' y='{y + h / 2 + 14}' font-size='12' text-anchor='middle' fill='{MUTED}'>"
                   f"{line2}</text>"] if line2 else [])]

    def arrow(x1, y1, x2, y2, label=""):
        out = [f"<line x1='{x1}' y1='{y1}' x2='{x2}' y2='{y2}' stroke='{MUTED}' stroke-width='1.5' marker-end='url(#a)'/>"]
        if label:
            out.append(f"<text x='{(x1 + x2) / 2}' y='{min(y1, y2) - 8}' font-size='12' text-anchor='middle' "
                       f"fill='{GREEN}' font-weight='600'>{label}</text>")
        return out

    body = [f"<defs><marker id='a' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' "
            f"orient='auto'><path d='M0,0 L10,5 L0,10 z' fill='{MUTED}'/></marker></defs>"]
    body += box(24, 120, 190, 70, "#f6f8fa", "Claude Code transcripts", "files already on your disk")
    body += arrow(214, 155, 314, 155, "0 tokens")
    body += box(316, 110, 200, 90, "#ddf4ff", "Memory graph", "requests · files · commands")
    body += [f"<text x='416' y='186' font-size='12' text-anchor='middle' fill='{MUTED}'>errors · decisions</text>"]
    outs = [("Startup brief", "after /clear, ~500–2,000 tokens"), ("/recall · explain · path", "look things up later"),
            ("Cache guard + watcher", "warns before costly resumes"), ("3D map · snapshot · stats", "see it all")]
    for i, (title, sub) in enumerate(outs):
        y = 76 + i * 62
        body += box(600, y, 236, 50, "#ffffff", title, sub)
        body += arrow(516, 155, 598, y + 25)
    return _svg(860, 340, body, "How it works", "Plain Python reads what Claude Code already saved; no model calls, nothing sent anywhere")


if __name__ == "__main__":
    main()
