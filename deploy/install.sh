#!/usr/bin/env bash
# 마틴봇 서버 설치 (Ubuntu 22.04 / 24.04)
#   bash deploy/install.sh
# 여러 번 실행해도 안전합니다. (IP가 바뀐 뒤 다시 실행하면 접속 주소가 갱신됩니다)
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(whoami)"
cd "$APP_DIR"
say(){ printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }

say "1/6 서버 시간을 한국 시간으로 맞춥니다"
sudo timedatectl set-timezone Asia/Seoul || true

say "2/6 메모리 보조(스왑) 확인"
MEM_MB=$(free -m | awk '/^Mem:/{print $2}')
if [ "$(swapon --show --noheadings | wc -l)" = "0" ] && [ "$MEM_MB" -lt 1800 ]; then
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo "스왑 1GB 추가"
else
  echo "필요 없음"
fi

say "3/6 필요한 프로그램 설치 (1~3분)"
sudo apt-get update -y -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip git curl >/dev/null
if ! command -v caddy >/dev/null; then
  if ! sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy >/dev/null 2>&1; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo apt-get update -y -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy >/dev/null
  fi
fi
python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

say "4/6 대시보드 비밀번호"
if [ -f data/auth.json ]; then
  echo "이미 설정됨 (바꾸려면: .venv/bin/python set_password.py)"
else
  .venv/bin/python set_password.py < /dev/tty
fi

say "5/6 자동 실행 등록 (서버가 재부팅돼도 자동으로 켜짐)"
sudo tee /etc/systemd/system/martin-bot.service >/dev/null <<EOF
[Unit]
Description=Martin Bot
After=network-online.target
Wants=network-online.target

[Service]
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
Environment=MARTIN_NO_BROWSER=1
Environment=MARTIN_SECURE_COOKIE=1
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable martin-bot >/dev/null 2>&1
sudo systemctl restart martin-bot

say "6/6 HTTPS 접속 주소 설정"
IP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
DOMAIN="${IP//./-}.sslip.io"
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
${DOMAIN} {
    encode gzip
    reverse_proxy 127.0.0.1:8765
}
EOF
sudo systemctl enable caddy >/dev/null 2>&1
sudo systemctl restart caddy

sleep 3
if systemctl is-active --quiet martin-bot; then STATE="실행 중 ✅"; else STATE="실행 실패 ❌ (sudo journalctl -u martin-bot -n 50 으로 확인)"; fi
cat <<EOF

==================================================================
  설치 완료 — 마틴봇 ${STATE}

  대시보드 주소 :  https://${DOMAIN}
                  (처음 접속은 인증서 발급 때문에 1분쯤 걸릴 수 있어요)

  거래소 API 허용 IP에 등록할 주소 :  ${IP}

  자주 쓰는 명령어
    업데이트   : bash ~/martin-bot/deploy/update.sh
    로그 보기  : sudo journalctl -u martin-bot -f     (나가기 Ctrl+C)
    다시 시작  : sudo systemctl restart martin-bot
    비번 변경  : cd ~/martin-bot && .venv/bin/python set_password.py
==================================================================
EOF
