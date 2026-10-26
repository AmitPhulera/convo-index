#!/bin/sh
# convo-index installer.
#
#   curl -fsSL https://raw.githubusercontent.com/AmitPhulera/convo-index/main/install.sh | sh
#
# Installs uv if it is missing, installs the convo-index CLI as a uv tool straight from
# GitHub (no clone), then runs `convo-index up`: writes the config, keeps the UI server
# running from login, schedules the daily refresh and builds the index. Everything stays on
# this machine until you allow hosted LLM processing (`convo-index init` on a terminal).
# Re-running upgrades to the latest commit.
#
# Overrides: CONVO_INDEX_REPO=owner/name  CONVO_INDEX_REF=branch-or-tag
#            CONVO_INDEX_SOURCE=/path/to/checkout (install from a local tree instead)
#            CONVO_INDEX_NO_UP=1 (install the command only)
set -eu

REPO="${CONVO_INDEX_REPO:-AmitPhulera/convo-index}"
REF="${CONVO_INDEX_REF:-main}"
SOURCE="${CONVO_INDEX_SOURCE:-git+https://github.com/$REPO.git@$REF}"

say() { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

have curl || { say "error: curl is required"; exit 1; }
case "$SOURCE" in git+*) have git || { say "error: git is required to install from GitHub"; exit 1; } ;; esac

if ! have uv; then
    say "uv not found; installing it from https://astral.sh/uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"; export PATH
    have uv || { say "error: uv was installed but is not on PATH yet; open a new shell and re-run this script"; exit 1; }
fi

say "installing convo-index from $SOURCE ..."
uv tool install --reinstall --quiet "$SOURCE"

BIN_DIR="$(uv tool dir --bin)"
say ""
say "installed: $BIN_DIR/convo-index"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say "note: $BIN_DIR is not on your PATH; run \`uv tool update-shell\` (then open a new shell)" ;;
esac
say ""
if [ -n "${CONVO_INDEX_NO_UP:-}" ]; then
    say "next: convo-index up    # config, UI server from login, daily refresh, index build"
    exit 0
fi
say "first run: config, UI server from login, daily refresh, index build in the background ..."
say ""
exec "$BIN_DIR/convo-index" up
