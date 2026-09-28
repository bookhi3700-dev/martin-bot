#!/usr/bin/env bash
# 마틴봇 업데이트: bash ~/martin-bot/deploy/update.sh
# 설정(config.json)과 기록(data/)은 그대로 유지됩니다.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
echo "> Downloading latest version"
git pull --ff-only
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
sudo systemctl restart martin-bot
sleep 2
if systemctl is-active --quiet martin-bot; then echo "UPDATE OK - running"; git log -1 --format='   version: %h (%cd)' --date=format:'%Y-%m-%d %H:%M'
else echo "FAILED - check: sudo journalctl -u martin-bot -n 50"; fi
