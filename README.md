# session-memory

A free Claude Code plugin that keeps a small memory of your project, so you can start a fresh chat instead of resuming a huge one.

## Quick start

1. Download or clone this repository, then run the installer from its folder. It finds Python 3.10 or newer, offers to install Pillow (optional), adds the plugin to Claude Code and checks the setup.

   Windows (PowerShell or cmd; the script is a local file, nothing is downloaded):

   ```
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```

   macOS, Linux or Git Bash:

   ```
   ./install.sh
   ```

   Add `-DryRun` (Windows) or `--dry-run` to see every step without changing anything, `-Yes`/`--yes` to install Pillow without asking, `-NoPillow`/`--no-pillow` to skip it, and `-Uninstall`/`--uninstall` to remove the plugin again (memory folders in your projects are left alone).

2. Check a project. Run this inside your project folder (use the real folder where you keep this repository):

   ```
   C:\Users\you\Desktop\session-memory\bin\run-python.cmd doctor
   /path/to/session-memory/bin/run-python doctor
   ```

   Each line is ✓ (fine), ✗ (needs a fix, shown below it) or – (unknown or optional). Add `--project PATH` to check another folder, or `--json` for machine-readable output.

3. Open Claude Code in your project, work as usual, and use `/memory-map` to see the memory.

> **Unofficial.** session-memory is a community project. It is not made, endorsed or supported by Anthropic, and is not affiliated with Anthropic.

## What it does

- **Memory graph at 0 model tokens.** Plain Python reads Claude Code's local transcripts and keeps a small graph of your requests, files, commands, errors, todos and decisions.
- **Startup brief.** Each new or cleared session starts with a short summary of where you left off.
- **Resume packs and a one-image snapshot.** Bigger summaries, as text or as one picture, when you need more than the brief.
- **Cache-expiry watcher and guard.** A desktop notification before the prompt cache expires, and a one-time warning before an expensive resume.
- **Read guard.** Stops Claude from reading the same unchanged file twice in one chat.
- **Status line (optional).** Chat size, cache time left and tokens saved, always visible.
- **MCP server.** The same memory for VS Code, Antigravity and other MCP clients.
- **Token counter.** Exact token use and measured savings from your transcripts.
- **3D memory map.** A self-contained page to explore the memory.

Everything runs on your machine; nothing is sent anywhere (see [Privacy](#privacy)).

## Quick install

You need Python 3.10 or newer on your PATH. In Claude Code, add this repository as a plugin marketplace and install the plugin:

```
/plugin marketplace add ArpitThakur7/session-memory
/plugin install session-memory@session-memory
```

Or, from a local checkout, start Claude Code with the plugin loaded:

```
claude --plugin-dir <path to this repo>
```

## Why it exists

Built to fix a restart-cost problem. After a usage-limit reset, or after an hour of idle time, continuing a long Claude Code chat re-sends the whole history at full price, because the prompt cache has expired. In the author's own use, one such restart cost roughly 20-30% of a weekly limit.

session-memory does three things:

1. **Builds a memory graph** of each project (files touched, commands run, errors, todos, your requests) from Claude Code's local transcripts. This costs 0 model tokens: it is plain Python reading files on your disk.
2. **Loads a short brief** (at most 9,500 characters, about 2,400 tokens; usually far less) at the start of every new or cleared session, so Claude knows the session's title, the last few requests, where things stood, open todos and recently changed files without re-reading the old chat.
3. **Warns before an expensive resume** (the cache guard). If the chat has been idle long enough for its cache to expire and it is large, your message is blocked once with the estimated cost and a cheaper option (`/clear`). Send the same message again within 10 minutes to go ahead anyway.

## Install

You need Python 3.10 or newer on your PATH. Install from the marketplace:

```
/plugin marketplace add ArpitThakur7/session-memory
/plugin install session-memory@session-memory
```

Or start Claude Code with the plugin loaded from a local checkout:

```
claude --plugin-dir <path to this repo>
```

No other setup is needed; the hooks start working immediately.
The memory folder ignores itself in git: the plugin writes a `.gitignore` containing `*` inside `.claude/session-memory/`, so the graph (which holds your prompts, commands and errors) is not committed by accident.

## Commands

- `/remember <decision>` saves one line, "decision because reason", to project memory. Use it for the "why" behind choices.
- `/recall <word>` searches project memory for older details that are not in the brief.
- `/memory-map` opens a 3D map of the project's memory (see below).
- `/memory-snapshot` makes a one-image summary of this project's chat history (about 1,590 tokens). The next chat can read it after `/clear`. Needs Pillow (`pip install pillow`).

## See your memory

`/memory-map` opens a 3D map of the project's memory in your browser. From a terminal, `bin/run-python map` does the same and costs no tokens at all (`bin/run-python map --no-open` only writes the file).

- Drag to rotate, scroll or pinch to zoom, right-drag (or Shift-drag) to pan, double-click empty space to fit everything.
- Each session is a gray dot, with its requests around it. Click a dot to open it: its files, commands, errors and decisions burst out of it, and the side panel shows its full text, when it was seen and what it is connected to. Click it again to fold it back up. A "+N" badge means there is more inside. A dashed ring stands for "+K more", and clicking it shows the rest.
- Search covers everything, including folded-up parts: click a result to fly to it. The colored chips hide or show a kind of item. "Where you left off" repeats the brief. The Snapshot view shows the one-image summary (`memory.png`, redrawn each time the map is built), opens it full size on click and has a "Save image" link; it needs Pillow.

The page is one self-contained file, `.claude/session-memory/memory-map.html`. It loads nothing from the network and is git-ignored like the rest of the memory folder. Run the command again to refresh it.

## Resume packs

The brief is small on purpose. A resume pack is a bigger summary that you ask for when you need more. It is made from the same memory graph and costs 0 model tokens to build.

Run this inside your project folder. Use the real folder where you keep this plugin (for example `C:\Users\you\Desktop\session-memory`).

Windows (PowerShell or cmd):

```
C:\Users\you\Desktop\session-memory\bin\run-python.cmd pack --size standard
```

macOS, Linux or Git Bash:

```
/path/to/session-memory/bin/run-python pack --size standard
```

- `--size tiny` is about 2,000 characters. `standard` (the default) is about 8,000. `deep` is about 24,000.
- It writes `.claude/session-memory/resume-pack.md`.
- It also draws `.claude/session-memory/memory.png`, a 1092 x 1092 picture of the same story (about 1,590 image tokens). This needs the Pillow package (`pip install pillow`). Without Pillow the picture is skipped and the text pack still works. Add `--no-visual` to skip the picture.
- The pack's third line says when it was built ("Built YYYY-MM-DD HH:MM", your local time). The picture shows the same line at the top.
- Each new session (including after `/clear`) rebuilds `resume-pack.md` from the latest memory before the brief is made, so the pack the brief points at is never older than the brief. With nothing to pack yet, no pack file is written.
- When these files exist, the brief adds one line for each, so Claude knows where to look. The picture line is added only when `memory.png` is at least as new as `resume-pack.md`; an older picture shows an older state, so the brief leaves it out. Run `pack` again (or let the cache watcher do it) to refresh the picture.

To compare the sizes on your own transcripts, use the full path to the plugin. You can run this from any folder:

```
python C:\Users\you\Desktop\session-memory\bench\compare_packs.py "C:\Users\you\.claude\projects\your-project-folder"
python C:\Users\you\Desktop\session-memory\bench\compare_packs.py "C:\Users\you\.claude\projects\your-project-folder" --questions
```

Use your real user name and project folder. Do not type angle brackets. The first command prints a table: history tokens, then tiny, standard and deep pack tokens, then the picture. With `--questions` it also prints 10 questions with the expected answers. Ask a fresh chat the same questions with each pack and count the right answers. The answers come from your own transcripts, so keep that output on your machine.

## Token counter

See how many tokens this project used and how much the plugin saved. It reads Claude Code's local transcripts, so it costs 0 model tokens and nothing leaves your machine. Run it inside your project folder (on Windows, `bin\run-python.cmd`):

```
bin/run-python usage                 # last 7 days, one row per day
bin/run-python usage --days 30 --by model    # or --by session, --by who (main chat vs helper agents)
bin/run-python savings               # measured savings and missed savings
```

Add `--json` for machine-readable output, or `--transcripts PATH` to point at a transcripts folder. Without it the folder the hooks last saw is used, else `~/.claude/projects/<project path with every non-letter/digit as ->`. The 3D map has a "Tokens" section (last 14 days, by model, by who, cache hit rate, saved vs missed), and the MCP tool `memory_usage` gives the same totals.

**What is measured (exact):**

- Every token number comes from the `usage` field Claude Code records on each model reply: input, cache writes (5-minute and 1-hour when the transcript says which), cache reads and output. Helper-agent (subagent) transcripts are included.
- Claude Code writes the same reply several times while streaming. Copies with the same message id and request id are counted once; the totals line says how many were skipped. Synthetic lines (usage-limit notices, API errors) are not replies and are skipped.
- Cache hit = cache reads / (input + cache writes + cache reads).
- **Measured saved** (`savings`): each time a new or cleared session starts with the brief, the hook logs the old chat's context size. The new chat's first real reply records what it actually read. Saved = old context - new first-reply context (never below 0). Both numbers come from transcripts. Only fresh starts made after this feature was installed are counted.

**What is estimated:**

- **Missed (estimate)**: when a main chat was resumed after more than `SESSION_MEMORY_CACHE_TTL_MIN` idle minutes (default 60), the next reply rebuilt the cache. That rebuild is measured; what a fresh start would have cost instead is estimated as the latest brief's characters / 4 (500 tokens when no brief size is known). "Could have saved" = rebuild - that estimate.
- **Dollars** use Anthropic's list prices as of 2026-10-06 (shown next to every dollar figure). Cache writes cost 1.25 x input (5 minutes) or 2 x input (1 hour); a write without a TTL breakdown is priced as a 5-minute write. Cache reads cost 0.1 x input unless the price list says otherwise. A fresh start's dollars are "saved (priced at old chat's cache state)": what resuming the old chat would have cost, a cache read if its last reply was within the cache time (warm) or a cache write if not (cold, using that reply's 5-minute/1-hour split), minus what the new chat's first reply actually cost. This can be negative when the old chat was small and still warm. Rebuilt tokens are priced as the reply's cache writes. The map's savings figures are all-time; its usage figures cover the last 14 days. A model missing from the price table shows "—" and the rest of the total is marked "partial". Your plan may not bill per token; the dollars show list-price value.

## Cache watcher

After each reply, a small background process watches the clock. Claude Code's prompt cache lasts about 60 minutes by default. Before it ends, you get a desktop notification, so you can decide what to do before the next message becomes expensive.

- The warning comes 10 minutes before the cache ends. If the cache time is short, it comes at half of that time.
- If your computer was asleep and the time has passed, the notification says the cache "has expired".
- Before it warns, it reads the transcript again. During a long turn Claude keeps replying without finishing, so the cache stays warm; the watcher then moves the deadline and keeps waiting instead of warning.
- Set `SESSION_MEMORY_CACHE_TTL_MIN` to change the cache time in minutes. The default is 60.
- It is silent when it works. Errors go to `.claude/session-memory/error.log`.
- Limit: there is one deadline for each project. If you have two chats open in the same project, the newest reply sets the deadline for both.

## Read guard

Claude often reads the same file again in one chat, and every re-read adds the whole file to the chat once more. The read guard is a pair of hooks on the Read tool: `PostToolUse` notes each read that worked, and `PreToolUse` refuses a new read when all of these are true:

- the same file (on Windows, `C:\a\b.py` and `c:/a/b.py` count as one file) was read successfully in this chat;
- the file has not changed since (same modification time and size);
- the requested lines were already read (a read without `offset`/`limit` counts as the first 2,000 lines);
- that read was less than 45 minutes ago (older tool results may have been cleared from the chat);
- the chat has not been compacted since (after `/compact` or an automatic compaction, reads are allowed again).

Claude then sees: "Not re-read: <file> is unchanged since you read it at HH:MM in this chat. Use what you read then..." If it really needs the file again, the same read within 2 minutes goes through. A read that was declined or failed is never noted, so it is never refused later. Helper agents (subagents) have their own record, so they are never refused a file only the main chat read. Files over 2 MB, PDF page reads and reads with an `offset` or `limit` that is not a number are never refused. The record lives in `reads.json` in the project's memory folder.

**Turn it off:** start Claude Code with `SESSION_MEMORY_READ_GUARD=0`.

## Status line

An optional status line under the prompt box:

```
Opus · chat 142k (71%) · cache 38m · saved 1.2M
```

- **chat**: tokens in the chat now and the share of the context window. From `SESSION_MEMORY_WARN_TOKENS` (default 150000) it turns yellow and says `chat 162k — /compact or /clear soon`; past 1.5 times that, red.
- **cache**: time until the prompt cache expires, from Claude Code's `prompt_cache.expires_at` field (Claude Code 2.1.251 or later), else from the cache watcher's deadline; `cache cold` once it has expired.
- **saved**: measured tokens saved by fresh starts (the `savings` command's measured total), read from a small `summary.json` that the Stop hook refreshes. The status line never reads transcripts.

Claude Code does not let a plugin set the status line, so it is a separate step that edits your Claude Code user settings (`~/.claude/settings.json`, or `settings.json` in `CLAUDE_CONFIG_DIR` when that is set):

```
bin/run-python setup-statusline --dry-run   # show the change only
bin/run-python setup-statusline             # add it (a backup of settings.json is saved first)
```

On Windows use `bin\run-python.cmd` with the full path to this repository. It keeps every other setting, refuses a file that is not valid JSON, and refuses to replace a different status line unless you add `--force`. `doctor` shows whether it is set. To remove it, delete the `statusLine` entry from the settings file (or set `SESSION_MEMORY_STATUSLINE=0` to make it print nothing).

## Use the memory in other tools

The plugin also runs as an MCP server, so other tools can read the same memory. The tools are `memory_brief`, `memory_recall`, `memory_remember`, `memory_pack` and `memory_usage` (token totals and savings for the last `days` days, default 7), plus the code tools `code_outline`, `code_find` and `code_read` (see "Code outlines").

## Use in VS Code / Antigravity

Run `setup-mcp` inside your project folder (on Windows, `bin\run-python.cmd`; elsewhere `bin/run-python`, with the full path to this repository):

```
bin/run-python setup-mcp vscode         # writes <project>/.vscode/mcp.json
bin/run-python setup-mcp antigravity    # adds an entry to ~/.gemini/antigravity/mcp_config.json
bin/run-python setup-mcp claude         # runs: claude mcp add -s user session-memory ...
```

- It prints what it will change first. Add `--dry-run` to only print it, and `--project PATH` for another folder. For `claude`, `--scope user|project|local` picks the scope (default `user`).
- Other servers and settings in the file are kept. The old file is copied to `<file>.<date-time>.bak` first. Running it again says "already set up", or updates an old session-memory entry.
- It refuses to touch a file that is not valid JSON, or that has comments, and prints the entry to add by hand.
- Restart the editor afterwards. `doctor` shows which tools have the server.

### Manual setup (fallback)

Two words in the snippets below are placeholders, not text to type:

- `<repo>` means the folder where you keep this project, for example `C:\Users\you\Desktop\session-memory`.
- `<your project>` means the folder of the project you work on.

Replace them with your real folders. In PowerShell, do not type the angle brackets `<` and `>`.

On Windows, use `bin\run-python.cmd` (PowerShell cannot run `bin/run-python`, which is a bash script). On macOS, Linux or Git Bash, use `bin/run-python`.

**Claude Code.** Run it inside your project folder. The server is then added for that project only. Add `-s user` after `add` to use it in all projects.

Windows:

```
claude mcp add session-memory -- "<repo>\bin\run-python.cmd" mcp --project "<your project>"
```

macOS, Linux or Git Bash:

```
claude mcp add session-memory -- "<repo>/bin/run-python" mcp --project "<your project>"
```

**VS Code.** Create `.vscode/mcp.json` in your project.

Windows:

```json
{"servers": {"session-memory": {"type": "stdio", "command": "py", "args": ["-3", "-m", "memgraph.cli", "mcp", "--project", "${workspaceFolder}"], "env": {"PYTHONPATH": "<repo>"}}}}
```

This is the recommended Windows form: it starts Python directly through the `py` launcher, because VS Code (an Electron app) may refuse to start a `.cmd` file. Write `<repo>` with forward slashes (`C:/Users/you/Desktop/session-memory`). If `py` is not installed, put the full path to `python.exe` in `command` (for example `C:/Program Files/Python313/python.exe`) and drop `"-3"` from `args`.

Alternative, through the launcher script:

```json
{"servers": {"session-memory": {"type": "stdio", "command": "<repo>/bin/run-python.cmd", "args": ["mcp", "--project", "${workspaceFolder}"]}}}
```

macOS, Linux or Git Bash:

```json
{"servers": {"session-memory": {"type": "stdio", "command": "<repo>/bin/run-python", "args": ["mcp", "--project", "${workspaceFolder}"]}}}
```

In JSON, write a Windows path with forward slashes (`C:/Users/you/Desktop/session-memory`) or with double backslashes (`C:\Users\you\Desktop\session-memory`).

**Antigravity.** Open `~/.gemini/antigravity/mcp_config.json`. On Windows this is `C:\Users\you\.gemini\antigravity\mcp_config.json`. Do not replace the file: it already has other servers. Add this entry inside `mcpServers`:

```json
"session-memory": {"command": "py", "args": ["-3", "-m", "memgraph.cli", "mcp", "--project", "<your project>"], "env": {"PYTHONPATH": "<repo>"}}
```

Antigravity on Windows may not run bash scripts, so this entry calls Python directly through the `py` launcher. If `py` is not installed, put the full path to `python.exe` in `command` (for example `C:/Program Files/Python313/python.exe`) and drop `"-3"` from `args`. On macOS or Linux use `"command": "python3"` and drop `"-3"`. In JSON files, write a Windows path with double backslashes (`C:\Users\you\Desktop\session-memory`) or with forward slashes (`C:/Users/you/Desktop/session-memory`).

## Code outlines

Reading a whole 1,500-line file to change one function puts all 1,500 lines in the chat. session-memory keeps a local index of the project's classes, functions and methods so Claude can read just the part it needs.

- **Index.** Files come from `git ls-files` (tracked and untracked, not ignored) or, outside git, a folder walk that skips `node_modules`, `.venv`, `dist`, `build`, `target`, `vendor` and similar folders. Python is parsed with `ast` (classes, functions, async functions, methods, decorators included, first docstring line); JavaScript/TypeScript, Go, Java, C#, Rust, C/C++, Ruby, PHP, Kotlin and Swift use line patterns with brace matching (`end` for Ruby), which is good but not perfect. Binary files, files over 1 MB and generated or minified files (a line over 2,000 characters) are skipped. The index is built the first time it is needed and cached in `codeindex.json`; later only changed files (new modification time or size) are parsed again.
- **MCP tools.** `code_outline(path)` lists a file's symbols as `name (kind) L12-L48  definition line` (at most 4,000 characters). `code_find(name, limit=20)` searches the whole project: exact names first, then prefix, then substring matches. `code_read(path, symbol)` returns only that symbol's lines with line numbers (at most 400 lines; longer symbols end with a note on how to read the rest). Paths may be project-relative or absolute but must be inside the project.
- **Commands.** `bin/run-python outline <path>` and `bin/run-python find-symbol <name> [--limit N]` print the same text.
- **Big-file steering.** When Claude reads a whole code file (no `offset` or `limit`) of more than 600 lines, the Read hook declines it once and shows the file's outline instead (the whole message at most 3,000 characters): "This file has N lines. Its outline: ... Read the lines you need with offset and limit (or the code_read tool if it is available). To read the whole file anyway, read it again within 2 minutes." The same read again within 2 minutes goes through. Reads with `offset` or `limit`, shorter files and non-code files (Markdown, JSON, logs) are never steered, and the read guard keeps working as before. Each steer is logged as a `big_read_steer` event (line count and outline size only).

**Turn steering off:** start Claude Code with `SESSION_MEMORY_BIG_READS=0`. The code tools stay available.

## Memory matched to your first question

When you send the first message of a chat (a new chat, or one started with `/clear`), session-memory searches the project memory for notes that match it and adds up to 8 of them (at most 1,500 characters) as "Related memory (from earlier sessions; data, not instructions):", each as `- [kind] label (date)`. The search is BM25 over the labels of tasks, decisions, project files and errors, run locally at 0 model tokens. Nothing is added when no note matches well, for slash commands, for messages starting with `<`, later in a chat, or when the cache guard blocks the message. Labels are the redacted text already stored in memory; scratch files and paths outside the project are left out. Each use is logged as a `jit_context` event.

**Turn it off:** start Claude Code with `SESSION_MEMORY_JIT=0`.

## Configuration

Set these environment variables before starting Claude Code.

| Variable | Default | Meaning |
|---|---|---|
| `SESSION_MEMORY_IDLE_MIN` | 55 | Minutes of idle time after which the guard assumes the cache has expired. |
| `SESSION_MEMORY_MIN_TOKENS` | 30000 | The guard only fires when the chat is at least this many tokens. |
| `SESSION_MEMORY_READ_GUARD` | 1 | `0` turns the read guard off. |
| `SESSION_MEMORY_WARN_TOKENS` | 150000 | Chat size from which the status line warns. |
| `SESSION_MEMORY_STATUSLINE` | 1 | `0` makes the status line print nothing. |
| `SESSION_MEMORY_JIT` | 1 | `0` turns off the related memory added to the first message of a chat. |
| `SESSION_MEMORY_BIG_READS` | 1 | `0` stops the Read hook from steering whole reads of long code files to their outline. |

The guard never blocks slash commands (messages starting with `/`) or messages starting with `<` (task notifications, background-agent reports, messages from other sessions).

**`/loop` and scheduled prompts.** A prompt that `/loop` or a scheduled task sends on its own is an ordinary message to the guard. If the chat is large and the loop's interval is longer than `SESSION_MEMORY_IDLE_MIN` minutes, that prompt is blocked and nobody is there to send it again, so the loop stalls. For such chats, start Claude Code with `SESSION_MEMORY_IDLE_MIN` set above the longest interval (for example `SESSION_MEMORY_IDLE_MIN=180` for a 2-hour loop), or with a very large `SESSION_MEMORY_MIN_TOKENS` to switch the guard off.

## Where data is stored

Everything is under `<project>/.claude/session-memory/`:

- `graph.json` - the memory graph
- `state.json` - how far each transcript has been read, and where the transcripts are
- `graph.lock` - held for a moment while something writes `graph.json`, so two writers never overwrite each other (a lock older than 30 seconds is ignored)
- `guard.json` - the cache guard's "already warned about this message" note
- `events.jsonl` - one line per guard block and per fresh start with a brief (times, session ids and sizes only, no text), used by `savings`
- `watch.json` - the cache watcher's deadline, warning time and which deadline was already announced
- `watch.lock` - the running cache watcher's process id, so only one watcher runs per project
- `reads.json` - the read guard's record: per chat, which files were read (normalized path, modification time, size, line ranges, times), no file contents
- `codeindex.json` - the code outline cache: per source file, its modification time, size, line count and symbols (names, kinds, line ranges, definition lines, first docstring lines)
- `summary.json` - measured-saved totals for the status line (numbers only)
- `resume-pack.md` and `memory.png` - the resume packs (made by the `pack` command, the cache watcher, and, for `resume-pack.md`, each new session)
- `memory-map.html` - the 3D memory map page (made by `/memory-map` or the `map` command)
- `error.log` - any internal errors (the plugin never blocks your work if it fails)
- `.gitignore` - contains `*`, so none of the above is committed

Nothing leaves your machine. Delete the folder to reset the memory.

**Secrets are hidden before they are stored.** Prompts, commands, error text, file paths, replies, todos, titles and `/remember` notes go through a redaction filter first (`memgraph/redact.py`). API keys (Anthropic, OpenAI, GitHub, AWS, Slack, Google, Stripe), JWTs, private-key blocks, `Bearer`/`Authorization` values, passwords in URLs, `--password`/`--token`/`--api-key` values and `NAME=value` settings whose name contains KEY, TOKEN, SECRET, PASSWORD, PASSWD or CREDENTIAL are replaced with `[hidden]`. It is a best-effort filter: it cannot recognise every kind of secret.

**Upgrading from 0.1.0:** an existing memory is not rewritten. To hide secrets in old entries, rebuild the memory: delete `graph.json` and `state.json` in `.claude/session-memory/`. The next reply rebuilds both from your transcripts, with redaction.

## Benchmark

`python bench/measure.py <transcripts_dir>` compares, for each transcript, the context size Claude Code recorded on its newest reply (what a resume would re-send) with the estimated size of the brief built from that transcript alone (characters / 4). Run on the author's local transcripts from four of their own projects (one project folder had no transcripts and is left out; project names are replaced with letters):

| project | history tokens | brief tokens | saved % |
|---|---:|---:|---:|
| Project A | 86270 | 933 | 98.9 |
| Project B | 549671 | 810 | 99.9 |
| Project B | 326085 | 775 | 99.8 |
| Project B | 17981 | 246 | 98.6 |
| Project B (worktree) | 70705 | 174 | 99.8 |
| Project C | 188500 | 361 | 99.8 |
| Project D | 707427 | 845 | 99.9 |
| Project D | 407675 | 712 | 99.8 |
| Project D | 84554 | 161 | 99.8 |

These are 9 sessions from one person, so treat them as examples, not an average. The brief's size depends on how much work a session recorded (todos, edited files, errors, `/remember` notes): here it ranged from 161 to 933 tokens, and it can grow to the 9,500-character cap (about 2,400 tokens) in a busy project. The saving is largest for long chats; for a short chat a resume is cheap anyway. The brief carries a summary, not the full conversation, so it is smaller than the history by design. What it loses is the detail, which is why `/recall` and `/remember` exist.

## Results to fill in

The live test needs an interactive Claude Code session and spends usage limit, so it has not been run yet. Fill this in after trying it.

**Live-test checklist.** Install with `claude --plugin-dir <path to this repo>` inside a real project, then check:

- [ ] After one reply, `.claude/session-memory/graph.json` exists.
- [ ] After `/clear`, the new session's first answer knows the last request and the next steps.
- [ ] With `SESSION_MEMORY_IDLE_MIN=0` and `SESSION_MEMORY_MIN_TOKENS=1000` set, sending a message is blocked by the guard; sending the same message again goes through.
- [ ] With `SESSION_MEMORY_CACHE_TTL_MIN=4` set, a desktop notification appears about 2 minutes after a reply.
- [ ] After `/clear`, the brief contains the "Full resume pack" line (and the "Visual summary" line when the picture is current), and `resume-pack.md` contains your latest request (the "Built" time stays the same when nothing changed).
- [ ] After a reply, close Claude Code: the cache watcher still shows its notification.
- [ ] In VS Code and in Antigravity, the session-memory server lists 8 tools: `memory_brief`, `memory_recall`, `memory_remember`, `memory_pack`, `memory_usage`, `code_outline`, `code_find`, `code_read`.

**Continuity check.** Do 10 `/clear` resumes during real work. Record whether the first answer named the current task and the next step correctly. Target: at least 9 of 10.

| Resume | Named current task? | Named next step? |
|---|---|---|
| 1 | | |
| 2 | | |
| 3 | | |
| 4 | | |
| 5 | | |
| 6 | | |
| 7 | | |
| 8 | | |
| 9 | | |
| 10 | | |
| **Total (target 9/10)** | / 10 | / 10 |

## Limits

- **The transcript format is not a public API.** The plugin reads Claude Code's local transcript files. A Claude Code update could change that format and break the parsing until the plugin is updated. Errors are logged, not shown, and never block your work.
- **The "why" needs `/remember`.** Transcripts show what happened, not always why. Decisions and reasons are only reliably kept if you save them with `/remember`.
- **Nothing is zero tokens.** Building the graph costs none, but the brief itself is read by the model every session. It is capped at 9,500 characters (about 2,400 tokens), below Claude Code's 10,000-character limit for hook context.
- The cache guard uses an idle-time estimate, not a real view of Anthropic's cache, so it can be wrong in either direction.

## Privacy

- **Everything is local.** The plugin makes no network requests. Building the memory, the brief, the packs, the map and the token counts is plain Python on your machine.
- **What it reads:** Claude Code's transcript files for the current project (`~/.claude/projects/<project folder>/`, including helper-agent transcripts) and the hook input Claude Code passes on stdin.
- **What it writes:** only files in `<your project>/.claude/session-memory/` (listed in [Where data is stored](#where-data-is-stored)). That folder ignores itself in git.
- **Secrets are hidden** with `[hidden]` before anything is stored. The filter is best-effort, so treat the memory folder as private, like the transcripts themselves. Memory built before 0.2.0 is not rewritten: delete `graph.json` and `state.json` to rebuild it.
- **The transcript format is not a public API.** Claude Code may change it at any time, which can break or change what the plugin reads until it is updated.

See `SECURITY.md` for how to report a security problem.

## Credits

No code was copied from other projects; these were inspiration only:

- [claude-mem](https://github.com/thedotmack/claude-mem), memory for Claude Code sessions.
- [ccusage](https://github.com/ryoppippi/ccusage), token usage from Claude Code's local transcripts.
- The official [MCP memory server](https://github.com/modelcontextprotocol/servers/tree/main/src/memory), a knowledge-graph memory over MCP.

Also studied for ideas: Cognee's Claude Code plugin (docs.cognee.ai), CodeLoupe (github.com/Terrio-cz/CodeLoupe) and Serena. Rule for this project: code from MIT, Apache or BSD projects may be reused only with its license notice; GPL or AGPL projects are used for ideas only.

session-memory is unofficial and not affiliated with Anthropic. "Claude" and "Claude Code" are Anthropic's names for their products.

## Contributing

See `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` and `CHANGELOG.md`.

## License

MIT. See `LICENSE`.
