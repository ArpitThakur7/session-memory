#!/usr/bin/env bash
# Install the session-memory Claude Code plugin from this folder (macOS, Linux, Git Bash).
# Usage: ./install.sh [--yes] [--no-pillow] [--dry-run] [--uninstall]
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v cygpath >/dev/null 2>&1; then ROOT="$(cygpath -m "$ROOT")"; fi  # Git Bash: C:/... for Windows programs
PLUGIN="session-memory@session-memory"
MARKET="session-memory"
YES=0; NO_PILLOW=0; DRY=0; UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    --no-pillow) NO_PILLOW=1 ;;
    -n|--dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) echo "Usage: ./install.sh [--yes] [--no-pillow] [--dry-run] [--uninstall]"; exit 0 ;;
    *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
[ "$DRY" = 1 ] && say "Dry run: nothing will be changed."

# Run a claude command; output containing any of the given words also counts as success.
claude_step() {
  local ok_words="$1"; shift
  if [ "$DRY" = 1 ]; then say "Would run: claude $*"; return 0; fi
  say "Running: claude $*"
  local out rc
  out="$(claude "$@" 2>&1)"; rc=$?
  [ -n "$out" ] && say "$out"
  if [ $rc -eq 0 ]; then return 0; fi
  if printf '%s' "$out" | grep -qiE "$ok_words"; then say "Already done."; return 0; fi
  say "This step failed (exit $rc)."
  return 1
}

if [ "$UNINSTALL" = 1 ]; then
  step "Uninstalling the plugin"
  if ! command -v claude >/dev/null 2>&1; then
    say "Claude Code CLI (claude) not found. Run these later:"
    say "  claude plugin uninstall $PLUGIN"
    say "  claude plugin marketplace remove $MARKET"
    exit 1
  fi
  claude_step "not installed|not found|no plugin" plugin uninstall "$PLUGIN" || exit 1
  claude_step "not found|no marketplace|not configured" plugin marketplace remove "$MARKET" || exit 1
  say ""
  if [ "$DRY" = 1 ]; then say "Dry run finished."; else say "Done."; fi
  say "Memory folders in your projects (.claude/session-memory) were left alone; delete them by hand if you want."
  say "If you added the MCP server, remove it with: claude mcp remove session-memory -s user"
  exit 0
fi

step "Finding Python 3.10 or newer"
PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  say "No Python 3.10 or newer found. Install it from https://www.python.org and run this again."
  exit 1
fi
say "Using $PY: $("$PY" -c 'import sys; print(sys.version.split()[0], sys.executable)')"

step "Pillow (optional, for the one-image summary)"
if [ "$NO_PILLOW" = 1 ]; then
  say "Skipped (--no-pillow)."
elif "$PY" -c 'import PIL' >/dev/null 2>&1; then
  say "Pillow is already installed."
elif [ "$DRY" = 1 ]; then
  say "Would ask, then run: $PY -m pip install --user pillow"
else
  answer="y"
  if [ "$YES" != 1 ]; then
    printf 'Install Pillow with "%s -m pip install --user pillow"? [Y/n] ' "$PY"
    read -r answer || answer="n"
  fi
  case "$answer" in
    ""|y|Y|yes|Yes) "$PY" -m pip install --user pillow || say "Pillow was not installed; everything else still works." ;;
    *) say "Skipped. Install it later with: $PY -m pip install --user pillow" ;;
  esac
fi

step "Adding the plugin to Claude Code"
if command -v claude >/dev/null 2>&1; then
  claude_step "already" plugin marketplace add "$ROOT" || exit 1
  claude_step "already" plugin install "$PLUGIN" || exit 1
else
  say "Claude Code CLI (claude) not found. After installing Claude Code, run:"
  say "  claude plugin marketplace add \"$ROOT\""
  say "  claude plugin install $PLUGIN"
fi

step "Checking the setup for $(pwd)"
if [ "$DRY" = 1 ]; then
  say "Would run: $PY -m memgraph.cli doctor (with PYTHONPATH=$ROOT)"
  exit 0
fi
PYTHONPATH="$ROOT" "$PY" -m memgraph.cli doctor
rc=$?
if [ $rc -eq 0 ]; then
  say ""
  say "Next: open Claude Code in your project and use /memory-map."
fi
exit $rc
