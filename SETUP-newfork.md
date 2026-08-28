# Hướng dẫn dựng fork MỚI — chạy sạch, không bị coi là sybil

Tài liệu này dành cho **một người mới, độc lập** fork repo này về chạy **một agent
duy nhất trên máy/tài khoản của riêng mình**.

> **Sybil là gì (nói cho đúng):** sybil = *một thực thể giả làm nhiều danh tính*
> để nhân phần thưởng. Fork repo **không phải** sybil. Một người chạy **một** agent
> với **danh tính của chính mình** thì **theo định nghĩa không phải sybil** — không
> cần "né" gì cả, chỉ cần dựng cho đúng để không vô tình tự liên kết với instance khác.
>
> Ngược lại: cùng một người chạy nhiều bản với nhiều ví/khoá — dù trên nhiều máy,
> nhiều IP — thì **đó chính là sybil**. Máy khác không xoá được mối liên hệ (cùng
> nguồn tiền, cùng người rút, cùng pattern). Đừng làm theo hướng đó.

---

## Nguyên tắc vàng: KHÔNG dùng chung bất cứ thứ gì

Ba thứ dưới đây phải **hoàn toàn của riêng bạn**, không copy từ ai:

| Thành phần | Biến | Vì sao |
|---|---|---|
| Khoá riêng Ed25519 | `AGENT_PRIVATE_KEY` | Trùng khoá = cùng một danh tính on-chain |
| DID | `AGENT_DID` (để trống, tự sinh) | Dán lại DID người khác = giả danh / trùng |
| Namespace state | `KV_NS`, `STATE_FILE` | Chung KV/state = bằng chứng liên kết rõ nhất |
| Tài khoản GitHub & Secrets | (Settings → Secrets) | Chung account = chung chủ sở hữu |
| Ví nhận FLOP | (ví của bạn) | Airdrop chấm theo ví; chung ví = gom về một mối |
| API key LLM | `*_API_KEY` | Nên tách để chi phí & hành vi độc lập |

---

## Các bước

### 1. Fork & clone
- Bấm **Fork** trên GitHub về tài khoản của **chính bạn**.
- `git clone` fork đó về máy.

### 2. Tạo danh tính mới
```bash
# Sinh seed Ed25519 mới (giữ bí mật tuyệt đối):
python -c "import os;print(os.urandom(32).hex())"
```
- Dán kết quả vào `AGENT_PRIVATE_KEY`.
- Để `AGENT_DID` **trống** → code tự dẫn xuất DID từ khoá trên.
- Đặt `AGENT_NAME`, `HANDLE` riêng; đặt `KV_NS` **duy nhất** (vd `ten-cua-ban-<random>`).

### 3. Cấu hình `.env`
```bash
cp .env.example .env
```
Điền theo mục 1 & 2 trong `.env`. Trên GitHub Actions thì bỏ các bí mật vào
**Settings → Secrets and variables → Actions**, đừng ghi vào file.

### 4. Cài & chạy KHÔ (simulation) trước
```bash
pip install -e .        # hoặc: pip install -r <deps của repo>
pytest -q               # nên xanh toàn bộ trước khi chạy thật
python agent_cron.py
```
- Giữ `TESTNET_ENABLED=0`. Bạn sẽ thấy log dạng `[SIMULATION] Spent … MOCK_FLOP …`.
- Windows: đặt `PYTHONUTF8=1` để log tiếng Việt in đúng.
- Xác nhận agent phản hồi **@mention thật** đúng cách rồi mới đi tiếp.

### 5. Bật nhóm knob chống sybil (đã set sẵn trong `.env.example`)
```
FLOP_ORGANIC_ONLY=1          # chỉ chi khi có event_id thật → chặn burn-loop
FLOP_PACE_JITTER_PCT=12      # phá đường chi tuyến tính
FLOP_MAX_SPENDS_PER_HOUR=6   # trần nhịp chi/giờ
FLOP_FAUCET_DEMAND_ONLY=1    # chỉ claim khi cạn (kèm FLOP_FAUCET_REFILL_BELOW)
FLOP_FAUCET_MAX_PER_DAY=2    # trần claim/ngày
FLOP_FAUCET_JITTER_MIN=1     # jitter cooldown
FLOP_PUBLISH_UNLOCK=1        # công khai spend_stats() để tự audit
```
Đây là đánh đổi *một chút* throughput lấy hồ sơ organic sạch — đáng, vì clawback
hồi tố (Arbitrum/OP/zkSync) là chuyện thường xảy ra.

### 6. Chỉ khi FLOP mở inference thật mới nối testnet
- Đặt `TESTNET_ENABLED=1` **và** điền `FLOP_RPC_URL` / `FLOP_SUBMIT_URL`.
- Nếu bật testnet mà thiếu RPC/SUBMIT, code trả `skipped_unconfigured` (không bịa tx) — đúng như thiết kế.

---

## Checklist trước khi bật "thật" ✅

- [ ] `AGENT_PRIVATE_KEY` sinh mới, chưa ai từng dùng
- [ ] `AGENT_DID` để trống (tự sinh), KHÔNG dán DID người khác
- [ ] `KV_NS` duy nhất, `STATE_FILE`/ledger bắt đầu từ trống
- [ ] Tài khoản GitHub + Secrets riêng của bạn
- [ ] Ví nhận FLOP là ví riêng của bạn
- [ ] API key LLM riêng
- [ ] `pytest` xanh; đã chạy simulation và thấy agent phản hồi @mention thật
- [ ] Nhóm knob mục 5 đang bật
- [ ] Chỉ 1 agent / 1 người — không nhân bản

> Cơ sở scoring (từ teaser flop.finance, đọc 28/08/2026 — **vẫn là teaser, chưa phải
> luật cuối**): pool agent 7%, chia **pro-rata theo lượng chi cho inference thật**.
> Cạnh tranh bằng *khối lượng công việc thật*, không phải bằng số lượng account.
> Teaser chưa công bố anti-sybil/KYC, nhưng cứ chạy sạch từ đầu là an toàn nhất.
