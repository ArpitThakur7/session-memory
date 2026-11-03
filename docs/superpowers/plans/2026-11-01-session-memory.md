# Session Memory Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A free Claude Code plugin that builds a per-project memory graph from local transcripts with 0 model tokens, warns before an expensive cold-cache resume, and loads a brief under 3,000 tokens when a new session starts.

**Architecture:** Three hooks call one Python CLI. `Stop` and `SessionStart` incrementally parse Claude Code's transcript JSONL files into a JSON graph stored in the project. `SessionStart` then injects a short brief built from that graph. `UserPromptSubmit` runs the cache guard, which blocks a message once when the chat is idle and large. Two slash commands (`/remember`, `/recall`) add decisions and fetch details on demand.

**Tech Stack:** Python 3.10+ standard library only (pytest for tests), Claude Code plugin format (`.claude-plugin/plugin.json`, `hooks/hooks.json`, `commands/*.md`), Bash launcher script.

**Spec:** [Problem Definition: Low-Token Session Memory for Claude Code](https://claude.ai/artifact/KMNWyUHCQ4eQXmKvVdGj4p)

## Global Constraints

- Python 3.10 or newer; runtime code imports only the standard library.
- Free and local only: no network calls, no paid APIs, no accounts.
- Building the graph uses 0 model tokens (pure parsing of transcript files).
- Startup brief: at most 12,000 characters (about 3,000 tokens).
- Cache guard blocks only when idle > 55 minutes AND context > 30,000 tokens. Overrides: env `SESSION_MEMORY_IDLE_MIN`, `SESSION_MEMORY_MIN_TOKENS`.
- Cache guard behavior (open question in the spec, default chosen here): block once; the same message sent again within 10 minutes goes through.
- Hooks must never break Claude Code: on any exception, write the traceback to `error.log`, print nothing, exit 0.
- All file I/O is UTF-8 (`encoding="utf-8", errors="replace"` on read).
- Storage lives in the project: `<cwd>/.claude/session-memory/` holding `graph.json`, `state.json`, `error.log`.
- Works on Windows, macOS, Linux. Hooks run under `"shell": "bash"` (Git Bash on Windows).
- License MIT. Any reused open-source code keeps its notice and is credited in `README.md` (spec: "Use of open-source projects").

## Review Focus

1. **Crash mid-write** (internet drop, killed terminal): the last transcript line is cut off. Ingest must skip it without error and pick it up once complete. Pinned in Task 3.
2. **Hook exceptions**: a bad graph file or an unexpected transcript shape must not block the user's prompt or session. Pinned in Task 6.
3. **Missing data in the guard**: no assistant line yet, no `usage`, or unparseable timestamps → allow the message. Pinned in Task 5.
4. **Two sessions in one project at once**: both write `graph.json`. Writes must be atomic (temp file + `os.replace`) so the file is never half-written. Pinned in Task 2.
5. **Non-ASCII text and paths** (Hindi prompts, `C:\Users\...` paths): must round-trip through the graph and the brief unchanged. Pinned in Task 2.

---

## File Structure

```
.claude-plugin/plugin.json      plugin manifest
hooks/hooks.json                 SessionStart, UserPromptSubmit, Stop → bin/run-python
bin/run-python                   picks a Python >= 3.10 and runs memgraph.cli
commands/remember.md             /remember <decision>
commands/recall.md               /recall <term>
memgraph/__init__.py
memgraph/transcript.py           JSONL line → Event list (pure)
memgraph/graph.py                Graph store: nodes, edges, meta, load/save, apply(Event), search
memgraph/ingest.py               incremental ingest of a transcripts folder; storage paths
memgraph/brief.py                Graph → brief text under budget
memgraph/guard.py                idle/size check → block or allow
memgraph/cli.py                  subcommands; hook stdin/stdout contract; fail-safe wrapper
bench/measure.py                 token comparison: full history vs brief
tests/fixtures/session_a.jsonl   hand-written transcript (Task 1)
tests/test_*.py                  one test file per module
README.md, LICENSE
```

---

### Task 1: Project scaffold and transcript parser

**Files:**
- Create: `memgraph/__init__.py`, `memgraph/transcript.py`, `tests/__init__.py`, `tests/helpers.py`, `tests/fixtures/session_a.jsonl`, `tests/test_transcript.py`, `pyproject.toml` (only `[tool.pytest.ini_options] pythonpath = ["."]`), `LICENSE` (MIT), `.gitignore` (`__pycache__/`, `.pytest_cache/`, `.claude/session-memory/`)

Test helpers live in `tests/helpers.py` and are imported with `from tests.helpers import ...`. This task adds `FIXTURE: Path` (the fixture file) and `LINES: list[str]` (its lines). Task 2 adds `graph_from_fixture() -> Graph`. Task 3 adds `copy_fixture(tmp_path) -> Path` and `PROMPT_LINE: str`.

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Event: kind: str; ts: str; session: str; data: dict`
  - `kind` is one of `"prompt" | "edit" | "read" | "command" | "error" | "todo" | "usage"`
  - `parse_line(line: str) -> list[Event]` (invalid JSON or unknown type → `[]`)
  - `parse_lines(lines: Iterable[str]) -> list[Event]`

Mapping rules (from real transcripts inspected on 2026-11-01):
- `type == "user"`, `message.content` is a string (or text blocks) not starting with `<`, and `isMeta` is not true → `prompt`, `data={"text": first 200 chars}`.
- `type == "assistant"`, each `tool_use` block:
  - `Edit | Write | MultiEdit | NotebookEdit` → `edit` with `{"path": input.file_path or input.notebook_path, "id": block.id}`
  - `Read` → `read` with `{"path", "id"}`
  - `Bash | PowerShell` → `command` with `{"cmd": first 200 chars of input.command, "id"}`
  - `TodoWrite` (`input.todos`) or `mcp__ccd_turn__update_plan` (`input.items`, only when present) → `todo` with `{"items": [{"content", "status"}]}`
- `type == "assistant"` with `message.usage` → `usage` with `{"context": input_tokens + cache_read_input_tokens + cache_creation_input_tokens}`.
- `type == "user"`, `tool_result` block with `is_error: true` → `error` with `{"text": first 200 chars of content (string or joined text blocks), "tool_use_id"}`.
- `ts` = line `timestamp`, `session` = line `sessionId`. Every other line type is ignored.
- Within one assistant line, emit `tool_use` events in block order first, then the `usage` event.

- [ ] **Step 1: `git init`, create the folders, and write the fixture `tests/fixtures/session_a.jsonl`** with exactly these lines. The last line is deliberately cut off and has no trailing newline.

```
{"type":"user","sessionId":"s1","timestamp":"2026-11-01T10:00:00.000Z","message":{"role":"user","content":"Add login page to the app"}}
{"type":"assistant","sessionId":"s1","timestamp":"2026-11-01T10:00:05.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"Edit","input":{"file_path":"src/login.py","old_string":"a","new_string":"b"}}],"usage":{"input_tokens":10,"cache_read_input_tokens":40000,"cache_creation_input_tokens":2000,"output_tokens":50}}}
{"type":"assistant","sessionId":"s1","timestamp":"2026-11-01T10:00:09.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"t2","name":"Bash","input":{"command":"pytest tests/test_login.py"}}]}}
{"type":"user","sessionId":"s1","timestamp":"2026-11-01T10:00:20.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t2","is_error":true,"content":"FAILED test_login - AssertionError"}]}}
{"type":"assistant","sessionId":"s1","timestamp":"2026-11-01T10:00:30.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"t3","name":"TodoWrite","input":{"todos":[{"content":"Fix login test","status":"in_progress"},{"content":"Add logout","status":"pending"},{"content":"Create login form","status":"completed"}]}}]}}
{"type":"attachment","sessionId":"s1","timestamp":"2026-11-01T10:00:31.000Z"}
{"type":"user","sessionId":"s1","timestamp":"2026-11-01T10:01:00.000Z","message":{"role":"user","content":"<command-name>/clear</command-name>"}}
{"type":"user","sessionId":"s1","timestamp":"2026-11-01T10:02:00.000Z","message":{"role":"user","con
```

- [ ] **Step 2: Write failing tests in `tests/test_transcript.py`**

```python
def test_fixture_kinds_in_order():
    events = parse_lines(FIXTURE.read_text(encoding="utf-8").splitlines())
    assert [e.kind for e in events] == ["prompt", "edit", "usage", "command", "error", "todo"]

def test_prompt_text_and_session():
    e = parse_line(LINES[0])[0]
    assert (e.data["text"], e.session, e.ts) == ("Add login page to the app", "s1", "2026-11-01T10:00:00.000Z")

def test_usage_context_sums_three_fields():
    assert [e for e in parse_line(LINES[1]) if e.kind == "usage"][0].data["context"] == 42010

def test_error_keeps_tool_use_id():
    e = parse_line(LINES[3])[0]
    assert e.data == {"text": "FAILED test_login - AssertionError", "tool_use_id": "t2"}

def test_todo_items():
    assert parse_line(LINES[4])[0].data["items"][1] == {"content": "Add logout", "status": "pending"}

def test_ignored_lines():
    assert parse_line(LINES[5]) == [] and parse_line(LINES[6]) == [] and parse_line(LINES[7]) == []

def test_long_text_truncated_to_200():
    line = json.dumps({"type": "user", "sessionId": "s", "timestamp": "t", "message": {"content": "x" * 500}})
    assert len(parse_line(line)[0].data["text"]) == 200
```

- [ ] **Step 3: Run** `python -m pytest tests/test_transcript.py -v`. Expected: FAIL (`ModuleNotFoundError: memgraph.transcript`).
- [ ] **Step 4: Implement `parse_line` and `parse_lines` in `memgraph/transcript.py`** following the mapping rules above.
- [ ] **Step 5: Run** `python -m pytest tests/test_transcript.py -v`. Expected: 7 passed.
- [ ] **Step 6: Commit** `git add . && git commit -m "feat: transcript parser"`

---

### Task 2: Graph store

**Files:**
- Create: `memgraph/graph.py`, `tests/test_graph.py`

**Interfaces:**
- Consumes: `Event` from Task 1.
- Produces:
  - `class Graph` with attributes `nodes: dict[str, dict]`, `edges: dict[str, dict]` (key `"src|rel|dst"`), `meta: dict`
  - `Graph.load(path: Path) -> Graph` (missing or corrupt file → empty graph)
  - `Graph.save(self, path: Path) -> None` (atomic: write `path.with_suffix(".tmp")`, then `os.replace`; `ensure_ascii=False`, UTF-8)
  - `add_node(self, kind: str, key: str, label: str, ts: str) -> str` returns id `f"{kind}:{key}"`; node = `{"id","kind","label","first","last","count"}`; repeat calls bump `count` and `last`
  - `add_edge(self, src: str, rel: str, dst: str, ts: str) -> None`; edge = `{"src","rel","dst","count","last"}`
  - `apply(self, event: Event) -> None`
  - `search(self, term: str, limit: int = 20) -> list[dict]` (case-insensitive match on `label`, newest `last` first)

Node kinds and edges created by `apply`:
| Event | Nodes | Edges |
| --- | --- | --- |
| prompt | `task:<sha1(text)[:12]>` | `session:<id>` -asked-> task; sets `meta["current_task"][session]` and `meta["last_prompt"] = {"text","ts","session"}` |
| edit / read | `file:<path with "\\" → "/">` | current task -edited/read-> file (session node if no task yet) |
| command | `command:<sha1(cmd)[:12]>` | current task -ran-> command |
| error | `error:<sha1(text)[:12]>` | the node for `tool_use_id` -failed-> error (look up `meta["tool_nodes"]`, a dict capped at the newest 500 ids); session node otherwise |
| todo | none | sets `meta["todos"][session] = items` and `meta["last_todos"] = {"session","ts","items"}` |
| usage | none | sets `meta["sessions"][session]["context"]` |

Every event also updates `meta["sessions"][session]` `first` and `last` timestamps. `edit`/`read`/`command` events store `meta["tool_nodes"][data["id"]] = node_id`.

- [ ] **Step 1: Write failing tests in `tests/test_graph.py`**

```python
def test_fixture_builds_expected_nodes(tmp_path):
    g = graph_from_fixture()
    kinds = sorted(n["kind"] for n in g.nodes.values())
    assert kinds == ["command", "error", "file", "session", "task"]

def test_edit_links_task_to_file():
    g = graph_from_fixture()
    assert any(e["rel"] == "edited" and e["dst"] == "file:src/login.py" and e["src"].startswith("task:") for e in g.edges.values())

def test_error_links_to_failing_command():
    g = graph_from_fixture()
    assert any(e["rel"] == "failed" and e["src"].startswith("command:") for e in g.edges.values())

def test_last_todos_and_context():
    g = graph_from_fixture()
    assert g.meta["last_todos"]["items"][0]["content"] == "Fix login test"
    assert g.meta["sessions"]["s1"]["context"] == 42010

def test_repeat_node_counts(tmp_path):
    g = Graph(); g.add_node("file", "a.py", "a.py", "t1"); g.add_node("file", "a.py", "a.py", "t2")
    assert g.nodes["file:a.py"]["count"] == 2 and g.nodes["file:a.py"]["last"] == "t2"

def test_save_load_roundtrip_non_ascii(tmp_path):            # Review Focus 5
    g = Graph(); g.add_node("task", "k", "लॉगिन पेज जोड़ें", "t"); g.add_node("file", "C:/Users/अ/x.py", "C:/Users/अ/x.py", "t")
    g.save(tmp_path / "graph.json")
    assert Graph.load(tmp_path / "graph.json").nodes == g.nodes

def test_save_is_atomic(tmp_path, monkeypatch):               # Review Focus 4
    p = tmp_path / "graph.json"; Graph().save(p)
    calls = []; real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append((a, b)), real(a, b)))
    Graph().save(p)
    assert calls and Path(calls[0][1]) == p and not p.with_suffix(".tmp").exists()

def test_corrupt_file_loads_empty(tmp_path):
    p = tmp_path / "graph.json"; p.write_text("{not json", encoding="utf-8")
    assert Graph.load(p).nodes == {}

def test_search_case_insensitive():
    assert graph_from_fixture().search("LOGIN")[0]["kind"] in {"file", "task", "command", "error"}
```

`graph_from_fixture()` is a test helper: parse the Task 1 fixture and `apply` every event to a new `Graph`.

- [ ] **Step 2: Run** `python -m pytest tests/test_graph.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement `Graph` in `memgraph/graph.py`** with the interface and table above.
- [ ] **Step 4: Run** `python -m pytest tests -v`. Expected: all passed.
- [ ] **Step 5: Commit** `git commit -am "feat: graph store"` (add new files first).

---

### Task 3: Incremental ingest

**Files:**
- Create: `memgraph/ingest.py`, `tests/test_ingest.py`

**Interfaces:**
- Consumes: `parse_line` (Task 1), `Graph` (Task 2).
- Produces:
  - `memory_dir(cwd: Path) -> Path` returns `cwd / ".claude" / "session-memory"` (created if missing)
  - `load_state(path: Path) -> dict` / `save_state(path: Path, state: dict) -> None` (same atomic write as `Graph.save`; missing or corrupt → `{}`)
  - `ingest_dir(transcripts_dir: Path, graph: Graph, state: dict) -> int` returns the number of events applied

Rules: for each `*.jsonl` in `transcripts_dir` (not recursive), open in binary, seek to `state["offsets"][name]` (default 0), read to EOF, and process only up to the last `b"\n"`. Advance the offset by exactly the bytes consumed. A file shorter than its stored offset (rewritten) restarts from 0.

- [ ] **Step 1: Write failing tests in `tests/test_ingest.py`**

```python
def test_ingest_fixture_counts_six_events(tmp_path):
    d = copy_fixture(tmp_path)
    assert ingest_dir(d, Graph(), {}) == 6

def test_second_run_reads_nothing_new(tmp_path):
    d = copy_fixture(tmp_path); g, s = Graph(), {}
    ingest_dir(d, g, s)
    assert ingest_dir(d, g, s) == 0

def test_cut_off_line_is_read_once_completed(tmp_path):       # Review Focus 1
    d = copy_fixture(tmp_path); g, s = Graph(), {}
    ingest_dir(d, g, s)
    f = d / "session_a.jsonl"
    f.write_text(f.read_text(encoding="utf-8").rsplit("\n", 1)[0] + "\n" + PROMPT_LINE + "\n", encoding="utf-8")
    assert ingest_dir(d, g, s) == 1

def test_rewritten_file_restarts(tmp_path):
    d = copy_fixture(tmp_path); g, s = Graph(), {}
    ingest_dir(d, g, s); (d / "session_a.jsonl").write_text(PROMPT_LINE + "\n", encoding="utf-8")
    assert ingest_dir(d, g, s) == 1

def test_memory_dir_location(tmp_path):
    assert memory_dir(tmp_path) == tmp_path / ".claude" / "session-memory" and memory_dir(tmp_path).is_dir()
```

`PROMPT_LINE` = a complete `user` line with content `"Add logout"`, session `s1`. `copy_fixture` copies `session_a.jsonl` into `tmp_path / "proj"`.

- [ ] **Step 2: Run** `python -m pytest tests/test_ingest.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `memory_dir`, `load_state`, `save_state`, `ingest_dir` in `memgraph/ingest.py`.** Move the atomic-write code into a shared helper `write_json_atomic(path: Path, obj) -> None` in `memgraph/graph.py` and use it from both modules.
- [ ] **Step 4: Run** `python -m pytest tests -v`. Expected: all passed.
- [ ] **Step 5: Commit** `git commit -m "feat: incremental transcript ingest"`

---

### Task 4: Startup brief

**Files:**
- Create: `memgraph/brief.py`, `tests/test_brief.py`

**Interfaces:**
- Consumes: `Graph` (Task 2).
- Produces: `build_brief(graph: Graph, budget_chars: int = 12000) -> str` (empty graph → `""`)

Brief layout, in priority order (lowest priority is trimmed first when over budget):
1. `# Project memory (auto-loaded by session-memory)`, then `Last request (<ts date+time>): <last_prompt.text>`
2. `## Next steps`: `last_todos` items whose status is not `completed`, max 10, as `- [<status>] <content>`
3. `## Decisions`: `decision` nodes, newest 10, as `- <label>`
4. `## Recently changed files`: `file` nodes that have an incoming `edited` edge, newest 15 by `last`
5. `## Recent errors`: `error` nodes, newest 5 by `last`
6. Footer: `For older details run /recall <word>. Do not re-read files listed here unless needed.`

Trimming: drop whole lines from the bottom of the lowest-priority section first (5, then 4, then 3, then 2). Sections 1 and 6 are never dropped; if they alone are over budget, cut the last-request text.

- [ ] **Step 1: Write failing tests in `tests/test_brief.py`**

```python
def test_empty_graph_gives_empty_brief():
    assert build_brief(Graph()) == ""

def test_fixture_brief_contents():
    b = build_brief(graph_from_fixture())
    assert "Last request" in b and "Add login page to the app" in b
    assert "- [in_progress] Fix login test" in b and "- [pending] Add logout" in b
    assert "Create login form" not in b
    assert "src/login.py" in b and "FAILED test_login" in b and "/recall" in b

def test_decisions_section():
    g = graph_from_fixture(); g.add_node("decision", "d1", "Use SQLite because the app is single-user", "2026-11-01T11:00:00Z")
    assert "- Use SQLite because the app is single-user" in build_brief(g)

def test_budget_enforced_and_low_priority_dropped_first():
    g = graph_from_fixture()
    for i in range(400):
        g.add_node("error", f"e{i}", "E" * 100, f"2026-11-01T12:{i % 60:02d}:00Z")
        g.add_node("file", f"f{i}.py", f"f{i}.py" + "x" * 80, "2026-11-01T12:00:00Z")
        g.add_edge("task:x", "edited", f"file:f{i}.py", "2026-11-01T12:00:00Z")
    b = build_brief(g, budget_chars=1500)
    assert len(b) <= 1500 and "Next steps" in b and "/recall" in b
```

- [ ] **Step 2: Run** `python -m pytest tests/test_brief.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `build_brief`** in `memgraph/brief.py` as specified.
- [ ] **Step 4: Run** `python -m pytest tests -v`. Expected: all passed.
- [ ] **Step 5: Commit** `git commit -m "feat: startup brief"`

---

### Task 5: Cache guard

**Files:**
- Create: `memgraph/guard.py`, `tests/test_guard.py`

**Interfaces:**
- Produces:
  - `@dataclass class GuardConfig: idle_minutes: int = 55; min_tokens: int = 30000; repeat_window_minutes: int = 10`
  - `GuardConfig.from_env() -> GuardConfig` (reads `SESSION_MEMORY_IDLE_MIN`, `SESSION_MEMORY_MIN_TOKENS`; bad values → defaults)
  - `last_activity(transcript_path: Path) -> tuple[datetime | None, int | None]`: reads only the last 256 KB, returns the UTC timestamp of the newest `assistant` line and the newest `usage` context (via `parse_line`)
  - `decide(now: datetime, last_ts: datetime | None, context: int | None, prompt: str, state: dict, cfg: GuardConfig) -> str | None`: returns the block reason, or `None` to allow. Mutates `state["guard"]`.

Rules: allow if `last_ts` or `context` is None, if idle ≤ `idle_minutes`, if `context` ≤ `min_tokens`, or if `prompt` starts with `/`. Also allow if `state["guard"]` holds the same `sha1(prompt.strip())` recorded within `repeat_window_minutes`, then clear it. Otherwise record `{"hash", "at": now ISO}` and return the reason.

Exact reason text:

```
Cache guard: this chat has been idle for {idle} minutes, so its prompt cache has likely expired. Sending now re-reads about {context:,} tokens at full price.
Cheaper: type /clear. Your project memory loads automatically as a short brief.
To send anyway, send the same message again within 10 minutes.
```

- [ ] **Step 1: Write failing tests in `tests/test_guard.py`**

```python
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

def test_blocks_idle_and_large():
    r = decide(NOW, NOW - timedelta(minutes=90), 140000, "continue", {}, GuardConfig())
    assert r.startswith("Cache guard: this chat has been idle for 90 minutes") and "140,000 tokens" in r

def test_allows_short_idle():
    assert decide(NOW, NOW - timedelta(minutes=20), 140000, "go", {}, GuardConfig()) is None

def test_allows_small_chat():
    assert decide(NOW, NOW - timedelta(hours=5), 12000, "go", {}, GuardConfig()) is None

def test_allows_slash_commands():
    assert decide(NOW, NOW - timedelta(hours=5), 140000, "/clear", {}, GuardConfig()) is None

def test_same_message_again_goes_through():
    s = {}; cfg = GuardConfig(); old = NOW - timedelta(hours=3)
    assert decide(NOW, old, 140000, "continue", s, cfg) is not None
    assert decide(NOW + timedelta(minutes=2), old, 140000, " continue ", s, cfg) is None

def test_repeat_after_window_blocks_again():
    s = {}; cfg = GuardConfig(); old = NOW - timedelta(hours=3)
    decide(NOW, old, 140000, "continue", s, cfg)
    assert decide(NOW + timedelta(minutes=11), old, 140000, "continue", s, cfg) is not None

def test_missing_data_allows():                               # Review Focus 3
    assert decide(NOW, None, 140000, "go", {}, GuardConfig()) is None
    assert decide(NOW, NOW - timedelta(hours=3), None, "go", {}, GuardConfig()) is None

def test_last_activity_from_fixture():
    ts, ctx = last_activity(FIXTURE)
    assert ts == datetime(2026, 11, 1, 10, 0, 30, tzinfo=timezone.utc) and ctx == 42010

def test_env_overrides(monkeypatch):
    monkeypatch.setenv("SESSION_MEMORY_IDLE_MIN", "30"); monkeypatch.setenv("SESSION_MEMORY_MIN_TOKENS", "abc")
    cfg = GuardConfig.from_env()
    assert (cfg.idle_minutes, cfg.min_tokens) == (30, 30000)
```

- [ ] **Step 2: Run** `python -m pytest tests/test_guard.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `GuardConfig`, `last_activity`, `decide`** in `memgraph/guard.py`. Parse timestamps with `datetime.fromisoformat(ts.replace("Z", "+00:00"))`; unparseable → None.
- [ ] **Step 4: Run** `python -m pytest tests -v`. Expected: all passed.
- [ ] **Step 5: Commit** `git commit -m "feat: cache guard"`

---

### Task 6: CLI, hooks and plugin wiring

**Files:**
- Create: `memgraph/cli.py`, `bin/run-python`, `hooks/hooks.json`, `.claude-plugin/plugin.json`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces: `main(argv: list[str], stdin: TextIO, stdout: TextIO, now: datetime | None = None) -> int` and `if __name__ == "__main__": sys.exit(main(sys.argv[1:], sys.stdin, sys.stdout))`

Subcommands. Hook stdin is JSON with `session_id`, `transcript_path`, `cwd`, `hook_event_name`, plus `prompt` (UserPromptSubmit) and `source` (SessionStart).
| Subcommand | Behavior | Stdout |
| --- | --- | --- |
| `hook-stop` | load graph and state from `memory_dir(cwd)`, `ingest_dir(dirname(transcript_path))`, save both | nothing |
| `hook-session-start` | same ingest, then `build_brief` | if brief non-empty: `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": <brief>}}` |
| `hook-prompt` | `last_activity(transcript_path)`, `decide(...)` with state from `memory_dir(cwd)`; save state | if blocked: `{"decision": "block", "reason": <reason>}` |
| `remember <text...>` | add `decision` node (key `sha1(text)[:12]`); if `meta["last_prompt"]` exists, link that session's current task to it with `decided`; save | `Saved to project memory.` |
| `recall <term>` | `graph.search(term, 20)` | one line per node: `[<kind>] <label> (last <date>)`, max 2,000 chars; none → `Nothing found for "<term>".` |
| `stats` | counts | `nodes=<n> edges=<n> sessions=<n> brief_chars=<n>` |

`remember`, `recall` and `stats` use `Path.cwd()` as the project. Every `hook-*` subcommand runs inside `try/except Exception`. On error it appends the traceback to `error.log` (falling back to `tempfile.gettempdir()/session-memory-error.log` if the memory dir is unknown or unusable), prints nothing, and returns 0.

`bin/run-python` (bash, `chmod +x`): set `ROOT` to the plugin root (the parent of `bin`). Pick the first of `python3`, `python`, `py` for which `"$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))'` succeeds; this skips the Windows Store `python3` stub. Then run `PYTHONPATH="$ROOT" exec "$c" -m memgraph.cli "$@"`. If none is found, exit 0 silently.

`hooks/hooks.json`:
```json
{"hooks": {
  "SessionStart": [{"matcher": "startup|clear", "hooks": [{"type": "command", "shell": "bash", "command": "\"${CLAUDE_PLUGIN_ROOT}/bin/run-python\" hook-session-start"}]}],
  "UserPromptSubmit": [{"hooks": [{"type": "command", "shell": "bash", "command": "\"${CLAUDE_PLUGIN_ROOT}/bin/run-python\" hook-prompt"}]}],
  "Stop": [{"hooks": [{"type": "command", "shell": "bash", "command": "\"${CLAUDE_PLUGIN_ROOT}/bin/run-python\" hook-stop"}]}]
}}
```

`.claude-plugin/plugin.json`: `name` `"session-memory"`, `version` `"0.1.0"`, `description` `"Zero-token project memory graph, cache guard and short startup brief for Claude Code"`, `license` `"MIT"`.

- [ ] **Step 1: Write failing tests in `tests/test_cli.py`** (each test copies the fixture to `tmp_path/"transcripts"` and uses `tmp_path/"proj"` as `cwd`)

```python
def test_session_start_outputs_brief_json(tmp_path):
    out = run(["hook-session-start"], hook_input(tmp_path))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "Fix login test" in ctx and (tmp_path / "proj/.claude/session-memory/graph.json").exists()

def test_session_start_empty_project_prints_nothing(tmp_path):
    assert run(["hook-session-start"], hook_input(tmp_path, transcripts=False)) == ""

def test_prompt_hook_blocks_cold_large_chat(tmp_path):
    out = run(["hook-prompt"], hook_input(tmp_path, prompt="continue"), now=datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc), min_tokens=1000)
    assert json.loads(out)["decision"] == "block"

def test_prompt_hook_allows_warm_chat(tmp_path):
    assert run(["hook-prompt"], hook_input(tmp_path, prompt="go"), now=datetime(2026, 11, 1, 10, 5, tzinfo=timezone.utc)) == ""

def test_hook_never_raises(tmp_path):                         # Review Focus 2
    rc, out = run_rc(["hook-stop"], "not json at all")
    assert rc == 0 and out == ""

def test_remember_then_recall(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run(["remember", "Use", "SQLite", "for", "users"], "") == "Saved to project memory.\n"
    assert "[decision] Use SQLite for users" in run(["recall", "sqlite"], "")
```

`run`/`run_rc` call `main` with `io.StringIO`. `min_tokens` is set through `monkeypatch.setenv("SESSION_MEMORY_MIN_TOKENS", ...)`.

- [ ] **Step 2: Run** `python -m pytest tests/test_cli.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `memgraph/cli.py`, `bin/run-python`, `hooks/hooks.json`, `.claude-plugin/plugin.json`** as specified.
- [ ] **Step 4: Run** `python -m pytest tests -v`. Expected: all passed.
- [ ] **Step 5: Smoke test the launcher:** `echo '{}' | bin/run-python hook-stop; echo "rc=$?"`. Expected output: `rc=0`.
- [ ] **Step 6: Commit** `git commit -m "feat: CLI and Claude Code hooks"`

---

### Task 7: Slash commands

**Files:**
- Create: `commands/remember.md`, `commands/recall.md`

**Interfaces:**
- Consumes: `remember` and `recall` subcommands (Task 6) through `${CLAUDE_PLUGIN_ROOT}/bin/run-python`.

- `commands/remember.md`: frontmatter `description: Save a key decision and its reason to project memory`, `argument-hint: [decision]`, `allowed-tools: Bash(*run-python*)`. Body tells Claude: rewrite `$ARGUMENTS` (or, if empty, the most important decision made since the last /remember) as one line under 160 characters in the form `<decision> because <reason>`. Then run `"${CLAUDE_PLUGIN_ROOT}/bin/run-python" remember "<line>"` once and reply with only the saved line.
- `commands/recall.md`: frontmatter `description: Search project memory`, `argument-hint: [word]`, `allowed-tools: Bash(*run-python*)`. Body: run `"${CLAUDE_PLUGIN_ROOT}/bin/run-python" recall "$ARGUMENTS"` and use the result. Do not open the listed files unless the task needs them.

- [ ] **Step 1: Write both files.**
- [ ] **Step 2: Manual check** (after Task 8 Step 3 loads the plugin): `/remember use JSON storage because no dependencies are allowed`, then `/recall json`. Expected: the decision line comes back.
- [ ] **Step 3: Commit** `git commit -m "feat: /remember and /recall commands"`

---

### Task 8: Benchmark, README and live test

**Files:**
- Create: `bench/__init__.py`, `bench/measure.py`, `tests/test_measure.py`, `README.md`

**Interfaces:**
- Consumes: `parse_line`, `Graph`, `ingest_dir`, `build_brief`.
- Produces: `measure(transcripts_dir: Path) -> list[dict]` with one row per transcript: `{"session": name, "history_tokens": newest usage context, "brief_tokens": len(build_brief(<graph of that transcript alone>)) // 4, "saved_pct": round(100 * (1 - brief/history), 1)}`. Transcripts without usage are skipped. The CLI prints a markdown table. Usage: `python bench/measure.py <transcripts_dir>`.

- [ ] **Step 1: Write failing test** `tests/test_measure.py`

```python
def test_measure_fixture(tmp_path):
    rows = measure(copy_fixture(tmp_path))
    assert rows[0]["history_tokens"] == 42010 and rows[0]["brief_tokens"] < 3000 and rows[0]["saved_pct"] > 90
```

- [ ] **Step 2: Run, implement `bench/measure.py`, run again.** Expected: FAIL, then PASS.
- [ ] **Step 3: Live test in Claude Code:** `claude --plugin-dir <this repo>` inside a real project (for example `<your project>`). Check:
  1. After one reply, `.claude/session-memory/graph.json` exists.
  2. `/clear` → the new session's first answer knows the last request and next steps.
  3. Set `SESSION_MEMORY_IDLE_MIN=0` and `SESSION_MEMORY_MIN_TOKENS=1000`, then send a message: the guard blocks it. Send it again: it goes through.
- [ ] **Step 4: Run** `python bench/measure.py "%USERPROFILE%\.claude\projects\<your project folder>"` and paste the table into `README.md`.
- [ ] **Step 5: Continuity check (spec success criterion):** do 10 `/clear` resumes during real work. Record in `README.md` how many times the first answer named the current task and next step correctly. Target: at least 9 of 10.
- [ ] **Step 6: Write `README.md`:** what it does, install (`claude --plugin-dir`, or a marketplace entry later), configuration env vars, where data is stored (add `.claude/session-memory/` to `.gitignore`), the benchmark table, credits for open-source projects used (spec section "Use of open-source projects"), and limits (transcript format is not a public API).
- [ ] **Step 7: Commit** `git commit -m "docs: benchmark and README"`
