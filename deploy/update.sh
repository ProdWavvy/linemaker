#!/usr/bin/env bash
# After copying a new linemaker.zip to the server's home folder:
#   bash linemaker/deploy/update.sh
# Unzips over the old code, keeping .env, tracking/, cache/ and snapshots/, then restarts.
set -euo pipefail
cd "$(dirname "$0")/../.."
ZIP="${1:-linemaker.zip}"
[ -f "$ZIP" ] || { echo "Put linemaker.zip in $(pwd) first."; exit 1; }
command -v unzip >/dev/null || sudo apt-get install -y unzip 2>/dev/null || sudo dnf install -y unzip
cp linemaker/.env /tmp/linemaker.env.bak
unzip -oq "$ZIP" -x 'linemaker/.env'
cp /tmp/linemaker.env.bak linemaker/.env
linemaker/.venv/bin/pip install -q -r linemaker/requirements.txt
sudo systemctl restart linemaker
sleep 5
sudo systemctl --no-pager status linemaker | head -8
