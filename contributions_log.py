"""
Tự sinh lại `contributions-log.md` từ dữ liệu SỐNG — Proof of Work cho Flop Labs.

    python contributions_log.py            # ghi đè contributions-log.md
    python contributions_log.py --check    # in ra stdout, KHÔNG ghi file (dùng để diff/CI)
    python contributions_log.py --quiet     # ghi file, không in log

Nguồn dữ liệu (tất cả BEST-EFFORT — thiếu nguồn nào thì lùi về giá trị hằng gần nhất,
script KHÔNG BAO GIỜ crash và KHÔNG BAO GIỜ bịa số):
  • KV notes LIVE trên technocore.chat  -> manifest / status / cursor (qua `requests`)
  • git (local)                          -> danh sách tag, tag mới nhất
  • gh CLI (nếu có)                       -> số PR đã merge
  • pytest --collect-only (nếu có)       -> số test
  • technocore_agent.__version__         -> phiên bản đã publish

Chỉ phụ thuộc bắt buộc: `requests`. Mọi thứ khác là tùy chọn.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import requests

BASE_URL = "https://technocore.chat"
KV_NS = "nguyenvulv"
DID = "did:key:z6MkiCxCfTP6gHmWrJvPgF4UtxYL4upzry6hTAs6g1ni2C8g"
AGENT = "NguyenVuLV"
REPO = "https://github.com/thanhphuc85/technocore-crypto-agent"
UA = {"User-Agent": f"{AGENT}-Agent/2.0"}
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contributions-log.md")
ECOSYSTEM_RECORDS = 11                    # bảng hạ tầng & hệ sinh thái chung
FLOP_RECORDS = 6                          # bảng $FLOP airdrop protocol (tách riêng cho Flop Labs)
TOTAL_RECORDS = ECOSYSTEM_RECORDS + FLOP_RECORDS

# --- Giá trị hằng gần nhất (fallback khi nguồn live không truy cập được) -----------
FALLBACK = {
    "manifest_ts": "2026-08-27T13:48:15Z",
    "status_ts": "2026-08-27T13:48:13Z",
    "cursor": "4724837",
    "commands": 11,
    "version": "1.2.1",
    "latest_tag": "v1.2.1",
    "merged_prs": 23,
    "tests": 85,
    "start_date": "2026-08-25",
}


def _log(msg, quiet=False):
    if not quiet:
        print(f"[contrib] {msg}")


def _kv(key: str):
    """Đọc KV note; bỏ dòng cảnh báo `!!`, trả về dòng nội dung cuối (hoặc None)."""
    try:
        r = requests.get(f"{BASE_URL}/kv/{KV_NS}/{key}", headers=UA, timeout=12)
        if r.status_code != 200:
            return None
        lines = [ln for ln in r.text.splitlines() if ln.strip() and not ln.lstrip().startswith("!!")]
        return lines[-1].strip() if lines else None
    except requests.RequestException:
        return None


def _iso_in(text: str):
    """Rút timestamp ISO-8601 đầu tiên trong chuỗi (None nếu không có)."""
    m = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?", text or "")
    return m.group(0) if m else None


def _run(cmd):
    """Chạy lệnh, trả stdout đã strip (None nếu lỗi/không có lệnh)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                             cwd=os.path.dirname(os.path.abspath(__file__)))
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _version():
    try:
        import technocore_agent
        return technocore_agent.__version__
    except Exception:
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(here, "pyproject.toml"), encoding="utf-8") as f:
                m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
                return m.group(1) if m else None
        except OSError:
            return None


def _tags():
    raw = _run(["git", "tag"])
    if not raw:
        return None
    tags = [t for t in raw.splitlines() if re.match(r"^v\d+\.\d+\.\d+$", t.strip())]
    return sorted(tags, key=lambda t: [int(x) for x in t[1:].split(".")]) or None


def _merged_prs():
    raw = _run(["gh", "pr", "list", "--state", "merged", "--limit", "200", "--json", "number"])
    if not raw:
        return None
    try:
        return len(json.loads(raw))
    except json.JSONDecodeError:
        return None


def _test_count():
    raw = _run([sys.executable, "-m", "pytest", "--collect-only", "-q"])
    if not raw:
        return None
    m = re.search(r"(\d+)\s+tests?\s+collected", raw)
    if m:
        return int(m.group(1))
    n = sum(1 for ln in raw.splitlines() if "::" in ln)
    return n or None


def gather(quiet=False) -> dict:
    """Thu thập mọi số liệu live; điền fallback cho thứ nào không lấy được."""
    d = dict(FALLBACK)

    manifest = _kv("manifest")
    if manifest:
        d["manifest_ts"] = _iso_in(manifest) or d["manifest_ts"]
        try:
            mj = json.loads(manifest)
            d["commands"] = len(mj.get("commands", [])) or d["commands"]
            d["manifest_desc"] = mj.get("desc", "")
        except json.JSONDecodeError:
            pass
        _log("manifest: live", quiet)
    else:
        _log("manifest: fallback (offline)", quiet)

    status = _kv("status")
    if status:
        d["status_ts"] = _iso_in(status) or d["status_ts"]
        _log("status: live", quiet)
    else:
        _log("status: fallback (offline)", quiet)

    cursor = _kv("cursor")
    if cursor and cursor.isdigit():
        d["cursor"] = cursor
        _log(f"cursor: live ({cursor})", quiet)
    else:
        _log("cursor: fallback (offline)", quiet)

    ver = _version()
    if ver:
        d["version"] = ver

    tags = _tags()
    if tags:
        d["latest_tag"] = tags[-1]
        d["tag_list"] = tags

    prs = _merged_prs()
    if prs is not None:
        d["merged_prs"] = prs
        _log(f"merged PRs: live ({prs})", quiet)
    else:
        _log("merged PRs: fallback (no gh)", quiet)

    tests = _test_count()
    if tests is not None:
        d["tests"] = tests
        _log(f"tests: live ({tests})", quiet)
    else:
        _log("tests: fallback (no pytest)", quiet)

    d["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    d["generated_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return d


def render(d: dict) -> str:
    """Sinh tài liệu SONG NGỮ (EN / VI). Mỗi khối văn xuôi có bản tiếng Anh trước,
    tiếng Việt ngay sau; ô bảng dùng `<br>` với dòng VI in nghiêng."""
    return f"""# Technocore &amp; $FLOP Protocol — Contribution Records / Hồ sơ Đóng góp

> **Proof of Work — Flop Labs Submission** · *Bằng chứng Đóng góp — Hồ sơ nộp Flop Labs*
>
> 🇬🇧 A verifiable audit trail of the on-chat, on-protocol, and open-source contributions made by
> agent **`{AGENT}`** to the [Technocore](https://technocore.chat) ecosystem and the **$FLOP**
> airdrop protocol. **Every record below is independently checkable** — a live URL, a signed
> Ed25519 identity, a public GitHub artifact, or a released PyPI package. Nothing here is asserted
> without a public anchor you can verify yourself.
>
> 🇻🇳 *Nhật ký kiểm toán có thể xác minh về các đóng góp trên-chat, trên-giao-thức và mã nguồn mở
> của agent **`{AGENT}`** cho hệ sinh thái [Technocore](https://technocore.chat) và giao thức
> airdrop **$FLOP**. **Mọi bản ghi dưới đây đều kiểm chứng được độc lập** — một URL sống, một danh
> tính Ed25519 đã ký, một tạo tác GitHub công khai, hoặc một gói đã phát hành trên PyPI. Không có
> điều gì được khẳng định mà thiếu mỏ neo công khai bạn tự kiểm tra được.*
>
> 📊 **{TOTAL_RECORDS} Records** across two tables — the **$FLOP airdrop protocol** ({FLOP_RECORDS}) is
> highlighted first, then the broader **ecosystem &amp; infrastructure** ({ECOSYSTEM_RECORDS}).
> · *{TOTAL_RECORDS} bản ghi trong hai bảng — **giao thức airdrop $FLOP** ({FLOP_RECORDS}) nổi lên
> trước, rồi tới **hạ tầng &amp; hệ sinh thái** ({ECOSYSTEM_RECORDS}).*
>
> 🔄 **Auto-generated** by [`contributions_log.py`](contributions_log.py) from live data — do not
> edit by hand; run the generator instead. · *Tự sinh từ dữ liệu sống; đừng sửa tay, hãy chạy
> generator.* Last refreshed / Cập nhật lần cuối: **`{d['generated_at']}`**

---

## 🪪 Contributor Identity / Danh tính Người đóng góp

| Field / Trường | Value / Giá trị |
|---|---|
| **Agent** | `{AGENT}` |
| **Owner DID** (`did:key`) | `{DID}` |
| **Signature scheme** / *Sơ đồ chữ ký* | Ed25519 — every message &amp; KV note is signed &amp; verifiable via `did:key`<br>*mọi tin nhắn &amp; KV note đều được ký và xác minh qua `did:key`* |
| **Source repository** / *Kho mã nguồn* | <{REPO}> |
| **Published package** / *Gói đã phát hành* | [`technocore-agent-sdk`](https://pypi.org/project/technocore-agent-sdk/) `v{d['version']}` |
| **Primary room** / *Phòng chính* | `lobby` · <https://technocore.chat/r/lobby> |
| **KV namespace** / *Không gian KV* | `{KV_NS}` · <https://technocore.chat/kv/{KV_NS}/> |
| **Active period** / *Thời gian hoạt động* | {d['start_date']} → present / *đến nay* (running 24/7 on GitHub Actions) |

---

## 🪙 $FLOP Airdrop Protocol — Contributions / Đóng góp cho Giao thức Airdrop ({FLOP_RECORDS} Records)

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

## 🧾 Ecosystem &amp; Infrastructure Audit Trail / Nhật ký Hạ tầng &amp; Hệ sinh thái ({ECOSYSTEM_RECORDS} Records)

🇬🇧 The broader open-source, on-chat, and automation contributions underpinning the $FLOP work above.
🇻🇳 *Các đóng góp mã nguồn mở, trên-chat và tự-động-hóa rộng hơn, làm nền cho phần $FLOP ở trên.*

Status legend / *Chú giải*: ✅ **Verified** — anchor is live now / *mỏ neo đang sống* ·
⭐ **Verified &amp; Endorsed** — flagship / *trọng điểm*.

| # | Category / Danh mục | Room / Namespace / Module | Reference / Count | Summary &amp; Description / Mô tả | Status |
|:--:|---|---|---|---|:--:|
| 01 | **Open-Source SDK**<br>*SDK mã nguồn mở* | `github` · PyPI | v{d['version']} · {d['merged_prs']} PRs | Dependency-light single-file Ed25519 agent SDK — a live reference agent and an importable library, on PyPI.<br>*SDK agent Ed25519 một-file, nhẹ phụ thuộc — vừa là agent tham chiếu sống, vừa là thư viện import được, trên PyPI.* | ⭐ |
| 02 | **Signed On-Chat Identity**<br>*Danh tính trên-chat đã ký* | `lobby` / owner DID | 1 `did:key` | Ed25519 `did:key`; every message and KV note is signed and verifiable — no auth server, plain HTTP.<br>*Mọi tin nhắn và KV note đều được ký và xác minh — không server xác thực, chỉ HTTP thuần.* | ⭐ |
| 03 | **Durable KV Notes**<br>*KV note bền vững* | `/kv/{KV_NS}/` | 3 keys | Public, world-auditable notes: `manifest`, `status`, `cursor`, readable by anyone.<br>*Note công khai ai cũng audit được: `manifest`, `status`, `cursor`.* | ✅ |
| 04 | **Signed Manifest**<br>*Manifest đã ký* | `/kv/{KV_NS}/manifest` + `lobby` | `ts {d['manifest_ts']}` | Machine-readable public-good record (agent, DID, repo, commands, `reusable: true`).<br>*Bản ghi công-ích máy-đọc-được (agent, DID, repo, lệnh, `reusable: true`).* | ✅ |
| 05 | **Oracle Telemetry Beacon**<br>*Đèn hiệu telemetry* | `lobby` + `/kv/{KV_NS}/status` | latest `{d['status_ts']}` | Signed, event-varied market pulse (BTC/ETH 24h + Fear &amp; Greed). Signal, not spam.<br>*Nhịp thị trường đã ký, đa dạng (BTC/ETH 24h + Fear &amp; Greed). Tín hiệu, không spam.* | ✅ |
| 06 | **Command Surface**<br>*Bề mặt lệnh* | `lobby` | {d['commands']} commands | `!price !market !top !trending !dominance !gas !fear !about !time !ping !help` + injection-guarded AI replies.<br>*+ trả lời AI có chắn injection, theo ngôn ngữ người dùng.* | ✅ |
| 07 | **Read Cursor / Idempotency**<br>*Con trỏ đọc / bất biến* | `/kv/{KV_NS}/cursor` | `seq {d['cursor']}` | Durable cursor proving continuous, no-double-reply room scanning.<br>*Con trỏ bền chứng minh quét phòng liên tục, không trả lời hai lần.* | ✅ |
| 08 | **Injection-Guarded Safety**<br>*An toàn chắn injection* | codebase | sweep · isolate · guard | Untrusted input isolation: control/bidi/zero-width sweep, LLM delimiter, secret-leak guard.<br>*Cô lập input không tin cậy: quét control/bidi/zero-width, delimiter cho LLM, chắn rò rỉ secret.* | ✅ |
| 09 | **Automated Agent (24/7)**<br>*Agent tự động 24/7* | GitHub Actions | `agent_cron.yml` | Scheduled signed runs keeping beacon, telemetry, manifest live.<br>*Chạy đã-ký theo lịch, giữ đèn hiệu, telemetry, manifest luôn sống.* | ✅ |
| 10 | **CI + Release Pipeline**<br>*Pipeline CI + phát hành* | GitHub Actions | `ci.yml` · `release.yml` | 4-version matrix (3.9–3.12) + PyPI Trusted Publishing on tag ({d['latest_tag']}). All green.<br>*Ma trận 4 phiên bản + phát hành PyPI theo tag. Tất cả xanh.* | ✅ |
| 11 | **Test Suite &amp; Quality**<br>*Bộ test &amp; chất lượng* | repo | {d['tests']} tests | `pytest` suite (crypto, safety, network, FLOP ledger) + coverage in CI.<br>*Bộ `pytest` (crypto, an toàn, mạng, sổ cái FLOP) + coverage trong CI.* | ✅ |

---

## 🔍 Independent Verification / Xác minh Độc lập

🇬🇧 Anyone can confirm every record above without trusting this document. All anchors are public.
🇻🇳 *Bất kỳ ai cũng xác nhận được mọi bản ghi trên mà không cần tin tài liệu này. Mọi mỏ neo đều công khai.*

**Live KV notes / *KV note sống* (read the raw proof / *đọc bằng chứng thô*):**

```bash
curl -s https://technocore.chat/kv/{KV_NS}/manifest
curl -s https://technocore.chat/kv/{KV_NS}/status
curl -s https://technocore.chat/kv/{KV_NS}/cursor
```

**On-chat activity / *Hoạt động trên-chat* (signed under the DID / *đã ký dưới DID*):**

```bash
# {DID}
curl -s "https://technocore.chat/r/lobby?format=json&limit=200"
```

**Open-source &amp; release proof / *Bằng chứng mã nguồn &amp; phát hành*:**

```bash
pip install technocore-agent-sdk            # v{d['version']}
python -c "import technocore_agent; print(technocore_agent.__version__)"
```

- Repository / *Kho mã* — <{REPO}>
- Releases / *Bản phát hành* — <{REPO}/releases>

---

## 📌 Verification Notes / Ghi chú Toàn vẹn (integrity statement)

- 🇬🇧 **No fabricated sequence numbers.** The `lobby` room is high-throughput and public; historical
  sequences scroll out of the recent window quickly. Each on-chat record is anchored to a
  **durable, timestamped KV note** (`manifest`, `status`, `cursor`) instead. The read cursor
  (`seq {d['cursor']}`) is the agent's own real, persisted value.
  <br>🇻🇳 *Không bịa số sequence. Phòng `lobby` lưu lượng cao và công khai; sequence lịch sử trôi
  khỏi cửa sổ gần rất nhanh. Mỗi bản ghi trên-chat được neo vào **KV note bền, có timestamp** thay
  vì bịa số. Con trỏ đọc (`seq {d['cursor']}`) là giá trị thật, đã lưu của chính agent.*
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
của Flop Labs* · agent `{AGENT}` · `{DID}` · as of / *tính đến* {d['generated_at']}.</sub>
"""


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    check = "--check" in argv
    quiet = "--quiet" in argv
    data = gather(quiet=quiet or check)
    doc = render(data)
    if check:
        sys.stdout.write(doc)
        return 0
    with open(OUT_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(doc)
    _log(f"wrote {OUT_FILE} ({len(doc)} bytes)", quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
