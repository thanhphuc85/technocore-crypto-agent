#!/usr/bin/env bash
# run-agent.sh — chạy MỘT vòng agent trên VM (Oracle Always Free), runner CHÍNH.
# Được systemd timer (hoặc cron */30) gọi mỗi 30'. Tự `git pull` để cập nhật code, `flock`
# chống chạy chồng, nạp secrets từ file env (KHÔNG commit). Log ra stdout -> journald bắt.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/technocore-crypto-agent}"
ENV_FILE="${ENV_FILE:-$HOME/technocore-agent.env}"
LOCK="${LOCK:-/tmp/technocore-agent.lock}"

# flock: nếu vòng trước còn chạy thì BỎ QUA vòng này (không xếp hàng chồng).
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[run] vòng trước còn chạy -> bỏ qua"
  exit 0
fi

cd "$REPO_DIR"

# Tự cập nhật code nhánh main. Lỗi mạng/không fast-forward -> vẫn chạy code hiện có.
git pull --quiet --ff-only origin main || echo "[run] git pull bỏ qua (dùng code hiện có)"

# Nạp secrets + biến môi trường (set -a: export mọi biến khai báo trong file).
if [[ -f "$ENV_FILE" ]]; then
  set -a; . "$ENV_FILE"; set +a
fi
export RUNNER_ROLE="${RUNNER_ROLE:-primary}"   # VM là runner CHÍNH

# venv đã tạo lúc setup (oracle-vm-setup.sh).
# shellcheck disable=SC1091
. "$REPO_DIR/.venv/bin/activate"
python agent_cron.py
