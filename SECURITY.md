# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub's security advisories: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue for a security problem, and do not attach real transcripts or memory files; a small made-up example is enough.

Fixes are released as a new version and noted in `CHANGELOG.md`. Only the latest release gets security fixes.

## What the plugin reads and writes

Everything happens on your machine. The plugin makes no network requests and sends nothing anywhere.

**Reads**

- Claude Code's local transcript files for the current project (`~/.claude/projects/<project folder>/*.jsonl`, including helper-agent transcripts), and the hook input Claude Code passes on stdin.

**Writes** (all inside `<your project>/.claude/session-memory/`, which contains a `.gitignore` with `*` so nothing there is committed)

- `graph.json` (the memory graph: short versions of your prompts, commands, errors, file paths, replies, todos and titles), `state.json`, `graph.lock`, `guard.json`, `events.jsonl`, `watch.json`, `watch.lock`, `resume-pack.md`, `memory.png`, `memory-map.html` and `error.log`.

**Runs**

- One small background process per project (the cache watcher), which shows a desktop notification through the operating system's own notifier.
- An MCP server over stdio, only when you add it to a tool yourself.

## Secret redaction

Before any text is stored, it passes through `memgraph/redact.py`, which replaces common secrets with `[hidden]`: Anthropic, OpenAI, GitHub, AWS, Slack, Google and Stripe keys, JWTs, private-key blocks, `Bearer` and `Authorization` values, passwords in URLs, `--password`/`--token`/`--api-key` values, and `NAME=value` settings whose name contains KEY, TOKEN, SECRET, PASSWORD, PASSWD or CREDENTIAL.

This is a best-effort filter. It cannot recognise every secret, so treat the memory folder as private, like the transcripts it is built from. Memory built by version 0.1.0 is not rewritten: delete `graph.json` and `state.json` to rebuild it with redaction.

A pattern that misses a common secret format, or hides normal text, is a welcome bug report (use a fake value).
