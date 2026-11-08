# Live test checklist

The live test needs an interactive Claude Code session and spends usage limit, so it has not been run yet. Fill this in after trying it.

**Live-test checklist.** Install with `claude --plugin-dir <path to this repo>` inside a real project, then check:

- [ ] After one reply, `.claude/session-memory/graph.json` exists.
- [ ] After `/clear`, the new session's first answer knows the last request and the next steps.
- [ ] With `SESSION_MEMORY_IDLE_MIN=0` and `SESSION_MEMORY_MIN_TOKENS=1000` set, sending a message is blocked by the guard; sending the same message again goes through.
- [ ] With `SESSION_MEMORY_CACHE_TTL_MIN=4` set, a desktop notification appears about 2 minutes after a reply.
- [ ] After `/clear`, the brief contains the "Full resume pack" line (and the "Visual summary" line when the picture is current), and `resume-pack.md` contains your latest request (the "Built" time stays the same when nothing changed).
- [ ] After a reply, close Claude Code: the cache watcher still shows its notification.
- [ ] In VS Code and in Antigravity, the session-memory server lists 10 tools: `memory_brief`, `memory_recall`, `memory_explain`, `memory_path`, `memory_remember`, `memory_pack`, `memory_usage`, `code_outline`, `code_find`, `code_read`.

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
