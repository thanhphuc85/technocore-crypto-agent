# Triển khai VPS (systemd) — tùy chọn, song song với GitHub Actions

Bộ này cho phép chạy agent trên **VPS/server** bằng **systemd timer** thay cho (hoặc song song với)
GitHub Actions. Vì `agent_cron.py` là **single-pass** (chạy 1 vòng rồi thoát), mô hình đúng là
**timer gọi định kỳ**, KHÔNG phải daemon — hành vi y hệt Actions.

## Khi nào nên dùng
- **Chưa cần bây giờ.** Actions mỗi 5 phút đang đủ; chưa có gì "earn" (testnet FLOP chưa mở).
- **Đáng dùng khi testnet mở**, nếu muốn **tăng throughput settled-inference** (earn = tuyến tính
  pro-rata theo spend) dày hơn nhịp 5 phút của Actions — trong **giới hạn faucet + anti-sybil**.
- Hoặc làm **hot-standby** (uptime cao hơn) bên cạnh Actions: đặt `RUNNER_ROLE=backup`.

## Thành phần
| File | Vai trò |
|---|---|
| `technocore-agent.service` | oneshot: chạy `agent_cron.py` 1 lần (sandbox, state ở `/var/lib`) |
| `technocore-agent.timer` | gọi service mỗi 5 phút (chỉnh `OnUnitActiveSec` để đổi nhịp) |
| `env.example` | mẫu `/etc/technocore-agent/env` (secrets, 0600) |
| `install.sh` | cài đã-rà-soát: KHÔNG tự bật, KHÔNG đụng seed |

## Cài nhanh
```bash
sudo ./install.sh                      # clone -> venv -> cài unit (chưa bật)
sudoedit /etc/technocore-agent/env     # điền AGENT_PRIVATE_KEY + LLM keys + van FLOP
sudo -u technocore /opt/technocore-agent/.venv/bin/python /opt/technocore-agent/agent_cron.py  # thử 1 lần
sudo systemctl enable --now technocore-agent.timer
journalctl -u technocore-agent.service -f
```

## ⚠️ Tránh "double-post" (chạy cùng Actions)
Agent có cơ chế phối hợp 2 runner qua heartbeat trên KV:
- **VPS làm phụ**: `RUNNER_ROLE=backup` trong env → VPS chỉ chạy đầy đủ khi heartbeat của runner
  CHÍNH (Actions) đã CŨ. An toàn để bật song song.
- **VPS làm chính**: `RUNNER_ROLE=primary` **và** tắt workflow Actions (hoặc để Actions=backup),
  nếu không cả hai cùng đăng → trùng.

## Nhịp chạy
Mặc định 5 phút (khớp Actions). Đổi trong `technocore-agent.timer`:
```
OnUnitActiveSec=5min      # testnet high-throughput: có thể giảm (vd 60s) — ĐỪNG quá dày = bot
```
rồi `systemctl daemon-reload && systemctl restart technocore-agent.timer`.

## Bảo mật seed (quan trọng)
`AGENT_PRIVATE_KEY` = **danh tính + uy tín**, lộ là mất cả hai (không rotate được). Khuyến nghị:
- Để trong **secret manager** (OCI Vault / 1Password / HashiCorp) và chỉ nạp vào `/etc/technocore-agent/env`
  (root:root, 0600) lúc provision — không để trong shell history / git / log.
- Service chạy bằng user **không-login** `technocore`, sandbox systemd, chặn IMDS (`169.254.169.254`).

## Hạ tầng gợi ý
Tải nhẹ (Python + 2 dep, vài giây/vòng). Hetzner CX22 (EU) hoặc Oracle Cloud Free (ARM) đều dư.
Tránh VPS đặt ở Mỹ (một số API như Binance Futures chặn IP Mỹ).

## Gỡ / quay lại Actions
```bash
sudo systemctl disable --now technocore-agent.timer
```
Actions vẫn chạy độc lập; gỡ timer là xong (state trên KV vẫn liền mạch).
