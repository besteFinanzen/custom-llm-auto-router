#!/bin/sh
# Optional Worktracker git hook installer.
#
#   curl -fsSL https://raw.githubusercontent.com/besteFinanzen/custom-llm-auto-router/feat/worktracker/integrations/git/install-hook.sh | sh
#
# Prompts on /dev/tty for the tracker URL and hook token, validates them, and
# installs a post-commit hook in the current repository. The OpenCode plugin
# already covers commits made inside OpenCode; this hook also credits commits
# made from a terminal, IDE or script.
#
# Environment overrides (skip the prompt):
#   WORKTRACKER_URL=https://ai.example.com WORKTRACKER_TOKEN=wt_hook_... sh install-hook.sh
set -eu

REPO_RAW="${WORKTRACKER_RAW:-https://raw.githubusercontent.com/besteFinanzen/custom-llm-auto-router/feat/worktracker/integrations/git}"
PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  echo "python3 is required for the worktracker hook" >&2
  exit 1
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "run this inside a git repository" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ -f "$(dirname "$0")/worktracker_hook.py" ]; then
  # running from a checkout
  cp "$(dirname "$0")/worktracker_hook.py" "$TMP/worktracker_hook.py"
else
  curl -fsSL "$REPO_RAW/worktracker_hook.py" -o "$TMP/worktracker_hook.py"
fi

# Run with the prompt attached to the terminal: curl|sh leaves stdin=pipe.
# A fully configured install needs no terminal at all (CI, containers, ssh).
if [ -t 0 ]; then
  "$PYTHON" "$TMP/worktracker_hook.py" install "$@"
elif [ -n "${WORKTRACKER_URL:-}" ] && [ -n "${WORKTRACKER_TOKEN:-}" ]; then
  "$PYTHON" "$TMP/worktracker_hook.py" install "$@"
elif [ -e /dev/tty ]; then
  "$PYTHON" "$TMP/worktracker_hook.py" install "$@" </dev/tty
else
  # No terminal at all: let the reporter explain how to pass credentials.
  "$PYTHON" "$TMP/worktracker_hook.py" install "$@"
fi
