# GO-LIVE — bật đường earn settled-inference khi FLOP mở testnet

Runbook để chuyển agent từ simulation → **earn FLOP thật** trong ~10 phút khi FLOP công bố
endpoint. Mọi code đã sẵn + gated OFF (PR #106/#108/#110). **Chỉ còn: nạp endpoint thật +
fit schema + flip cờ.** KHÔNG tự động flip — đây là hành động CHI TIỀN, phải người duyệt.

> Cơ chế (đã xác nhận, flop.finance/airdrop): agent earn = **pro-rata tuyến tính theo
> compute mua trong settled sessions**. Nút thắt = **faucet**. Luật: mua từ **miner độc lập**,
> **1 identity** (ví cùng chủ = 1 score), **manufactured activity → forfeit**.

---

## Bước 0 — khi task "Canh FLOP" báo 🚨
Nó sẽ nêu CÁI GÌ mở + tóm tắt schema API mới. Thu thập từ trang FLOP:
- [ ] `FLOP_FAUCET_URL` (endpoint xin test token)
- [ ] `FLOP_MEMPOOL_URL` (đăng session request)
- [ ] `FLOP_RPC_URL` và/hoặc `FLOP_SUBMIT_URL` (gửi tx settle)
- [ ] `FLOP_SESSION_MODEL_HASH` — hash model THẬT miner phục vụ (lấy >=2 để xoay)
- [ ] Fee/giá tham khảo (`FLOP_INFERENCE_COST`), trần faucet, min-activity floor

## Bước 1 — FIT SCHEMA (việc code duy nhất; ~5 phút)
So response/request thật của FLOP với 2 adapter scaffold, chỉnh TÊN TRƯỜNG nếu khác:
- `agent_cron.py::_mempool_submit_fn` — body gửi + đọc `{session_id, miner_did, poui}`.
- `agent_cron.py::_faucet_claim_fn` — body gửi (payload đã ký) + đọc `{amount}`.
- Nếu PoUI có cấu trúc thật (activation commitments + co-signed transcript) →
  siết `flop_session.py::verify_poui` (hiện chỉ kiểm liên kết).
Chạy test: `python -m pytest test_flop_session.py test_flop_faucet.py -q`

## Bước 2 — NẠP endpoint + tham số (repo Variables)
```bash
R=thanhphuc85/technocore-crypto-agent
gh variable set FLOP_FAUCET_URL   --repo $R --body "<faucet_url>"
gh variable set FLOP_MEMPOOL_URL  --repo $R --body "<mempool_url>"
gh variable set FLOP_RPC_URL      --repo $R --body "<rpc_url>"
gh variable set FLOP_SUBMIT_URL   --repo $R --body "<submit_url>"      # nếu dùng relay
gh variable set FLOP_SESSION_MODEL_HASH --repo $R --body "<hash1>,<hash2>"  # CSV để xoay
gh variable set FLOP_INFERENCE_COST     --repo $R --body "<fee_nền>"
# submit_tx mode (relay là mặc định; đổi nếu FLOP dùng chain EVM)
gh variable set FLOP_TX_MODE      --repo $R --body "relay"
```
Secret (nếu endpoint cần auth riêng ngoài chữ ký Ed25519 đã có): `gh secret set ...`.

## Bước 3 — THỬ KHÔ trước khi chi thật
```bash
# Vẫn simulation (TESTNET_ENABLED chưa bật): kiểm wiring không lỗi
gh variable set FLOP_SESSION_ENABLED --repo $R --body "on"
gh variable set FLOP_FAUCET_ENABLED  --repo $R --body "on"
# Xem run kế: faucet=<outcome> session=<n✓> ; đảm bảo KHÔNG error/skipped_unconfigured
```
Nếu faucet=claimed và session chạy (dù testnet chưa bật, sim vẫn ok) → wiring đúng.

## Bước 4 — BẬT CHI THẬT (human-verified)
```bash
gh variable set TESTNET_ENABLED --repo $R --body "1"
```
Kiểm run đầu: `[faucet] claimed ...` + `[session] N✓ settle/spent_onchain` + có tx_hash.
**Xác minh**: tx thật lên đúng endpoint FLOP; spend trừ số dư; unlock_status tăng 3:1.

## Bước 5 — TỐI ƯU throughput (trong giới hạn sạch)
- Chi HẾT faucet đều đặn: `FLOP_SESSION_MAX_PER_RUN` khớp tốc độ faucet refill (tăng dần,
  quan sát 429/risk). Nhịp 5-phút Actions thường đủ; chỉ cần VPS (deploy/vps/) nếu faucet
  refill nhanh hơn 1 batch chi hết.
- Giữ đa dạng: `FLOP_SESSION_MODEL_HASH` nhiều hash + `FLOP_SESSION_JITTER_PCT=20`.
- Nới van spend nếu quá chặt: `FLOP_MAX_SPENDS_PER_HOUR`, `FLOP_ORGANIC_ONLY`.
- Faucet organic: `FLOP_FAUCET_DEMAND_ONLY=1` + `FLOP_FAUCET_REFILL_BELOW=<ngưỡng>`.

## Bước 6 — PHÒNG THỦ anti-sybil (bắt buộc)
- [ ] Mua từ **miner độc lập** — KHÔNG tự chạy miner rồi tự mua.
- [ ] **1 identity** — không chia spend qua ví mình kiểm soát (không được tính + sybil-link).
- [ ] Spend **trải đều 90 ngày**, không dump phút chót (snapshot lúc đóng).
- [ ] Giữ `FLOP_PUBLISH_UNLOCK=1` → dấu vết audit công khai, defensible.
- [ ] (Mạnh nhất) route inference THẬT của agent (digest/reply/kibble) qua FLOP → spend có
      output thật chống lưng.

## Rollback (nếu sai)
```bash
gh variable set TESTNET_ENABLED --repo $R --body "0"   # về simulation ngay, ngừng chi thật
```
Hoặc tắt từng cái: `FLOP_SESSION_ENABLED=off` / `FLOP_FAUCET_ENABLED=off`.

---
**Tóm tắt flip:** fit 2 adapter → nạp URL/model → dry (SESSION/FAUCET on, TESTNET off) →
`TESTNET_ENABLED=1` → kiểm `spent_onchain` + tx thật → tối ưu MAX_PER_RUN theo faucet.
