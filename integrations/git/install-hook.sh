#!/bin/sh
# Optional Worktracker git hook installer.
#
#   curl -fsSL https://raw.githubusercontent.com/besteFinanzen/custom-llm-auto-router/feat/worktracker/integrations/git/install-hook.sh | sh
#
# Opens a guided setup on /dev/tty: it pre-fills saved values, validates the
# tracker URL and hook token, lets you name the device, and asks for
# confirmation before writing. The OpenCode plugin already covers commits made
# inside OpenCode; this hook also credits commits made from a terminal, IDE or
# script.
#
# Flags (forwarded to worktracker_hook.py):
#   --url URL --token TOKEN   skip the corresponding prompt
#   --interactive             always open the wizard, even with saved values
#   --no-prompt               never prompt; fail if credentials are missing
#
# Environment overrides (skip the prompts when both are set):
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
  if ! curl -fsSL "$REPO_RAW/worktracker_hook.py" -o "$TMP/worktracker_hook.py"; then
    echo "could not download worktracker_hook.py; run the script from a checkout or retry" >&2
    exit 1
  fi
fi

# Accept `install-hook.sh install` as well as a bare `install-hook.sh`.
if [ "${1:-}" = "install" ]; then
  shift
fi

install_hook() {
  "$PYTHON" "$TMP/worktracker_hook.py" install "$@"
}

force_interactive=0
for arg in "$@"; do
  case "$arg" in
    --interactive) force_interactive=1 ;;
  esac
done

# A fully configured install needs no terminal at all (CI, containers, ssh).
# Otherwise hand the wizard the controlling terminal: `curl | sh` leaves
# stdin as a pipe, so /dev/tty is the only interactive stream.
if [ "$force_interactive" -eq 0 ] && [ -n "${WORKTRACKER_URL:-}" ] \
    && [ -n "${WORKTRACKER_TOKEN:-}" ]; then
  install_hook "$@"
elif (exec 3<>/dev/tty) 2>/dev/null; then
  # /dev/tty can exist yet be unopenable when there is no controlling terminal.
  install_hook "$@" </dev/tty
else
  # No terminal at all: let the reporter explain how to pass credentials.
  install_hook "$@"
fi
