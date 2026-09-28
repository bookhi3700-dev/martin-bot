#!/usr/bin/env bash
# 마틴봇 업데이트: bash ~/martin-bot/deploy/update.sh
# 설정(config.json)과 기록(data/)은 그대로 유지됩니다.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
echo "▶ 최신 버전 받는 중"
git pull --ff-only
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
sudo systemctl restart martin-bot
sleep 2
if systemctl is-active --quiet martin-bot; then echo "✅ 업데이트 완료 — 실행 중"; git log -1 --format='   버전: %h %s (%cd)' --date=format:'%Y-%m-%d %H:%M'
else echo "❌ 실행 실패 — sudo journalctl -u martin-bot -n 50"; fi
