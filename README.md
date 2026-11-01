# session-memory

A free Claude Code plugin that keeps a small memory of your project, so you can start a fresh chat instead of resuming a huge one.

## Why it exists

Built to fix a restart-cost problem. After a usage-limit reset, or after an hour of idle time, continuing a long Claude Code chat re-sends the whole history at full price, because the prompt cache has expired. In the author's own use, one such restart cost roughly 20-30% of a weekly limit.

session-memory does three things:

1. **Builds a memory graph** of each project (files touched, commands run, errors, todos, your requests) from Claude Code's local transcripts. This costs 0 model tokens: it is plain Python reading files on your disk.
2. **Loads a short brief** (under 3,000 tokens) at the start of every new or cleared session, so Claude knows the last request, open todos and recent files without re-reading the old chat.
3. **Warns before an expensive resume** (the cache guard). If the chat has been idle long enough for its cache to expire and it is large, your message is blocked once with the estimated cost and a cheaper option (`/clear`). Send the same message again within 10 minutes to go ahead anyway.

## Install

You need Python 3.10 or newer on your PATH. Then start Claude Code with the plugin loaded:

```
claude --plugin-dir <path to this repo>
```

(A marketplace entry may come later.) No other setup is needed; the hooks start working immediately.

Add this line to your project's `.gitignore`, because the memory is local to your machine:

```
.claude/session-memory/
```

## Commands

- `/remember <decision>` saves one line, "decision because reason", to project memory. Use it for the "why" behind choices.
- `/recall <word>` searches project memory for older details that are not in the brief.

## Configuration

Set these environment variables before starting Claude Code.

| Variable | Default | Meaning |
|---|---|---|
| `SESSION_MEMORY_IDLE_MIN` | 55 | Minutes of idle time after which the guard assumes the cache has expired. |
| `SESSION_MEMORY_MIN_TOKENS` | 30000 | The guard only fires when the chat is at least this many tokens. |

## Where data is stored

Everything is under `<project>/.claude/session-memory/`:

- `graph.json` - the memory graph
- `state.json` - how far each transcript has been read
- `error.log` - any internal errors (the plugin never blocks your work if it fails)

Nothing leaves your machine. Delete the folder to reset the memory.

## Benchmark

`python bench/measure.py <transcripts_dir>` compares, for each transcript, the context size Claude Code recorded (what a resume would re-send) with the estimated size of the brief (characters / 4). Run on the author's local transcripts for one project (`~/.claude/projects/C--Users-admin-Desktop-RepoQuest`):

| session | history tokens | brief tokens | saved % |
|---|---:|---:|---:|
| af0934d1-91c1-4909-87f6-f027c2fb3721.jsonl | 188500 | 203 | 99.9 |

This is a single session, so treat it as one data point, not an average. The brief carries a summary, not the full conversation, so it is smaller than the history by design. What it loses is the detail, which is why `/recall` and `/remember` exist.

## Results to fill in

The live test needs an interactive Claude Code session and spends usage limit, so it has not been run yet. Fill this in after trying it.

**Live-test checklist.** Install with `claude --plugin-dir <path to this repo>` inside a real project, then check:

- [ ] After one reply, `.claude/session-memory/graph.json` exists.
- [ ] After `/clear`, the new session's first answer knows the last request and the next steps.
- [ ] With `SESSION_MEMORY_IDLE_MIN=0` and `SESSION_MEMORY_MIN_TOKENS=1000` set, sending a message is blocked by the guard; sending the same message again goes through.

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
- **Nothing is zero tokens.** Building the graph costs none, but the brief itself is read by the model every session. It is capped under 3,000 tokens.
- The cache guard uses an idle-time estimate, not a real view of Anthropic's cache, so it can be wrong in either direction.

## Credits

No code was copied from other projects. These were studied for ideas: the official MCP Memory server (modelcontextprotocol/servers), claude-mem (docs.claude-mem.ai), Cognee's Claude Code plugin (docs.cognee.ai), CodeLoupe (github.com/Terrio-cz/CodeLoupe) and Serena. Rule for this project: code from MIT, Apache or BSD projects may be reused only with its license notice; GPL or AGPL projects are used for ideas only.

## License

MIT. See `LICENSE`.
