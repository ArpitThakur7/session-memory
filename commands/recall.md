---
description: Search project memory
argument-hint: [word] [--budget N]
allowed-tools: Bash(*run-python*)
---
Search word: $ARGUMENTS
Run `"${CLAUDE_PLUGIN_ROOT}/bin/run-python" recall '<word>'`, with the word in single quotes (write any `'` inside it as `'\''`), and use the result. Do not open the listed files unless the task needs them.
