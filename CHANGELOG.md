# Changelog

All notable changes to this project are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Fixed

- **Cache guard let model-running slash commands through.** Any prompt starting with `/` skipped the guard, so `/session-memory:memory-map` as the first message of a cold 886k-token chat rebuilt the whole cache (840,843 tokens written). Now only built-in commands that do not call the model (`/clear`, `/compact`, `/model`, `/help` and similar) pass; plugin commands, skills, custom commands and model-running built-ins (`/init`, `/review`, ...) are blocked once like a normal message. The block message now points to `/clear` and to the launcher command that opens the memory map without Claude.

### Added

- **Installers.** `install.ps1` (Windows PowerShell 5.1 and 7) and `install.sh` (macOS, Linux, Git Bash) find Python, offer Pillow, install the plugin and run `doctor`. Both have a dry-run and an uninstall mode.
- **`doctor`.** Checks Python, Pillow, plugin files, the `claude` CLI, the plugin install, transcripts, memory, the cache watcher and MCP setup, each with a one-line fix.
- **`setup-mcp claude|vscode|antigravity`.** Adds the MCP server with a backup, an atomic write and `--dry-run`.
- **Read guard.** `PostToolUse` notes each successful Read; `PreToolUse` refuses to re-read a file that is unchanged since it was read in the same chat (same lines, within 45 minutes, no compaction since). The same read again within 2 minutes is allowed. Off with `SESSION_MEMORY_READ_GUARD=0`. Compaction (`PreCompact`, `SessionStart` with source `compact`) resets it.
- **Status line.** `statusline` prints `<model> · chat 142k (71%) · cache 38m · saved 1.2M`, warning from `SESSION_MEMORY_WARN_TOKENS` (default 150000). `setup-statusline [--dry-run] [--force]` adds it to `~/.claude/settings.json` (or `CLAUDE_CONFIG_DIR`) with a backup and a printed diff; `doctor` checks it. The Stop hook keeps a small `summary.json` so the status line never reads transcripts. Setup writes a stable shim (`session-memory/statusline.py|.sh|.cmd` beside the settings file) that finds the current plugin install, so updates do not break the status line; `doctor` checks it. `chat —` before the first reply; windows over 200k warn at 75% used.
- **Memory matched to your first question.** The first message of a chat (or after `/clear`) gets up to 8 matching notes from project memory (BM25, at most 1,500 characters, redacted text only) as extra context. Nothing is added when nothing matches or the cache guard blocks. Near-duplicate labels are shown once, errors are capped at 2 and need two matching words, deleted files are skipped, and any absolute path outside the project (POSIX or Git Bash `/c/...` too) becomes `[path]`. Off with `SESSION_MEMORY_JIT=0`.
- **Code outlines.** A local symbol index (`codeindex.json`, incremental by modification time and size; `git ls-files` or a folder walk; Python via `ast`, JS/TS, Go, Java, C#, Rust, C/C++, Ruby, PHP, Kotlin and Swift via line patterns). MCP tools `code_outline`, `code_find` and `code_read`, and the `outline <path>` and `find-symbol <name>` commands.
- **Big-file steering.** A whole Read of a code file over 600 lines is declined once with the file's outline (at most 3,000 characters); the same read within 2 minutes goes through, and a file is steered at most once per chat until compaction. Logged as `big_read_steer` events; reads taken anyway after a denial or steer are logged as `read_retry` and `audit` subtracts them. Off with `SESSION_MEMORY_BIG_READS=0`.

- **`audit [--project PATH] [--json]`.** Measures what every new chat costs before your first message is answered (median, smallest, largest first-reply context over your main chats), itemises CLAUDE.md files, MCP servers, plugins (from `claude plugin details`) and skills as labelled estimates, lists the top 3 things to switch off, and counts this week's read-guard denials and big-file steers (saved, minus retries) and related-memory additions (added). A missing `claude` marks rows "not checked".
- **Helper costs.** `usage --by helper` gives one row per helper-agent transcript (description, model, replies, tokens, cost, biggest first); the map's Tokens section lists the top 10 and `memory_usage` the top 5.

### Changed

- Hooks keep one memory folder per project: they use `CLAUDE_PROJECT_DIR` when Claude Code sets it, so a chat that moved into a subfolder no longer creates a second `.claude/session-memory` there.
- `usage`, `savings`, `audit` and the map ignore a recorded transcripts folder that no longer exists and fall back to `CLAUDE_CONFIG_DIR/projects/<slug>` (or `~/.claude/projects/<slug>`).
- The Stop hook refreshes the status line's `summary.json` only when a new `fresh_start` event was logged, not on every read, steer or guard event.
- `audit`, `usage`, `savings`, `outline` and `find-symbol` no longer create a memory folder in the folder they look at.
- Code outlines end with an ASCII "... N more symbols" line that names no tool. Helper descriptions are redacted before they reach the map or MCP replies.
- Faster hooks: the CLI imports only what each command needs, and `bin/run-python.cmd` starts Python once (`py -3` directly; the version check moved into the CLI). `bin/run-python` (bash) also starts Python once and skips the Windows Store `python3` stub without running it.

## 0.2.0

### Added

- **Resume packs.** `pack --size tiny|standard|deep` writes `resume-pack.md` from the memory graph at 0 model tokens; each new session rebuilds it before the brief.
- **One-image snapshot.** `memory.png`, a 1092 x 1092 picture of the same story (about 1,590 image tokens), and the `/memory-snapshot` command. Needs Pillow; everything else works without it.
- **Cache-expiry watcher.** A background process warns with a desktop notification before the prompt cache expires (`SESSION_MEMORY_CACHE_TTL_MIN`).
- **MCP server.** `memory_brief`, `memory_recall`, `memory_remember`, `memory_pack` and `memory_usage` for Claude Code, VS Code, Antigravity and other MCP clients.
- **Token counter.** `usage` and `savings` commands: exact token use from transcripts, cache hit rate, and measured and estimated savings.
- **Secret redaction.** API keys, tokens, JWTs, private keys, URL passwords and secret settings are replaced with `[hidden]` before anything is stored. Memory built by 0.1.0 is not rewritten; delete `graph.json` and `state.json` to rebuild it.
- Plugin marketplace manifest (`.claude-plugin/marketplace.json`), CI on Linux, macOS and Windows, and community files.

### Changed

- **3D memory map redesign.** Sessions as dots that open into their files, commands, errors and decisions; search, kind filters, "Where you left off", a Tokens section and a Snapshot view.
- Helper-agent (subagent) transcripts are read too; only project-work errors are kept.

## 0.1.0

### Added

- Plugin core: a memory graph built from Claude Code's local transcripts at 0 model tokens (files, commands, errors, todos, requests, titles).
- A short startup brief on new and cleared sessions.
- A cache guard that warns once before an expensive resume of a large, idle chat.
- `/remember` and `/recall` commands, and `bench/measure.py`.
