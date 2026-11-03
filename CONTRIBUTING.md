# Contributing to session-memory

Thanks for helping. This is a small, plain-Python project with no runtime dependencies (Pillow is optional, for the picture packs).

## Setup

You need Python 3.10 or newer and git.

```
git clone https://github.com/ArpitThakur7/session-memory
cd session-memory
python -m pip install pytest pillow   # pillow is optional
```

To try your changes in Claude Code, start it with the plugin loaded from your checkout:

```
claude --plugin-dir <path to your checkout>
```

## Tests

```
python -m pytest -q -W error::SyntaxWarning
```

CI runs this on Linux, macOS and Windows with Python 3.10 to 3.13, plus one run without Pillow. Tests that only make sense on one OS use `pytest.mark.skipif`; tests that need Pillow skip themselves when it is missing.

## How we work

- **Test first.** Write a failing test for the behaviour or bug, see it fail, then make it pass. Keep tests small and fast: no network, no real notifications, no real background processes (`tests/conftest.py` blocks the watcher).
- **No real transcripts in tests, issues or pull requests.** Claude Code transcripts hold your prompts, code, paths and sometimes secrets. Write small fake JSONL lines by hand (see `tests/fixtures/` and `tests/helpers.py`). Use placeholder names such as `demo-project`, `C:\Users\me\proj` or `/home/me/proj`. Build fake secrets in tests from pieces (see `tests/test_redact.py`) so no scanner mistakes them for real ones.
- **Keep it local.** The plugin must never send data over the network.
- **Keep the brief small.** Anything added to the startup brief costs tokens in every session.

## Commit style

A short imperative subject with a type prefix, as in the existing history:

```
feat: resume pack shows unlinked decisions
fix: watcher keeps waiting on a moved deadline
docs: README install via marketplace
test: make pack path test cross-platform
ci: add Python 3.13
chore: tidy fixtures
```

Add a body when the reason is not obvious from the subject. One logical change per commit.

## Pull requests

Open an issue first for larger changes. Fill in the pull request template, make sure the full test suite passes, and update `README.md` and `CHANGELOG.md` when behaviour changes.
