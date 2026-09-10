#!/bin/sh
# Install the llmhub gateway LaunchAgent for the current user.
#
#   launchd/install.sh              install/replace com.llmhub.gateway
#   launchd/install.sh --uninstall  boot out the agent and remove its plist
#
# The plist in this directory is a template: __REPO__ and __HOME__ are filled
# in here, and the result goes to ~/Library/LaunchAgents. API keys are NOT in
# the plist - the gateway sources them itself from ~/.llmhub/env/*.env. Set
# LLMHUB_ENV_DIR before running this script to point the agent at a different
# directory; it gets written into the plist's EnvironmentVariables.
#
# The job execs .venv/bin/python3 itself rather than `uv run`, so dependencies
# are synced here, at install time, not on every start.
set -e

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
agents="$HOME/Library/LaunchAgents"
logs="$HOME/Library/Logs/llmhub"
domain="gui/$(id -u)"
label="com.llmhub.gateway"

uninstall=0

while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) uninstall=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ "$uninstall" = 1 ]; then
    plist="$agents/$label.plist"
    if [ -f "$plist" ]; then
        launchctl bootout "$domain/$label" 2>/dev/null || true
        rm -f "$plist"
        echo "removed $label"
    fi
    exit 0
fi

mkdir -p "$agents" "$logs"

(cd "$repo" && uv sync)
python="$repo/.venv/bin/python3"
[ -x "$python" ] || { echo "no interpreter at $python - run 'uv sync' in $repo" >&2; exit 1; }

plist="$agents/$label.plist"
# bootout first so a re-run picks up a changed template.
launchctl bootout "$domain/$label" 2>/dev/null || true
sed -e "s|__REPO__|$repo|g" \
    -e "s|__HOME__|$HOME|g" \
    "$here/$label.plist.template" > "$plist"
if [ -n "${LLMHUB_ENV_DIR:-}" ]; then
    plutil -insert EnvironmentVariables.LLMHUB_ENV_DIR -string "$LLMHUB_ENV_DIR" "$plist"
fi
plutil -lint -s "$plist"
# the old job takes a moment to leave the domain, and bootstrap fails with EIO until it has
i=0
until launchctl bootstrap "$domain" "$plist" 2>/dev/null; do
    i=$((i + 1))
    [ "$i" -lt 10 ] || { launchctl bootstrap "$domain" "$plist"; exit 1; }
    sleep 1
done
echo "installed $label -> $plist"
echo "logs in $logs"
