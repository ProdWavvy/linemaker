#!/usr/bin/env bash
# One-time setup on an Ubuntu/Debian or Oracle Linux server (it can share the machine with
# other bots: own folder, own .venv, own "linemaker" service). Run it with
#   bash deploy/setup_server.sh
# It installs Python, builds a virtual environment, and registers the bot as a service that
# starts on boot and restarts itself if it ever crashes.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"
USER_NAME="$(whoami)"

if [ ! -f .env ]; then
  echo "No .env in $DIR. Copy your .env here first (it holds your keys), then run this again."
  exit 1
fi

# Python 3.10+ (Ubuntu/Debian use apt; Oracle Linux / RHEL use dnf, where the stock python3
# can be too old, so a newer one is installed alongside it).
if command -v apt-get >/dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-venv python3-pip unzip git
else
  sudo dnf install -y python3.12 python3.12-pip unzip git 2>/dev/null \
    || sudo dnf install -y python3.11 python3.11-pip unzip git
fi
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || { echo "Couldn't find Python 3.10 or newer."; exit 1; }
echo "Using $($PY --version)"

"$PY" -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

sudo tee /etc/systemd/system/linemaker.service > /dev/null <<UNIT
[Unit]
Description=Linemaker Discord bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/run_bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

# Auto-update: when this folder is a git clone, check GitHub every 5 minutes and redeploy.
if [ -d .git ]; then
  sudo tee /etc/systemd/system/linemaker-update.service > /dev/null <<UNIT
[Unit]
Description=Pull Linemaker updates from GitHub and restart the bot
After=network-online.target

[Service]
Type=oneshot
ExecStart=$DIR/deploy/autoupdate.sh
UNIT
  sudo tee /etc/systemd/system/linemaker-update.timer > /dev/null <<UNIT
[Unit]
Description=Check GitHub for Linemaker updates every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNIT
fi

sudo systemctl daemon-reload
sudo systemctl enable --now linemaker
if [ -d .git ]; then
  sudo systemctl enable --now linemaker-update.timer
  echo "Auto-update on: GitHub is checked every 5 minutes (log: sudo journalctl -u linemaker-update)"
fi
sleep 5
sudo systemctl --no-pager status linemaker | head -15
echo
echo "Done. Live log: sudo journalctl -u linemaker -f   (Ctrl+C to stop watching)"
