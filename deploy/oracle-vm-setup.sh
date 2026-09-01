#!/usr/bin/env bash
# oracle-vm-setup.sh — cài agent trên VM Oracle Always Free (Ubuntu/Debian). Chạy 1 LẦN
# sau khi SSH vào VM. An toàn khi chạy lại (idempotent). Sau đó chỉ cần điền file env.
#
#   curl -fsSL https://raw.githubusercontent.com/thanhphuc85/technocore-crypto-agent/main/deploy/oracle-vm-setup.sh | bash
#   # hoặc: git clone ... && bash technocore-crypto-agent/deploy/oracle-vm-setup.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/thanhphuc85/technocore-crypto-agent.git}"
REPO_DIR="${REPO_DIR:-$HOME/technocore-crypto-agent}"
ENV_FILE="${ENV_FILE:-$HOME/technocore-agent.env}"

echo "==> Cài Python + git"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

echo "==> Clone / cập nhật repo"
if [[ -d "$REPO_DIR/.git" ]]; then
  git -C "$REPO_DIR" pull --ff-only origin main
else
  git clone "$REPO_URL" "$REPO_DIR"
fi

echo "==> Tạo venv + cài package"
python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install --upgrade pip
"$REPO_DIR/.venv/bin/pip" install -e "$REPO_DIR"

echo "==> Chuẩn bị file env (secrets)"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$REPO_DIR/deploy/technocore-agent.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "   -> Đã tạo $ENV_FILE. HÃY MỞ RA ĐIỀN AGENT_PRIVATE_KEY + các key: nano $ENV_FILE"
else
  echo "   -> $ENV_FILE đã tồn tại, giữ nguyên."
fi

echo "==> Cài systemd user service + timer"
chmod +x "$REPO_DIR/deploy/run-agent.sh"
mkdir -p "$HOME/.config/systemd/user"
cp "$REPO_DIR/deploy/technocore-agent.service" "$HOME/.config/systemd/user/"
cp "$REPO_DIR/deploy/technocore-agent.timer"   "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now technocore-agent.timer

echo "==> Cho phép timer chạy khi bạn CHƯA đăng nhập"
sudo loginctl enable-linger "$USER"

cat <<EOF

XONG ✅
1) Điền secrets:      nano $ENV_FILE   (nhớ AGENT_PRIVATE_KEY)
2) Chạy thử 1 vòng:   systemctl --user start technocore-agent.service
3) Xem log:           journalctl --user -u technocore-agent -n 50 --no-pager
4) Xem lịch kế tiếp:  systemctl --user list-timers | grep technocore
EOF
