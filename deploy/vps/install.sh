#!/usr/bin/env bash
# Trình cài đặt ĐÃ RÀ SOÁT cho VPS (Ubuntu/Debian/Oracle Linux). Chạy bằng root:
#   sudo ./install.sh [REPO_URL]
#
# Nó KHÔNG tự bật/chạy service, KHÔNG hỏi/đụng tới seed. Sau khi chạy, operator tự điền
# /etc/technocore-agent/env rồi mới enable. Idempotent: chạy lại = cập nhật mã + deps.
set -euo pipefail

REPO_URL="${1:-https://github.com/thanhphuc85/technocore-crypto-agent.git}"
APP_DIR=/opt/technocore-agent
ETC_DIR=/etc/technocore-agent
STATE_DIR=/var/lib/technocore-agent
SVC_USER=technocore

[ "$(id -u)" -eq 0 ] || { echo "Phải chạy bằng root (sudo)."; exit 1; }

echo "[1/6] user hệ thống không-login: $SVC_USER"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"

echo "[2/6] mã nguồn -> $APP_DIR (read-only cho service)"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --depth 1 origin HEAD && git -C "$APP_DIR" reset --hard FETCH_HEAD
else
  mkdir -p "$APP_DIR"
  git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi

echo "[3/6] virtualenv + deps (cryptography, requests)"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet "$APP_DIR"      # cài theo pyproject (deps nhẹ)

echo "[4/6] thư mục state + env (0600)"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$STATE_DIR"
install -d -m 0755 "$ETC_DIR"
if [ ! -f "$ETC_DIR/env" ]; then
  install -m 0600 "$APP_DIR/deploy/vps/env.example" "$ETC_DIR/env"
  echo "    -> tạo $ETC_DIR/env RỖNG. ĐIỀN secrets trước khi enable."
else
  echo "    -> giữ $ETC_DIR/env hiện có (không ghi đè)."
fi

echo "[5/6] cài unit systemd"
install -m 0644 "$APP_DIR/deploy/vps/technocore-agent.service" /etc/systemd/system/
install -m 0644 "$APP_DIR/deploy/vps/technocore-agent.timer"   /etc/systemd/system/
systemctl daemon-reload

echo "[6/6] XONG — CHƯA bật gì (có chủ đích). Bước tiếp theo của bạn:"
cat <<EOF

  1) sudoedit $ETC_DIR/env        # điền AGENT_PRIVATE_KEY + LLM keys + van FLOP
  2) sudo -u $SVC_USER $APP_DIR/.venv/bin/python $APP_DIR/agent_cron.py   # chạy thử 1 lần
  3) systemctl enable --now technocore-agent.timer   # bật chạy mỗi 5 phút
  4) systemctl list-timers technocore-agent.timer    # kiểm lịch
     journalctl -u technocore-agent.service -f       # xem log

  LƯU Ý double-post: nếu GitHub Actions VẪN chạy, đặt RUNNER_ROLE=backup trong env
  (VPS làm hot-standby), hoặc tắt workflow Actions để VPS làm runner chính.
  Cập nhật sau: sudo $APP_DIR/deploy/vps/install.sh   (rồi systemctl restart nếu cần)
EOF
