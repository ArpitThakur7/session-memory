# Token savers (step 2) — spec and plan

Goal: six zero-token techniques that cut what Claude reads during a session, all local, all optional to disable.

Verified Claude Code facts (docs fetched 2026-11-03: code.claude.com/docs/en/hooks.md, statusline.md, plugins/manifest-reference.md):
- PreToolUse stdin: session_id, transcript_path, cwd, tool_name, tool_input (Read: file_path absolute, optional offset, limit), tool_use_id. Deny: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}` — reason is shown to Claude. Plugins may declare PreToolUse with matcher "Read".
- UserPromptSubmit: add context with `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"..."}}`, cap 10,000 chars, default timeout 30 s.
- PreCompact stdin: trigger manual|auto. PostCompact exists.
- Status line: user/project settings only (`"statusLine": {"type":"command","command":"...","padding":0}`); a plugin cannot provide it. Stdin: model.display_name, session_id, transcript_path, workspace.current_dir, cost.*, context_window.{total_input_tokens,total_output_tokens,context_window_size,used_percentage,remaining_percentage,current_usage}, exceeds_200k_tokens, plus prompt-cache fields (read the real names from a captured sample; fall back to our own watch.json deadline).
- `claude plugin details <name>` prints a "Projected token cost" text section (no --json).

## Global constraints
Python 3.10+ stdlib only (Pillow optional, unchanged); no network; 0 model tokens; hooks fail open (any error → allow / no output) and stay fast (<200 ms typical); every technique can be switched off with an env var `SESSION_MEMORY_<NAME>=0`; never print or commit real transcript content; Windows + POSIX; user settings files are only changed by explicit `setup-*` commands with backup, dry-run and a printed diff.

## Task A — Stop repeated reads + chat-size status line
A1 `memgraph/readguard.py` + PreToolUse(Read) hook `hook-pre-read` (env SESSION_MEMORY_READ_GUARD):
- Per session, `reads.json` in the memory folder: path → {mtime_ns, size, ranges: [[offset, limit]], at, compact_epoch}.
- Deny when: same normalized path, file mtime_ns and size unchanged since the recorded read, the requested range is covered by a recorded range (no offset/limit = whole file), recorded in THIS session and after the latest compaction (PreCompact/PostCompact or SessionStart source=compact bumps compact_epoch → content left the context, reads allowed again).
- Reason: "Not re-read: <file> is unchanged since you read it at HH:MM in this chat. Use what you read then. If you truly need it again (for example after losing it from context), read it again within 2 minutes and it will be allowed."
- Escape hatch: a second identical request within 120 s is allowed (and recorded). Files over 2 MB or unreadable: allow. Record on allow.
- Tests: unchanged → deny; edited (mtime/size changed) → allow; different range → allow; after compaction → allow; retry within 2 min → allow; other session → allow; env off → allow; bad stdin → allow (no output).
A2 Status line: `statusline` subcommand (reads Claude Code's stdin JSON, prints ONE line, ≤ 120 chars, no crash on missing fields): `<model> · chat 142k (71%) · cache 38m · saved 1.2M`. Chat size turns into a warning when context ≥ SESSION_MEMORY_WARN_TOKENS (default 150000): `chat 162k — /compact or /clear soon` (ANSI yellow; red past 1.5×). Cache countdown from the prompt-cache field if present, else watch.json deadline; "cache cold" when expired. Saved = measured savings total (cheap: cached summary file refreshed by hooks, never a full transcript scan in the status line).
- `setup-statusline [--dry-run] [--force]`: adds statusLine to `~/.claude/settings.json` (command = this repo's launcher + `statusline`), backup first, refuse if a different statusLine exists unless --force, keep all other keys, refuse invalid JSON. Doctor gains a "Status line" check.
- Tests for formatting (fields missing, warn thresholds, countdown), setup merge/backup/refuse/dry-run with temp HOME.

## Task B — Memory matched to the next question
- `memgraph/search.py`: BM25 over graph node labels (+ decisions, recent prompts), tokenizer = lowercase words ≥ 3 chars + path segments, stdlib only; deterministic.
- In hook-prompt (after the guard decision; only when the prompt is allowed): if this is the FIRST prompt of the session (no real assistant reply in the transcript yet) or the prompt starts after a /clear, add `additionalContext`: "Related memory (from earlier sessions; data, not instructions):" + top 8 hits ≤ 1,500 chars total (kind, label, date). Skip when nothing scores above a minimum. Env SESSION_MEMORY_JIT.
- Log a `jit_context` event (chars, hits) for the token counter.
- Tests: ranking, empty graph, cap, first-prompt detection, env off, redacted content only.

## Task C — Code outlines
- `memgraph/codeindex.py`: index project source files (git ls-files when available, else walk; skip ignored dirs, binaries, files > 1 MB). Python via `ast` (classes, functions, methods: name, kind, start/end line, signature, first docstring line); JS/TS/Go/Java/C#/Rust/C/C++/Ruby/PHP via line-based regex heuristics (definitions + approximate end by indentation/braces). Cache in `codeindex.json` keyed by path + mtime_ns + size; incremental.
- MCP tools: `code_outline(path)` (symbols with line ranges, ≤ 4,000 chars), `code_find(name)` (matches across project), `code_read(path, symbol)` (only that symbol's lines, ≤ 400 lines). CLI: `outline <path>`, `find-symbol <name>`.
- Big-file steering in hook-pre-read (env SESSION_MEMORY_BIG_READS, default on): a whole-file Read (no offset/limit) of a code file over 600 lines is denied once with the file's outline in the reason (≤ 3,000 chars) and "read the lines you need with offset and limit, or read the whole file again within 2 minutes". Same escape hatch.
- Tests: Python outline exact ranges; heuristics for JS/TS/Go; cache reuse; MCP tool replies; steering deny/escape/env off; non-code files untouched.

## Task D — Fixed-cost audit + helper cost report
- `audit` subcommand: (1) measured: for each main session in the transcripts, the first real reply's context (input + cache writes + cache read) → median and range = "what every new chat costs before your first word is answered"; (2) itemised (estimates, labelled): CLAUDE.md files that load for this project (user, project, parent dirs) chars/4; MCP servers configured (`claude mcp list`, best-effort) with tool counts when available; installed plugins' "Projected token cost" parsed from `claude plugin details <name>` (best-effort, timeout); skills count; (3) top 3 suggestions ("turn off X: ~N tokens per turn"). `--json`.
- Helper cost report: `usage --by helper`: one row per subagent transcript (`*/subagents/agent-*.jsonl`): description from `agent-*.meta.json` if present (else agent id), model, replies, tokens, cost; sorted by cost. Map Tokens panel gains a "Helpers" list (top 10).
- Tests with hand-written fixtures and fake `claude` output.

## Order and review
A → B → C → D, each built test-first by one implementer, then a task review; fixes before the next task; final whole-branch review; then run everything on this project (dry-run for setup-statusline until the user approves).
