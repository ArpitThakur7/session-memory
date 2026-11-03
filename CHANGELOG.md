# Changelog

All notable changes to this project are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- **Installers.** `install.ps1` (Windows PowerShell 5.1 and 7) and `install.sh` (macOS, Linux, Git Bash) find Python, offer Pillow, install the plugin and run `doctor`. Both have a dry-run and an uninstall mode.
- **`doctor`.** Checks Python, Pillow, plugin files, the `claude` CLI, the plugin install, transcripts, memory, the cache watcher and MCP setup, each with a one-line fix.
- **`setup-mcp claude|vscode|antigravity`.** Adds the MCP server with a backup, an atomic write and `--dry-run`.
- **Read guard.** A `PreToolUse` hook on Read refuses to re-read a file that is unchanged since it was read in the same chat (same lines, no compaction since). The same read again within 2 minutes is allowed. Off with `SESSION_MEMORY_READ_GUARD=0`. Compaction (`PreCompact`, `SessionStart` with source `compact`) resets it.
- **Status line.** `statusline` prints `<model> · chat 142k (71%) · cache 38m · saved 1.2M`, warning from `SESSION_MEMORY_WARN_TOKENS` (default 150000). `setup-statusline [--dry-run] [--force]` adds it to `~/.claude/settings.json` with a backup and a printed diff; `doctor` checks it. The Stop hook keeps a small `summary.json` so the status line never reads transcripts.

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
