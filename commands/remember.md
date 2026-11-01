---
description: Save a key decision and its reason to project memory
argument-hint: [decision]
allowed-tools: Bash(*run-python*)
---
Rewrite "$ARGUMENTS" (if empty, the most important decision made since the last /remember) as one line under 160 characters: `<decision> because <reason>`.
Run once: `"${CLAUDE_PLUGIN_ROOT}/bin/run-python" remember '<line>'`, with the line in single quotes (write any `'` inside it as `'\''`).
Reply with only the saved line.
