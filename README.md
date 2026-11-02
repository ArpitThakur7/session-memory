# session-memory

A free Claude Code plugin that keeps a small memory of your project, so you can start a fresh chat instead of resuming a huge one.

## Why it exists

Built to fix a restart-cost problem. After a usage-limit reset, or after an hour of idle time, continuing a long Claude Code chat re-sends the whole history at full price, because the prompt cache has expired. In the author's own use, one such restart cost roughly 20-30% of a weekly limit.

session-memory does three things:

1. **Builds a memory graph** of each project (files touched, commands run, errors, todos, your requests) from Claude Code's local transcripts. This costs 0 model tokens: it is plain Python reading files on your disk.
2. **Loads a short brief** (at most 9,500 characters, about 2,400 tokens; usually far less) at the start of every new or cleared session, so Claude knows the session's title, the last few requests, where things stood, open todos and recently changed files without re-reading the old chat.
3. **Warns before an expensive resume** (the cache guard). If the chat has been idle long enough for its cache to expire and it is large, your message is blocked once with the estimated cost and a cheaper option (`/clear`). Send the same message again within 10 minutes to go ahead anyway.

## Install

You need Python 3.10 or newer on your PATH. Then start Claude Code with the plugin loaded:

```
claude --plugin-dir <path to this repo>
```

(A marketplace entry may come later.) No other setup is needed; the hooks start working immediately.

The memory folder ignores itself in git: the plugin writes a `.gitignore` containing `*` inside `.claude/session-memory/`, so the graph (which holds your prompts, commands and errors) is not committed by accident.

## Commands

- `/remember <decision>` saves one line, "decision because reason", to project memory. Use it for the "why" behind choices.
- `/recall <word>` searches project memory for older details that are not in the brief.
- `/memory-map` opens a 3D map of the project's memory (see below).

## See your memory

`/memory-map` opens a 3D map of the project's memory in your browser. From a terminal, `bin/run-python map` does the same and costs no tokens at all (`bin/run-python map --no-open` only writes the file).

- Drag to rotate, scroll or pinch to zoom, right-drag (or Shift-drag) to pan, double-click empty space to fit everything.
- Each session is a gray dot, with its requests around it. Click a dot to open it: its files, commands, errors and decisions burst out of it, and the side panel shows its full text, when it was seen and what it is connected to. Click it again to fold it back up. A "+N" badge means there is more inside. A dashed ring stands for "+K more", and clicking it shows the rest.
- Search covers everything, including folded-up parts: click a result to fly to it. The colored chips hide or show a kind of item. "Where you left off" repeats the brief.

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

## Cache watcher

After each reply, a small background process watches the clock. Claude Code's prompt cache lasts about 60 minutes by default. Before it ends, you get a desktop notification, so you can decide what to do before the next message becomes expensive.

- The warning comes 10 minutes before the cache ends. If the cache time is short, it comes at half of that time.
- If your computer was asleep and the time has passed, the notification says the cache "has expired".
- Before it warns, it reads the transcript again. During a long turn Claude keeps replying without finishing, so the cache stays warm; the watcher then moves the deadline and keeps waiting instead of warning.
- Set `SESSION_MEMORY_CACHE_TTL_MIN` to change the cache time in minutes. The default is 60.
- It is silent when it works. Errors go to `.claude/session-memory/error.log`.
- Limit: there is one deadline for each project. If you have two chats open in the same project, the newest reply sets the deadline for both.

## Use the memory in other tools

The plugin also runs as an MCP server, so other tools can read the same memory. The tools are `memory_brief`, `memory_recall`, `memory_remember` and `memory_pack`.

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

## Configuration

Set these environment variables before starting Claude Code.

| Variable | Default | Meaning |
|---|---|---|
| `SESSION_MEMORY_IDLE_MIN` | 55 | Minutes of idle time after which the guard assumes the cache has expired. |
| `SESSION_MEMORY_MIN_TOKENS` | 30000 | The guard only fires when the chat is at least this many tokens. |

The guard never blocks slash commands (messages starting with `/`) or messages starting with `<` (task notifications, background-agent reports, messages from other sessions).

**`/loop` and scheduled prompts.** A prompt that `/loop` or a scheduled task sends on its own is an ordinary message to the guard. If the chat is large and the loop's interval is longer than `SESSION_MEMORY_IDLE_MIN` minutes, that prompt is blocked and nobody is there to send it again, so the loop stalls. For such chats, start Claude Code with `SESSION_MEMORY_IDLE_MIN` set above the longest interval (for example `SESSION_MEMORY_IDLE_MIN=180` for a 2-hour loop), or with a very large `SESSION_MEMORY_MIN_TOKENS` to switch the guard off.

## Where data is stored

Everything is under `<project>/.claude/session-memory/`:

- `graph.json` - the memory graph
- `state.json` - how far each transcript has been read
- `graph.lock` - held for a moment while something writes `graph.json`, so two writers never overwrite each other (a lock older than 30 seconds is ignored)
- `guard.json` - the cache guard's "already warned about this message" note
- `watch.json` - the cache watcher's deadline, warning time and which deadline was already announced
- `watch.lock` - the running cache watcher's process id, so only one watcher runs per project
- `resume-pack.md` and `memory.png` - the resume packs (made by the `pack` command, the cache watcher, and, for `resume-pack.md`, each new session)
- `error.log` - any internal errors (the plugin never blocks your work if it fails)
- `.gitignore` - contains `*`, so none of the above is committed

Nothing leaves your machine. Delete the folder to reset the memory.

## Benchmark

`python bench/measure.py <transcripts_dir>` compares, for each transcript, the context size Claude Code recorded on its newest reply (what a resume would re-send) with the estimated size of the brief built from that transcript alone (characters / 4). Run on the author's local transcripts, every project folder under `~/.claude/projects` whose name starts with `C--Users-admin-Desktop-` (one folder had no transcripts and is left out):

| project folder | session | history tokens | brief tokens | saved % |
|---|---|---:|---:|---:|
| CalcVault | fbeac537-5818-4897-ae1a-0f242c948eaf.jsonl | 86270 | 933 | 98.9 |
| JOB-PREDICTION | 19342041-33a9-4a50-9918-a8156435415c.jsonl | 549671 | 810 | 99.9 |
| JOB-PREDICTION | e46aa26e-c223-4690-97ad-653b7c605f3b.jsonl | 326085 | 775 | 99.8 |
| JOB-PREDICTION | 51cfe12e-3da9-4d2e-b662-15f054945ac2.jsonl | 17981 | 246 | 98.6 |
| JOB-PREDICTION (worktree) | cfd1b0af-e75d-4671-8f88-8dee8da2ed87.jsonl | 70705 | 174 | 99.8 |
| RepoQuest | af0934d1-91c1-4909-87f6-f027c2fb3721.jsonl | 188500 | 361 | 99.8 |
| Sql-bot | 979c499b-b5d3-4689-86de-264081666608.jsonl | 707427 | 845 | 99.9 |
| Sql-bot | b6918e82-2d01-4895-9870-c5dcbdfcec84.jsonl | 407675 | 712 | 99.8 |
| Sql-bot | 1774b924-82ca-4ce7-90ff-29571f146622.jsonl | 84554 | 161 | 99.8 |

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
- [ ] In VS Code and in Antigravity, the session-memory server lists 4 tools: `memory_brief`, `memory_recall`, `memory_remember`, `memory_pack`.

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

## Credits

No code was copied from other projects. These were studied for ideas: the official MCP Memory server (modelcontextprotocol/servers), claude-mem (docs.claude-mem.ai), Cognee's Claude Code plugin (docs.cognee.ai), CodeLoupe (github.com/Terrio-cz/CodeLoupe) and Serena. Rule for this project: code from MIT, Apache or BSD projects may be reused only with its license notice; GPL or AGPL projects are used for ideas only.

## License

MIT. See `LICENSE`.
