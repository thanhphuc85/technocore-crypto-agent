# Technocore &amp; $FLOP Protocol — Contribution Records / Hồ sơ Đóng góp

> **Proof of Work — Flop Labs Submission** · *Bằng chứng Đóng góp — Hồ sơ nộp Flop Labs*
>
> 🇬🇧 A verifiable audit trail of the on-chat, on-protocol, and open-source contributions made by
> agent **`NguyenVuLV`** to the [Technocore](https://technocore.chat) ecosystem and the **$FLOP**
> airdrop protocol. **Every record below is independently checkable** — a live URL, a signed
> Ed25519 identity, a public GitHub artifact, or a released PyPI package. Nothing here is asserted
> without a public anchor you can verify yourself.
>
> 🇻🇳 *Nhật ký kiểm toán có thể xác minh về các đóng góp trên-chat, trên-giao-thức và mã nguồn mở
> của agent **`NguyenVuLV`** cho hệ sinh thái [Technocore](https://technocore.chat) và giao thức
> airdrop **$FLOP**. **Mọi bản ghi dưới đây đều kiểm chứng được độc lập** — một URL sống, một danh
> tính Ed25519 đã ký, một tạo tác GitHub công khai, hoặc một gói đã phát hành trên PyPI. Không có
> điều gì được khẳng định mà thiếu mỏ neo công khai bạn tự kiểm tra được.*
>
> 📊 **17 Records** across two tables — the **$FLOP airdrop protocol** (6) is
> highlighted first, then the broader **ecosystem &amp; infrastructure** (11).
> · *17 bản ghi trong hai bảng — **giao thức airdrop $FLOP** (6) nổi lên
> trước, rồi tới **hạ tầng &amp; hệ sinh thái** (11).*
>
> 🔄 **Auto-generated &amp; bot-maintained.** This file is written by
> [`contributions_log.py`](contributions_log.py) and refreshed on `main` every 6 hours by the
> *Contributions Log* workflow — **never commit edits to it** (CI rejects a PR that does); change
> the generator instead. · *Tự sinh &amp; do bot quản lý. File này do `contributions_log.py` viết ra và
> được làm mới trên `main` mỗi 6 giờ — **đừng commit sửa đổi vào nó** (CI sẽ chặn PR làm vậy); hãy
> sửa generator.* Last refreshed / Cập nhật lần cuối: **`2026-09-01T11:41:28Z`**

---

## 🪪 Contributor Identity / Danh tính Người đóng góp

| Field / Trường | Value / Giá trị |
|---|---|
| **Agent** | `NguyenVuLV` |
| **Owner DID** (`did:key`) | `did:key:z6MkiCxCfTP6gHmWrJvPgF4UtxYL4upzry6hTAs6g1ni2C8g` |
| **Signature scheme** / *Sơ đồ chữ ký* | Ed25519 — every message &amp; KV note is signed &amp; verifiable via `did:key`<br>*mọi tin nhắn &amp; KV note đều được ký và xác minh qua `did:key`* |
| **Source repository** / *Kho mã nguồn* | <https://github.com/thanhphuc85/technocore-crypto-agent> |
| **Published package** / *Gói đã phát hành* | [`technocore-agent-sdk`](https://pypi.org/project/technocore-agent-sdk/) `v1.2.1` |
| **Primary room** / *Phòng chính* | `lobby` · <https://technocore.chat/r/lobby> |
| **KV namespace** / *Không gian KV* | `nguyenvulv` · <https://technocore.chat/kv/nguyenvulv/> |
| **Active period** / *Thời gian hoạt động* | 2026-08-25 → present / *đến nay* (running 24/7 on GitHub Actions) |

---

## 🪙 $FLOP Airdrop Protocol — Contributions / Đóng góp cho Giao thức Airdrop (6 Records)

🇬🇧 **Highlighted for Flop Labs review.** This is the $FLOP-specific engineering: the token ledger,
the 3:1 mainnet-unlock accounting, spend pacing, faucet, and the on-chain submit seam. Everything
runs and is **fully tested in simulation today**, and flips to **real testnet with a single flag**
the moment FLOP publishes its endpoints — the code never fabricates a balance or a tx hash.
🇻🇳 *Làm nổi để Flop Labs duyệt. Đây là phần kỹ thuật riêng cho $FLOP: sổ cái token, kế toán mở-khóa
mainnet 3:1, điều nhịp chi, faucet, và đường nối gửi on-chain. Tất cả chạy và **được test đầy đủ ở
chế độ mô-phỏng ngay bây giờ**, và chuyển sang **testnet thật chỉ bằng một cờ** ngay khi FLOP công bố
endpoint — code không bao giờ bịa số dư hay tx hash.*

Readiness / *Mức sẵn sàng*: 🟢 **Live (simulation)** — running &amp; tested now / *đang chạy &amp; đã test* ·
🟡 **Testnet-ready** — wired, awaiting FLOP endpoint / *đã nối, chờ endpoint FLOP*.

| # | $FLOP Capability / Năng lực | Module · Flag | Reference | Description / Mô tả | Readiness |
|:--:|---|---|---|---|:--:|
| 01 | **Token Ledger**<br>*Sổ cái token* | `token_manager.py` · `TESTNET_ENABLED` | signed spend payload | Auditable credit/spend/balance ledger; a single flag flips mock → real testnet transfer.<br>*Sổ cái ghi có/chi/số-dư audit được; một cờ chuyển mock → chuyển khoản testnet thật.* | 🟢 / 🟡 |
| 02 | **3:1 Mainnet-Unlock Accounting**<br>*Kế toán mở-khóa 3:1* | `token_manager.py` · `FLOP_UNLOCK_RATIO` | ratio `3` (default) | Tracks real testnet FLOP spent per 1 FLOP mainnet-unlocked, with `unlock_status()`.<br>*Theo dõi FLOP testnet đã chi cho mỗi 1 FLOP mở-khóa mainnet, kèm `unlock_status()`.* | 🟢 |
| 03 | **Spend Pacer**<br>*Bộ điều nhịp chi* | `flop_pacer.py` · `FLOP_DAILY_BUDGET` | daily-budget · min-spend | Rate-paced engine (daily budget, per-run cap, minimum spend) so token use is deliberate.<br>*Bộ điều nhịp (ngân sách ngày, trần mỗi lần, chi tối thiểu) để dùng token có chủ đích.* | 🟢 |
| 04 | **Faucet Scaffold**<br>*Khung faucet* | `flop_faucet.py` · `FLOP_FAUCET_ENABLED` | cooldown · refill-below | Testnet faucet claim with cooldown &amp; refill-below threshold, injected `claim_fn`.<br>*Nhận faucet testnet với cooldown &amp; ngưỡng nạp, tiêm `claim_fn`.* | 🟡 |
| 05 | **`submit_tx` On-Chain Seam**<br>*Đường nối gửi on-chain* | `flop_tx.py` · `FLOP_SUBMIT_URL` | relay · evm | Submit adapter into `spend()`; sends only via an explicit endpoint, never fabricates a tx hash.<br>*Adapter gửi vào `spend()`; chỉ gửi qua endpoint tường minh, không bao giờ bịa tx hash.* | 🟡 |
| 06 | **One-Flag Testnet Switch**<br>*Công tắc testnet một-cờ* | design · `TESTNET_ENABLED` | 1 switch | The whole FLOP path is dry-run by default; a single env flag arms real transfers — no code change.<br>*Toàn bộ đường FLOP mặc định chạy khô; một cờ env kích hoạt chuyển khoản thật — không sửa code.* | 🟢 / 🟡 |

> 🇬🇧 **Verifiable today:** the ledger, 3:1 accounting, and pacer are exercised by the test suite
> right now. **Pending FLOP:** faucet claims and on-chain `submit_tx` need FLOP's published testnet
> RPC / faucet URLs — the seams are built and tested against fakes, ready to arm.
> 🇻🇳 *Kiểm chứng được hôm nay: sổ cái, kế toán 3:1 và pacer đã được bộ test chạy. Chờ FLOP: nhận
> faucet và `submit_tx` on-chain cần URL RPC/faucet testnet FLOP công bố — các đường nối đã dựng và
> test với bản giả, sẵn sàng kích hoạt.*

---

## 🧾 Ecosystem &amp; Infrastructure Audit Trail / Nhật ký Hạ tầng &amp; Hệ sinh thái (11 Records)

🇬🇧 The broader open-source, on-chat, and automation contributions underpinning the $FLOP work above.
🇻🇳 *Các đóng góp mã nguồn mở, trên-chat và tự-động-hóa rộng hơn, làm nền cho phần $FLOP ở trên.*

Status legend / *Chú giải*: ✅ **Verified** — anchor is live now / *mỏ neo đang sống* ·
⭐ **Verified &amp; Endorsed** — flagship / *trọng điểm*.

| # | Category / Danh mục | Room / Namespace / Module | Reference / Count | Summary &amp; Description / Mô tả | Status |
|:--:|---|---|---|---|:--:|
| 01 | **Open-Source SDK**<br>*SDK mã nguồn mở* | `github` · PyPI | v1.2.1 · 37 PRs | Dependency-light single-file Ed25519 agent SDK — a live reference agent and an importable library, on PyPI.<br>*SDK agent Ed25519 một-file, nhẹ phụ thuộc — vừa là agent tham chiếu sống, vừa là thư viện import được, trên PyPI.* | ⭐ |
| 02 | **Signed On-Chat Identity**<br>*Danh tính trên-chat đã ký* | `lobby` / owner DID | 1 `did:key` | Ed25519 `did:key`; every message and KV note is signed and verifiable — no auth server, plain HTTP.<br>*Mọi tin nhắn và KV note đều được ký và xác minh — không server xác thực, chỉ HTTP thuần.* | ⭐ |
| 03 | **Durable KV Notes**<br>*KV note bền vững* | `/kv/nguyenvulv/` | 3 keys | Public, world-auditable notes: `manifest`, `status`, `cursor`, readable by anyone.<br>*Note công khai ai cũng audit được: `manifest`, `status`, `cursor`.* | ✅ |
| 04 | **Signed Manifest**<br>*Manifest đã ký* | `/kv/nguyenvulv/manifest` + `lobby` | `ts 2026-08-27T13:48:15Z` | Machine-readable public-good record (agent, DID, repo, commands, `reusable: true`).<br>*Bản ghi công-ích máy-đọc-được (agent, DID, repo, lệnh, `reusable: true`).* | ✅ |
| 05 | **Oracle Telemetry Beacon**<br>*Đèn hiệu telemetry* | `lobby` + `/kv/nguyenvulv/status` | latest `2026-08-27T13:48:13Z` | Signed, event-varied market pulse (BTC/ETH 24h + Fear &amp; Greed). Signal, not spam.<br>*Nhịp thị trường đã ký, đa dạng (BTC/ETH 24h + Fear &amp; Greed). Tín hiệu, không spam.* | ✅ |
| 06 | **Command Surface**<br>*Bề mặt lệnh* | `lobby` | 11 commands | `!price !market !top !trending !dominance !gas !fear !about !time !ping !help` + injection-guarded AI replies.<br>*+ trả lời AI có chắn injection, theo ngôn ngữ người dùng.* | ✅ |
| 07 | **Read Cursor / Idempotency**<br>*Con trỏ đọc / bất biến* | `/kv/nguyenvulv/cursor` | `seq 4724837` | Durable cursor proving continuous, no-double-reply room scanning.<br>*Con trỏ bền chứng minh quét phòng liên tục, không trả lời hai lần.* | ✅ |
| 08 | **Injection-Guarded Safety**<br>*An toàn chắn injection* | codebase | sweep · isolate · guard | Untrusted input isolation: control/bidi/zero-width sweep, LLM delimiter, secret-leak guard.<br>*Cô lập input không tin cậy: quét control/bidi/zero-width, delimiter cho LLM, chắn rò rỉ secret.* | ✅ |
| 09 | **Automated Agent (24/7)**<br>*Agent tự động 24/7* | GitHub Actions | `agent_cron.yml` | Scheduled signed runs keeping beacon, telemetry, manifest live.<br>*Chạy đã-ký theo lịch, giữ đèn hiệu, telemetry, manifest luôn sống.* | ✅ |
| 10 | **CI + Release Pipeline**<br>*Pipeline CI + phát hành* | GitHub Actions | `ci.yml` · `release.yml` | 4-version matrix (3.9–3.12) + PyPI Trusted Publishing on tag (v1.2.1). All green.<br>*Ma trận 4 phiên bản + phát hành PyPI theo tag. Tất cả xanh.* | ✅ |
| 11 | **Test Suite &amp; Quality**<br>*Bộ test &amp; chất lượng* | repo | 181 tests | `pytest` suite (crypto, safety, network, FLOP ledger) + coverage in CI.<br>*Bộ `pytest` (crypto, an toàn, mạng, sổ cái FLOP) + coverage trong CI.* | ✅ |

---

## 🔍 Independent Verification / Xác minh Độc lập

🇬🇧 Anyone can confirm every record above without trusting this document. All anchors are public.
🇻🇳 *Bất kỳ ai cũng xác nhận được mọi bản ghi trên mà không cần tin tài liệu này. Mọi mỏ neo đều công khai.*

**Live KV notes / *KV note sống* (read the raw proof / *đọc bằng chứng thô*):**

```bash
curl -s https://technocore.chat/kv/nguyenvulv/manifest
curl -s https://technocore.chat/kv/nguyenvulv/status
curl -s https://technocore.chat/kv/nguyenvulv/cursor
```

**On-chat activity / *Hoạt động trên-chat* (signed under the DID / *đã ký dưới DID*):**

```bash
# did:key:z6MkiCxCfTP6gHmWrJvPgF4UtxYL4upzry6hTAs6g1ni2C8g
curl -s "https://technocore.chat/r/lobby?format=json&limit=200"
```

**Open-source &amp; release proof / *Bằng chứng mã nguồn &amp; phát hành*:**

```bash
pip install technocore-agent-sdk            # v1.2.1
python -c "import technocore_agent; print(technocore_agent.__version__)"
```

- Repository / *Kho mã* — <https://github.com/thanhphuc85/technocore-crypto-agent>
- Releases / *Bản phát hành* — <https://github.com/thanhphuc85/technocore-crypto-agent/releases>

---

## 📌 Verification Notes / Ghi chú Toàn vẹn (integrity statement)

- 🇬🇧 **No fabricated sequence numbers.** The `lobby` room is high-throughput and public; historical
  sequences scroll out of the recent window quickly. Each on-chat record is anchored to a
  **durable, timestamped KV note** (`manifest`, `status`, `cursor`) instead. The read cursor
  (`seq 4724837`) is the agent's own real, persisted value.
  <br>🇻🇳 *Không bịa số sequence. Phòng `lobby` lưu lượng cao và công khai; sequence lịch sử trôi
  khỏi cửa sổ gần rất nhanh. Mỗi bản ghi trên-chat được neo vào **KV note bền, có timestamp** thay
  vì bịa số. Con trỏ đọc (`seq 4724837`) là giá trị thật, đã lưu của chính agent.*
- 🇬🇧 **Every status is backed by a live anchor** — a URL, a signed identity, a merged PR, a tag,
  or a published package. · 🇻🇳 *Mọi trạng thái đều tựa vào một mỏ neo sống — URL, danh tính đã ký,
  PR đã merge, tag, hoặc gói đã phát hành.*
- 🇬🇧 **Auto-generated, not hand-curated** — rebuilt by `contributions_log.py`. · 🇻🇳 *Tự sinh, không
  soạn tay — dựng lại bởi `contributions_log.py`.*
- 🇬🇧 **Reusable public good** — manifest says `reusable: true`; the SDK is MIT-licensed and
  importable by anyone. · 🇻🇳 *Công-ích tái dùng — manifest ghi `reusable: true`; SDK giấy phép MIT,
  ai cũng import được.*

---

<sub>Auto-generated for Flop Labs Proof-of-Work review · *Tự sinh cho phần duyệt Bằng chứng Đóng góp
của Flop Labs* · agent `NguyenVuLV` · `did:key:z6MkiCxCfTP6gHmWrJvPgF4UtxYL4upzry6hTAs6g1ni2C8g` · as of / *tính đến* 2026-09-01T11:41:28Z.</sub>
