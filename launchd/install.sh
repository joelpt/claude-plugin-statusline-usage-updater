#!/usr/bin/env bash
# Install (or reinstall) the daily launchd job for the statusline-usage-updater.
#
# Idempotent: safe to re-run; unloads any prior copy of the agent before
# loading the new one. Renders the plist template into ~/Library/LaunchAgents/
# with the real PYTHON3, plugin root, $HOME, and state dir paths.
#
# Usage:
#   ./install.sh [--uninstall]
set -euo pipefail

LABEL="com.joelpt.statusline-usage-updater"
PLIST_TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="${PLUGIN_ROOT}/launchd/${LABEL}.plist.template"
STATE_DIR="$HOME/.claude/statusline-usage-updater"
PYTHON3="$(command -v python3 || true)"

if [[ "${1:-}" == "--uninstall" ]]; then
    if [[ -f "$PLIST_TARGET" ]]; then
        launchctl unload "$PLIST_TARGET" 2>/dev/null || true
        rm -f "$PLIST_TARGET"
        echo "uninstalled: $PLIST_TARGET"
    else
        echo "nothing to uninstall (no $PLIST_TARGET)"
    fi
    exit 0
fi

if [[ -z "$PYTHON3" ]]; then
    echo "error: python3 not found in PATH" >&2
    exit 2
fi
if [[ ! -f "$TEMPLATE" ]]; then
    echo "error: template missing at $TEMPLATE" >&2
    exit 2
fi

mkdir -p "$STATE_DIR/log"
mkdir -p "$(dirname "$PLIST_TARGET")"

# Token substitution via sed. The template uses @@PLACEHOLDERS@@ to avoid
# colliding with any plist content.
sed \
    -e "s|@@PYTHON3@@|${PYTHON3}|g" \
    -e "s|@@PLUGIN_ROOT@@|${PLUGIN_ROOT}|g" \
    -e "s|@@STATE_DIR@@|${STATE_DIR}|g" \
    -e "s|@@HOME@@|${HOME}|g" \
    "$TEMPLATE" > "$PLIST_TARGET"

# Reload: unload any existing copy, then load.
launchctl unload "$PLIST_TARGET" 2>/dev/null || true
launchctl load "$PLIST_TARGET"

echo "installed: $PLIST_TARGET"
echo "next run: tomorrow at 03:17 local time"
echo
echo "To trigger a run now (useful for testing):"
echo "  launchctl start ${LABEL}"
echo
echo "To watch logs:"
echo "  tail -f ${STATE_DIR}/log/launchd.stderr.log"
