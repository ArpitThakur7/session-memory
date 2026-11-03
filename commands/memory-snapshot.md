---
description: Make a one-image summary of this project's chat history
allowed-tools: Bash(*run-python*)
---
Run once: `"${CLAUDE_PLUGIN_ROOT}/bin/run-python" pack`
Reply with only the lines it printed, plus one sentence: the image is about 1,590 tokens and the next chat can read it after /clear.
