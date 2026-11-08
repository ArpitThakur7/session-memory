"""Build the README pictures from a made-up project, so no real chat ever appears in them.

    python docs/demo/make_demo.py

Writes docs/images/demo-*.{png,html,txt}. Every output comes from the plugin's own code
(brief, recall, explain, path, visual pack, memory map) run on the invented chats below.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from memgraph.brief import build_brief  # noqa: E402
from memgraph.explore import explain_lines, path_lines  # noqa: E402
from memgraph.graph import Graph  # noqa: E402
from memgraph.memory_ops import recall_lines, remember  # noqa: E402
from memgraph.transcript import Event  # noqa: E402
from memgraph.viewer import write_map  # noqa: E402
from memgraph.visual import render_visual_pack  # noqa: E402

OUT = REPO / "docs" / "images"
PROJECT = Path("/home/demo/recipe-app")  # never read; only used to show paths relative to it

# (session, minute offset, kind, data) — an invented two-day project.
DAY1, DAY2 = "2026-11-03T09:", "2026-11-04T10:"
CHATS = [
    ("s1", DAY1 + "00", "title", {"text": "Recipe app: search and login", "source": "custom"}),
    ("s1", DAY1 + "00", "prompt", {"text": "add a search box to the recipe list page"}),
    ("s1", DAY1 + "02", "read", {"path": "src/pages/RecipeList.tsx", "id": "r1"}),
    ("s1", DAY1 + "05", "edit", {"path": "src/pages/RecipeList.tsx", "id": "e1"}),
    ("s1", DAY1 + "06", "edit", {"path": "src/components/SearchBox.tsx", "id": "e2"}),
    ("s1", DAY1 + "08", "command", {"cmd": "npm test -- SearchBox", "id": "c1"}),
    ("s1", DAY1 + "09", "error", {"text": "FAIL src/components/SearchBox.tsx: expected 3 results, got 0", "tool_use_id": "c1"}),
    ("s1", DAY1 + "12", "edit", {"path": "src/api/recipes.ts", "id": "e3"}),
    ("s1", DAY1 + "14", "command", {"cmd": "npm test -- SearchBox", "id": "c2"}),
    ("s1", DAY1 + "20", "prompt", {"text": "make search ignore upper and lower case"}),
    ("s1", DAY1 + "22", "edit", {"path": "src/api/recipes.ts", "id": "e4"}),
    ("s1", DAY1 + "30", "prompt", {"text": "add login with email and password"}),
    ("s1", DAY1 + "33", "edit", {"path": "src/pages/Login.tsx", "id": "e5"}),
    ("s1", DAY1 + "35", "edit", {"path": "src/api/auth.ts", "id": "e6"}),
    ("s1", DAY1 + "38", "command", {"cmd": "npm run build", "id": "c3"}),
    ("s1", DAY1 + "39", "error", {"text": "TS2322 in src/api/auth.ts: Type 'string' is not assignable to type 'User'", "tool_use_id": "c3"}),
    ("s2", DAY2 + "00", "title", {"text": "Recipe app: favourites", "source": "custom"}),
    ("s2", DAY2 + "00", "prompt", {"text": "let users save favourite recipes"}),
    ("s2", DAY2 + "03", "read", {"path": "src/api/recipes.ts", "id": "r2"}),
    ("s2", DAY2 + "06", "edit", {"path": "src/api/favourites.ts", "id": "e7"}),
    ("s2", DAY2 + "08", "edit", {"path": "src/pages/RecipeList.tsx", "id": "e8"}),
    ("s2", DAY2 + "10", "command", {"cmd": "npm test", "id": "c4"}),
    ("s2", DAY2 + "11", "todo", {"items": [
        {"content": "Save favourites in the database", "status": "completed"},
        {"content": "Show a heart button on each recipe", "status": "in_progress"},
        {"content": "Add a Favourites page", "status": "pending"},
        {"content": "Fix the login type error in auth.ts", "status": "pending"},
    ]}),
    ("s2", DAY2 + "12", "reply", {"text": "Favourites are saved through src/api/favourites.ts and the list page "
                                          "shows a heart on each recipe. Tests pass. Next: the Favourites page."}),
]


def build() -> Graph:
    g = Graph()
    for sid, ts, kind, data in CHATS:
        g.apply(Event(kind, ts + ":00Z", sid, data))
    from datetime import datetime, timezone
    remember(g, "Store favourites per user in a separate table, because recipes are shared",
             datetime(2026, 11, 4, 10, 9, tzinfo=timezone.utc))
    return g


def main() -> None:
    import memgraph.transcript as transcript
    from datetime import timezone
    transcript._LOCAL_TZ = timezone.utc  # same pictures on every machine
    OUT.mkdir(parents=True, exist_ok=True)
    g = build()
    texts = {
        "demo-brief.txt": build_brief(g, project_root=PROJECT),
        "demo-recall.txt": "\n".join(recall_lines(g, "search", PROJECT)),
        "demo-explain.txt": "\n".join(explain_lines(g, "auth.ts", PROJECT)),
        "demo-path.txt": "\n".join(path_lines(g, "login with email", "TS2322", PROJECT)),
    }
    for name, text in texts.items():
        (OUT / name).write_text(text + "\n", encoding="utf-8", newline="\n")
    render_visual_pack(g, OUT / "demo-snapshot.png", PROJECT)
    mem = OUT / "_map"
    mem.mkdir(exist_ok=True)
    tokens = {"available": False, "reason": "Demo project: no token data."}
    print(write_map(g, mem, "recipe-app (demo)", PROJECT, tokens))
    for name in texts:
        print(OUT / name)


if __name__ == "__main__":
    main()
