#!/usr/bin/env bash
# Runs every few minutes (linemaker-update.timer). If GitHub has new commits it pulls them,
# installs any new requirements, checks the bot still imports, and restarts it. If the new
# code fails that check it rolls back, so a bad update can't take the bot down.
# .env, tracking/, cache/ and snapshots/ are git-ignored, so updates never touch them.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="$(stat -c %U "$DIR")"
as_owner() { runuser -u "$OWNER" -- "$@"; }
cd "$DIR"

BRANCH="$(as_owner git rev-parse --abbrev-ref HEAD)"
as_owner git fetch -q origin "$BRANCH"
OLD="$(as_owner git rev-parse HEAD)"
NEW="$(as_owner git rev-parse "origin/$BRANCH")"
[ "$OLD" = "$NEW" ] && exit 0
BAD="$DIR/.git/linemaker-bad-commit"
[ -f "$BAD" ] && [ "$(cat "$BAD")" = "$NEW" ] && exit 0   # already tried and rolled back

echo "linemaker: updating ${OLD:0:7} -> ${NEW:0:7}"
as_owner git reset -q --hard "$NEW"
if ! as_owner git diff --quiet "$OLD" "$NEW" -- requirements.txt; then
  as_owner .venv/bin/pip install -q -r requirements.txt
fi
if ! as_owner .venv/bin/python -c "import sys; sys.path.insert(0, '.'); import linemaker.bot" 2>/tmp/linemaker-update.err; then
  echo "linemaker: new code failed to load, rolling back:"; cat /tmp/linemaker-update.err
  as_owner git reset -q --hard "$OLD"
  echo "$NEW" > "$BAD"
  exit 1
fi
systemctl restart linemaker
echo "linemaker: now running ${NEW:0:7} ($(as_owner git log -1 --format=%s))"
