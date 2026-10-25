"""
token_manager.py — Sổ cái (ledger) token FLOP cho agent Technocore.

Đây là lớp "quản lý giao dịch token" mà nhiều guide săn airdrop khuyến nghị dựng
SẴN trước khi testnet mở: giữ số dư ví, kiểm tra số dư (check_balance), ký giao
dịch (sign_transaction) và chi tiêu (spend) — để đến ngày FLOP mở Faucet Testnet
chỉ cần "bật van" (đổi 1 biến môi trường) là chạy thật, KHÔNG phải sửa core logic.

Cùng tinh thần với phần còn lại của repo: đọc secret từ env (không hardcode),
KHÔNG raise ở top-level (import được như thư viện), và mỗi nhánh đều trả về một
kết quả rõ ràng thay vì ném lỗi làm sập agent 24/7.

MỘT công tắc duy nhất quyết định hành vi — biến môi trường TESTNET_ENABLED:

  TESTNET_ENABLED=false (mặc định)  ->  SIMULATION (Mock):
      spend() trừ số dư MOCK và log dòng:
          [SIMULATION] Spent 0.001 MOCK_FLOP for Gemini Inference
      Không đụng tới bất kỳ blockchain / RPC nào.

  TESTNET_ENABLED=true              ->  TESTNET (thật):
      spend() gửi giao dịch THẬT, nhưng CHỈ qua một hàm submit_tx được tiêm vào
      + một FLOP_RPC_URL tường minh. Thiếu một trong hai -> trả 'skipped_unconfigured'
      và KHÔNG gửi gì (không bao giờ bịa ra tx hash).

Khi FLOP công bố RPC testnet: nạp Private Key/RPC vào Secrets, cài submit_tx theo
chuẩn của họ, đổi TESTNET_ENABLED=true. Logic kế toán ở đây giữ nguyên.

Chạy thử offline (không key, không mạng):
    python token_manager.py
"""

import os
import hashlib
import json
import time
import calendar
from decimal import Decimal, InvalidOperation

# Tái dùng crypto Ed25519 của SDK (did:key + ký). Import "mềm": nếu vì lý do gì
# agent_cron không import được, token_manager vẫn dùng được ở chế độ không-ký.
try:
    from agent_cron import sign_message, did_of, load_private_key
except Exception as _e:  # pragma: no cover - chỉ chạy khi môi trường thiếu SDK
    sign_message = None
    did_of = None
    load_private_key = None
    print(f"[ledger] cảnh báo: không import được crypto từ agent_cron ({_e}); "
          "sign_transaction sẽ bị tắt.")


# --- Cấu hình (đọc LIVE từ env mỗi lần gọi -> đổi cờ là đổi hành vi ngay) ---------

DEFAULT_TOKEN = "FLOP"


def _env_flag(name: str) -> bool:
    """True nếu biến env bật (1/true/on/yes), phần còn lại (kể cả rỗng) -> False."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "on", "yes")


def organic_only() -> bool:
    """FLOP_ORGANIC_ONLY — MẶC ĐỊNH BẬT: chỉ chi khi lần suy luận gắn một sự kiện thật.
    Chỉ tắt khi đặt tường minh 0/false/off/no. Từng mặc định tắt ("bảo hiểm, bật khi FLOP
    công bố lọc hành vi") — FLOP đã công bố: "an account flagged for manufactured activity
    forfeits its allocation". Fork không đặt Variable giờ cũng được bảo vệ."""
    return os.environ.get("FLOP_ORGANIC_ONLY", "").strip().lower() not in ("0", "false", "off", "no")


def testnet_enabled() -> bool:
    """Công tắc dry-run: bật giao dịch testnet THẬT khi TESTNET_ENABLED=true."""
    return _env_flag("TESTNET_ENABLED")


def ledger_mode() -> str:
    """'testnet' khi cờ bật, ngược lại 'simulation' (mặc định)."""
    return "testnet" if testnet_enabled() else "simulation"


def rpc_url() -> str:
    """RPC testnet tường minh (nạp khi FLOP công bố). Rỗng -> testnet chưa cấu hình."""
    return os.environ.get("FLOP_RPC_URL", "").strip()


def submit_url() -> str:
    """Endpoint relayer (nếu tách khỏi RPC). Relay adapter ưu tiên biến này."""
    return os.environ.get("FLOP_SUBMIT_URL", "").strip()


def endpoint_url() -> str:
    """Cổng để gửi giao dịch: FLOP_SUBMIT_URL (relayer) hoặc FLOP_RPC_URL. Rỗng ->
    testnet chưa cấu hình endpoint."""
    return submit_url() or rpc_url()


def unlock_ratio() -> Decimal:
    """Tỉ lệ mở khóa mainnet: mỗi `ratio` FLOP chi THẬT trên testnet mở khóa 1 FLOP
    mainnet (mainnet = tổng_testnet_đã_chi / ratio). Mặc định 3 (FLOP_UNLOCK_RATIO);
    giá trị sai/không dương -> 3.

    NGUỒN / TRUNG THỰC: con số 3:1 lấy từ TEASER + bản nháp agent.html (intro.flop.network,
    27/08/2026). Yellow Paper (flop.finance/intro/yellowpaper/) KHÔNG xác nhận cơ chế này —
    Appendix E còn để ngỏ "liệu spend-to-unlock có ship" và "dạng sublinear của điểm quy đổi".
    Vì vậy ĐỪNG coi 3 là luật đã chốt; nó chỉ là mặc định có thể chỉnh, giữ ở đây để khi FLOP
    công bố công thức thật thì đổi 1 env (hoặc bỏ hẳn) mà không phải sửa logic."""
    raw = os.environ.get("FLOP_UNLOCK_RATIO", "").strip()
    d = _parse_amount(raw) if raw else None
    return d if (d is not None and d > 0) else Decimal(3)


def default_token() -> str:
    """Ký hiệu token mặc định (đổi qua FLOP_TOKEN_SYMBOL nếu cần)."""
    return os.environ.get("FLOP_TOKEN_SYMBOL", "").strip() or DEFAULT_TOKEN


def ledger_path(path: str = None) -> str:
    """Đường dẫn file sổ cái (mặc định token_ledger.json, testnet token_ledger_testnet.json;
    override TOKEN_LEDGER_FILE). Tách file theo chế độ để sổ mô phỏng còn nằm trên đĩa không
    bao giờ bị đọc như sổ testnet."""
    override = path or os.environ.get("TOKEN_LEDGER_FILE", "").strip()
    if override:
        return override
    return "token_ledger_testnet.json" if testnet_enabled() else "token_ledger.json"


# --- Số học thập phân CHÍNH XÁC (Decimal -> không trôi số float ở mức 0.001) ------

def _parse_amount(x) -> Decimal:
    """Ép về Decimal an toàn; trả None cho input rỗng/sai/NaN/vô cực (không raise)."""
    try:
        d = Decimal(str(x).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if d.is_nan() or d.is_infinite():
        return None
    return d


def _fmt(d: Decimal) -> str:
    """Decimal -> chuỗi thập phân gọn (fixed-point, bỏ số 0 thừa): 0.0010 -> '0.001'."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# --- Lưu trữ sổ cái (JSON: { balances: {token: "amount"}, entries: [...] }) --------

def _empty_state() -> dict:
    return {"balances": {}, "entries": []}


def load_ledger(path: str = None) -> dict:
    """Đọc toàn bộ sổ cái (state rỗng nếu file chưa tồn tại hoặc hỏng cấu trúc)."""
    p = ledger_path(path)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _empty_state()
    except Exception as e:                       # file hỏng: không làm sập agent
        print(f"[ledger] không đọc được {p} ({e}); coi như rỗng")
        return _empty_state()
    if not isinstance(data, dict) or not isinstance(data.get("balances"), dict) \
            or not isinstance(data.get("entries"), list):
        print(f"[ledger] cấu trúc {p} không nhận dạng được; coi như rỗng")
        return _empty_state()
    return data


def _save_ledger(state: dict, path: str = None) -> None:
    p = ledger_path(path)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ledger] không lưu được {p}: {e}")


def save_ledger(state: dict, path: str = None) -> None:
    """Ghi sổ cái ra đĩa — alias CÔNG KHAI của _save_ledger cho các module FLOP khác
    (flop_stake…) cùng dùng một token_ledger.json làm nguồn sự thật."""
    _save_ledger(state, path)


def check_balance(token: str = None, path: str = None) -> str:
    """Số dư hiện tại của token (mặc định FLOP) dạng chuỗi thập phân ('0' nếu chưa có)."""
    token = (token or default_token())
    return load_ledger(path).get("balances", {}).get(token, "0")


# --- Nạp (credit) — ghi nhận 1 lần faucet top-up ----------------------------------

def credit(amount, token: str = None, memo: str = "faucet credit", path: str = None) -> dict:
    """Cộng `amount` token vào sổ cái (ghi nhận nạp từ faucet). Kế toán thuần, không
    mạng ở cả 2 chế độ. Từ chối số không hợp lệ/không dương mà không ném lỗi. Việc
    tránh nạp trùng (idempotency) là trách nhiệm của caller (nạp 1 lần mỗi claim)."""
    token = (token or default_token())
    mode = ledger_mode()
    amt = _parse_amount(amount)
    if amt is None or amt <= 0:
        return {"ok": False, "token": token, "amount": "0", "mode": mode,
                "reason": f"credit amount phải là số dương (nhận {amount!r})"}

    state = load_ledger(path)
    cur = _parse_amount(state["balances"].get(token, "0")) or Decimal(0)
    new_bal = _fmt(cur + amt)
    state["balances"][token] = new_bal
    state["entries"].append({
        "kind": "credit", "token": token, "amount": _fmt(amt), "memo": memo,
        "mode": mode, "balance_after": new_bal,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    _accrue_credit_hour(state, token, amt)
    _save_ledger(state, path)
    return {"ok": True, "token": token, "amount": _fmt(amt), "mode": mode,
            "balance_after": new_bal, "reason": "credited"}


# --- Kế toán MỞ KHÓA MAINNET (mặc định 3:1 — GIẢ ĐỊNH TỪ TEASER, CHƯA CHỐT) --------
# Mỗi `unlock_ratio` FLOP chi THẬT trên testnet (spent_onchain) mở khóa 1 FLOP mainnet.
# LƯU Ý: tỉ lệ 3:1 là giả định từ teaser/agent.html; Yellow Paper chưa xác nhận (Appendix E
# để ngỏ). Xem docstring unlock_ratio(). Đây là hạch toán nội bộ, không phải cam kết của FLOP.
# Chi MÔ PHỎNG (spent_simulated) KHÔNG tính -> chống "farm" mở khóa bằng tiền giả.
# Đây chỉ là HẠCH TOÁN quyền; claim FLOP thật là hành động tài chính, đi qua seam
# gated claim_mainnet_unlock() (từ chối cho tới khi có cơ chế + endpoint thật).

def _unlock_state(state: dict) -> dict:
    u = state.setdefault("unlock", {})
    u.setdefault("spent_testnet", {})
    u.setdefault("claimed_mainnet", {})
    return u


HOUR_KEEP = 25               # đủ cho cửa sổ 24h trượt
DAY_KEEP = 7                 # giữ 7 ngày gần nhất (đủ cho pacer + báo cáo), chặn phình


SESSION_MEMO_PREFIX = "inference-session"   # chi cho MỘT phiên inference đã settle


def is_session_spend(memo) -> bool:
    """True nếu lần chi này là MUA COMPUTE trong một phiên đã settle.

    Vì sao phải tách: thước đo airdrop của agent là "compute purchased in settled
    sessions" — CHỈ cái này. Metering (ghi sổ phí một lần gọi LLM) KHÔNG được tính.
    Đo gộp hai thứ là tự đánh lừa mình suốt 90 ngày bằng con số sai."""
    return str(memo or "").startswith(SESSION_MEMO_PREFIX)


def _accrue_session(state: dict, token: str, amount: Decimal, now: int = None) -> None:
    """Tổng chạy RIÊNG cho chi tiêu phiên — thước đo thật, tách khỏi metering."""
    now = int(now if now is not None else time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    cm = state.setdefault("session_cumulative", {})
    cm[token] = _fmt((_parse_amount(cm.get(token, "0")) or Decimal(0)) + amount)
    cc = state.setdefault("session_count_cumulative", {})
    try:
        cc[token] = int(cc.get(token, 0)) + 1
    except (TypeError, ValueError):
        cc[token] = 1
    by = state.setdefault("session_by_day", {}).setdefault(token, {})
    by[day] = _fmt((_parse_amount(by.get(day, "0")) or Decimal(0)) + amount)
    for d in sorted(by)[:-DAY_KEEP]:
        by.pop(d, None)


def _accrue_day(state: dict, token: str, amount: Decimal, now: int = None) -> None:
    """Cộng dồn chi theo NGÀY UTC vào `spent_by_day`: {token: {'YYYY-MM-DD': tổng}}.

    Vì sao không đếm từ `entries`: bản đẩy lên KV phải cắt log cho vừa giới hạn 8192 ký
    tự của technocore.chat, nên entries KHÔNG còn là nguồn đáng tin để biết "hôm nay đã
    chi bao nhiêu". Tổng chạy thì cắt log bao nhiêu cũng không sai."""
    now = int(now if now is not None else time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    # Ngày chi ĐẦU TIÊN — đặt một lần, không bao giờ đổi. Cần nó để biết "đang ở ngày
    # thứ mấy của kỳ": spent_by_day chỉ giữ 7 ngày nên không suy ngược ra được.
    # KHÔNG dùng setdefault: bản publish trước đó đã ghi khoá này với giá trị None
    # (payload luôn kèm `first_spend_day`), mà setdefault chỉ đặt khi khoá VẮNG — nên
    # None sẽ nằm lại vĩnh viễn và không bao giờ đếm được "ngày thứ mấy".
    by = state.setdefault("spent_by_day", {})
    tok = by.setdefault(token, {})
    if not state.get("first_spend_day"):
        # TỰ CHỮA: lấy ngày SỚM NHẤT đã biết thay vì mặc định hôm nay, để sổ cái đã mất
        # khoá này (bản publish cũ ghi None) không bị gán nhầm ngày bắt đầu muộn hơn.
        known = sorted(d for d in tok if isinstance(d, str))
        state["first_spend_day"] = known[0] if known else day
    prev = _parse_amount(tok.get(day, "0")) or Decimal(0)
    tok[day] = _fmt(prev + amount)
    for d in sorted(tok)[:-DAY_KEEP]:        # chỉ giữ DAY_KEEP ngày gần nhất
        tok.pop(d, None)


def _accrue_cumulative(state: dict, token: str, amount: Decimal) -> None:
    """Cộng dồn MỌI lần chi (cả simulation lẫn testnet) vào `spent_cumulative`.

    Vì sao cần một đại lượng CHỈ TĂNG: khi so hai bản sổ cái (local vs KV) để chọn bản
    "đi xa hơn", không thể dùng |số dư| — ví được NẠP thì số dư dương và GIẢM dần khi
    chi, nên bản cũ lại có |số dư| lớn hơn và sẽ thắng, kéo sổ cái tua ngược mỗi lần
    hydrate. `spent_cumulative` chỉ tăng nên so sánh luôn đúng chiều.

    Khác `unlock.spent_testnet`: cái đó CHỈ cộng khi chi on-chain thật, nên bằng 0 ở
    simulation — không dùng để so bản được."""
    cm = state.setdefault("spent_cumulative", {})
    prev = _parse_amount(cm.get(token, "0")) or Decimal(0)
    cm[token] = _fmt(prev + amount)
    # Đếm số lần chi cũng phải là TỔNG CHẠY: bản publish cắt entries còn 20, nên đếm từ
    # log sẽ hụt hàng chục lần.
    cc = state.setdefault("spend_count_cumulative", {})
    try:
        cc[token] = int(cc.get(token, 0)) + 1
    except (TypeError, ValueError):
        cc[token] = 1
    # Đếm theo GIỜ UTC (giữ HOUR_KEEP giờ): đếm "trong 1h/24h qua" từ entries sẽ hụt khi
    # log bị cắt còn 20 bản ghi -> trần FLOP_MAX_SPENDS_PER_HOUR >20 vô tác dụng.
    by = state.setdefault("spend_count_by_hour", {})
    tok = by.get(token) if isinstance(by.get(token), dict) else {}
    hour = time.strftime("%Y-%m-%dT%H", time.gmtime(int(time.time())))
    try:
        tok[hour] = int(tok.get(hour, 0)) + 1
    except (TypeError, ValueError):
        tok[hour] = 1
    for h in sorted(tok)[:-HOUR_KEEP]:
        tok.pop(h, None)
    by[token] = tok


def _accrue_credit_hour(state: dict, token: str, amount: Decimal) -> None:
    """Nạp theo GIỜ UTC: {token: {'YYYY-MM-DDTHH': [tổng, số lần]}} (giữ HOUR_KEEP giờ). Báo
    cáo "nạp 24h" đếm từ entries thì hụt: bản KV chỉ giữ 20 entries, lần nạp vài giờ trước đã
    bị đẩy ra (báo 0 dù vừa nạp)."""
    by = state.setdefault("credit_by_hour", {})
    tok = by.get(token) if isinstance(by.get(token), dict) else {}
    hour = time.strftime("%Y-%m-%dT%H", time.gmtime(int(time.time())))
    cur = tok.get(hour) if isinstance(tok.get(hour), list) and len(tok.get(hour)) == 2 else ["0", 0]
    try:
        tok[hour] = [_fmt((_parse_amount(cur[0]) or Decimal(0)) + amount), int(cur[1]) + 1]
    except (TypeError, ValueError):
        tok[hour] = [_fmt(amount), 1]
    for h in sorted(tok)[:-HOUR_KEEP]:
        tok.pop(h, None)
    by[token] = tok


def cumulative_spent(state: dict, token: str = None) -> Decimal:
    """Tổng đã chi tích luỹ. `token=None` -> cộng mọi token (dùng để so hai bản sổ cái)."""
    cm = (state or {}).get("spent_cumulative")
    if not isinstance(cm, dict):
        return Decimal(0)
    if token is not None:
        return _parse_amount(cm.get(token, "0")) or Decimal(0)
    total = Decimal(0)
    for v in cm.values():
        total += _parse_amount(v) or Decimal(0)
    return total


def _accrue_unlock(state: dict, token: str, amount: Decimal) -> None:
    """Cộng dồn chi testnet THẬT vào quyền mở khóa. Gọi CHỈ khi spent_onchain."""
    u = _unlock_state(state)
    prev = _parse_amount(u["spent_testnet"].get(token, "0")) or Decimal(0)
    u["spent_testnet"][token] = _fmt(prev + amount)


def unlock_status(token: str = None, path: str = None) -> dict:
    """Tiến độ mở khóa mainnet cho token:
       spent_testnet    — tổng FLOP đã chi THẬT trên testnet
       unlocked_mainnet — = spent_testnet / ratio (quyền đã kiếm được)
       claimed_mainnet  — đã claim
       claimable        — còn claim được = max(unlocked - claimed, 0)."""
    token = (token or default_token())
    u = _unlock_state(load_ledger(path))
    ratio = unlock_ratio()
    spent = _parse_amount(u["spent_testnet"].get(token, "0")) or Decimal(0)
    claimed = _parse_amount(u["claimed_mainnet"].get(token, "0")) or Decimal(0)
    unlocked = spent / ratio
    claimable = unlocked - claimed
    return {
        "token": token, "ratio": _fmt(ratio),
        "spent_testnet": _fmt(spent), "unlocked_mainnet": _fmt(unlocked),
        "claimed_mainnet": _fmt(claimed),
        "claimable": _fmt(claimable if claimable > 0 else Decimal(0)),
    }


def claim_mainnet_unlock(amount=None, token: str = None, *, path: str = None,
                         claim_fn=None, claim_url: str = None,
                         private_key=None, did: str = None, log=print) -> dict:
    """CLAIM phần FLOP mainnet đã mở khóa. Đây là HÀNH ĐỘNG TÀI CHÍNH THẬT -> chỉ đi
    qua seam được tiêm `claim_fn` + endpoint tường minh (FLOP_MAINNET_CLAIM_URL).
    Thiếu một trong hai -> skipped_unconfigured (KHÔNG bao giờ bịa claim). Không có gì
    để claim -> skipped_nothing. Thành công -> tăng claimed_mainnet. `amount` None =
    claim toàn bộ phần claimable."""
    token = (token or default_token())
    st = unlock_status(token, path=path)
    claimable = _parse_amount(st["claimable"]) or Decimal(0)
    want = _parse_amount(amount) if amount is not None else claimable
    if want is None or want <= 0:
        return {**st, "outcome": "skipped_nothing", "reason": "không có phần mở khóa để claim"}
    if want > claimable:
        want = claimable

    url = (claim_url or os.environ.get("FLOP_MAINNET_CLAIM_URL", "").strip())
    if not url or claim_fn is None:
        return {**st, "outcome": "skipped_unconfigured",
                "reason": ("FLOP_MAINNET_CLAIM_URL trống — từ chối claim tới endpoint đoán mò"
                           if not url else
                           "chưa tiêm claim_fn — từ chối bịa giao dịch claim mainnet")}

    signed = sign_transaction(private_key, did, token, _fmt(want), "mainnet-unlock-claim")
    try:
        result = claim_fn({"token": token, "amount": _fmt(want), "claim_url": url, "signed": signed}) or {}
        tx_hash = result.get("tx_hash") or result.get("txHash")
    except Exception as e:
        return {**st, "outcome": "error_claim", "reason": f"claim mainnet thất bại: {e}"}

    state = load_ledger(path)
    u = _unlock_state(state)
    prev = _parse_amount(u["claimed_mainnet"].get(token, "0")) or Decimal(0)
    u["claimed_mainnet"][token] = _fmt(prev + want)
    _save_ledger(state, path)
    log(f"[unlock] claimed {_fmt(want)} {token} mainnet (tx {(tx_hash or '')[:12]}…)")
    return {**unlock_status(token, path=path), "outcome": "claimed_mainnet",
            "amount": _fmt(want), "tx_hash": tx_hash, "reason": "đã claim phần mở khóa mainnet"}


# --- Nối submit_tx + nạp khóa agent (lazy, có cache) ------------------------------

def _resolve_submit_tx():
    """Tự dựng adapter submit_tx từ flop_tx theo env (FLOP_TX_MODE/FLOP_SUBMIT_URL).
    None nếu chưa cấu hình -> spend() báo skipped_unconfigured. Không làm sập nếu
    flop_tx thiếu."""
    try:
        import flop_tx
        return flop_tx.build_submit_tx()
    except Exception as e:
        print(f"[ledger] không dựng được submit_tx ({e})")
        return None


_key_cache = None


def _agent_key():
    """(private_key, did) của agent từ AGENT_PRIVATE_KEY, cache lại. (None, None) nếu
    không có khóa/SDK — spend vẫn chạy ở simulation, chỉ là payload không được ký."""
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    if load_private_key is None or did_of is None:
        _key_cache = (None, None)
        return _key_cache
    try:
        pk = load_private_key()
        _key_cache = (pk, did_of(pk))
    except Exception:
        _key_cache = (None, None)
    return _key_cache


# --- Ký giao dịch (Ed25519 THẬT — canonical: did|token|amount|nonce|memo) ----------

_last_nonce = 0


def next_nonce() -> str:
    """Nonce mili-giây TĂNG NGHIÊM NGẶT trong tiến trình: nhiều phiên/giao dịch trong cùng
    một mili-giây (vd 3 phiên phí cố định/run) trước đây ra CÙNG nonce -> cùng id yêu cầu,
    và mạng coi lần 2, 3 là gửi lại (replay). Giữa các run, đồng hồ đã tự tăng."""
    global _last_nonce
    _last_nonce = max(int(time.time() * 1000), _last_nonce + 1)
    return str(_last_nonce)


def sign_transaction(private_key, did: str, token: str, amount: str, memo: str,
                     nonce: str = None) -> dict:
    """Ký 1 'giao dịch' token bằng Ed25519 (chữ ký THẬT, dùng lại sign_message của
    SDK). KHÔNG gửi đi đâu — gửi là việc của submit_tx ở chế độ testnet. Trả về
    payload đã ký (proof-of-intent), hoặc None nếu SDK ký không khả dụng."""
    if sign_message is None or private_key is None or did is None:
        return None
    nonce = nonce or next_nonce()
    canonical = f"{did}|{token}|{amount}|{nonce}|{memo}"
    return {
        "did": did, "token": token, "amount": amount, "nonce": nonce,
        "memo": memo, "sig": sign_message(private_key, canonical),
    }


# --- Chi tiêu (spend) — HÀNH ĐỘNG có kiểm soát ------------------------------------

def spend(amount, memo, token: str = None, *, path: str = None,
          submit_tx=None, rpc: str = None, private_key=None, did: str = None,
          log=print) -> dict:
    """Chi `amount` token kèm `memo`. Luồng (mỗi nhánh là 1 kết quả có ghi nhận,
    KHÔNG ném lỗi):
      1. skipped_insufficient — amount không hợp lệ/không dương, HOẶC vượt số dư;
      2. simulation           — trừ số dư MOCK, ghi entry, và log:
                                   [SIMULATION] Spent <amount> MOCK_<TOKEN> for <memo>
      3. testnet              — cần FLOP_RPC_URL + submit_tx được tiêm (nếu thiếu ->
                                skipped_unconfigured), gửi giao dịch thật rồi trừ số dư.
    Chữ ký Ed25519 (nếu có private_key+did) được đính kèm ở CẢ hai chế độ như bằng
    chứng ý định. `submit_tx(tx: dict) -> {"tx_hash": ...}` là seam được tiêm vào."""
    token = (token or default_token())
    mode = ledger_mode()
    amt = _parse_amount(amount)
    base = {"token": token, "memo": memo, "mode": mode,
            "amount": _fmt(amt) if amt is not None else "0"}

    if amt is None or amt <= 0:
        return {**base, "outcome": "skipped_insufficient",
                "reason": f"spend amount phải là số dương (nhận {amount!r})"}

    state = load_ledger(path)
    cur = _parse_amount(state["balances"].get(token, "0")) or Decimal(0)
    if cur < amt:
        return {**base, "outcome": "skipped_insufficient",
                "reason": f"không đủ {token}: số dư {_fmt(cur)} < {_fmt(amt)}"}

    # Ký giao dịch (thật) nếu có khóa — bằng chứng ý định ở cả 2 chế độ.
    signed = sign_transaction(private_key, did, token, _fmt(amt), memo)

    # --- TESTNET: chỉ gửi thật qua seam được tiêm (hoặc tự dựng từ flop_tx) ---
    if mode == "testnet":
        url = (rpc or endpoint_url())                # RPC hoặc relayer (FLOP_SUBMIT_URL)
        submit = submit_tx or _resolve_submit_tx()   # tự nối adapter khi không tiêm tay
        if not url or submit is None:
            return {**base, "outcome": "skipped_unconfigured", "signed": signed,
                    "reason": ("TESTNET_ENABLED=true nhưng FLOP_RPC_URL/FLOP_SUBMIT_URL trống "
                               "— từ chối gửi tới endpoint đoán mò") if not url else
                              ("TESTNET_ENABLED=true nhưng chưa có submit_tx (đặt FLOP_TX_MODE/"
                               "FLOP_SUBMIT_URL hoặc tiêm tay) — từ chối bịa giao dịch")}
        try:
            result = submit({"token": token, "amount": _fmt(amt), "memo": memo,
                             "rpc_url": url, "signed": signed}) or {}
            tx_hash = result.get("tx_hash") or result.get("txHash")
        except Exception as e:
            return {**base, "outcome": "error_submit",
                    "reason": f"gửi giao dịch testnet thất bại: {e}"}
        if not tx_hash:
            # Không có tx hash = không có bằng chứng giao dịch đã lên chain -> KHÔNG trừ sổ,
            # KHÔNG tính là chi thật (trước đây adapter tiêm tay trả {} vẫn thành spent_onchain).
            return {**base, "outcome": "error_submit", "signed": signed,
                    "reason": f"submit_tx không trả tx hash (keys: {sorted(result)[:10]})"}
        new_bal = _fmt(cur - amt)
        state["balances"][token] = new_bal
        state["entries"].append({
            "kind": "spend", "token": token, "amount": _fmt(amt), "memo": memo,
            "mode": mode, "balance_after": new_bal, "tx_hash": tx_hash,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        _accrue_unlock(state, token, amt)     # chi testnet THẬT -> tăng quyền mở khóa 3:1
        _accrue_cumulative(state, token, amt)  # mốc CHỈ TĂNG -> hydrate chọn đúng bản
        _accrue_day(state, token, amt)         # tổng theo ngày -> pacer không cần entries
        if is_session_spend(memo):             # thước đo airdrop THẬT
            _accrue_session(state, token, amt)
        _save_ledger(state, path)
        short = (tx_hash or "")[:12]
        log(f"[ledger] spent {_fmt(amt)} {token} for {memo} (tx {short}…)")
        return {**base, "outcome": "spent_onchain", "balance_after": new_bal,
                "tx_hash": tx_hash, "signed": signed, "reason": "đã gửi giao dịch on-chain"}

    # --- SIMULATION (mặc định): trừ số dư MOCK + dòng log [SIMULATION] trung thực ---
    new_bal = _fmt(cur - amt)
    state["balances"][token] = new_bal
    state["entries"].append({
        "kind": "spend", "token": token, "amount": _fmt(amt), "memo": memo,
        "mode": mode, "balance_after": new_bal,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    _accrue_cumulative(state, token, amt)      # mốc CHỈ TĂNG -> hydrate chọn đúng bản
    _accrue_day(state, token, amt)             # tổng theo ngày -> pacer không cần entries
    if is_session_spend(memo):                 # thước đo airdrop THẬT
        _accrue_session(state, token, amt)
    _save_ledger(state, path)
    log(f"[SIMULATION] Spent {_fmt(amt)} MOCK_{token} for {memo}")
    return {**base, "outcome": "spent_simulated", "balance_after": new_bal,
            "signed": signed, "reason": "chi tiêu mô phỏng (không đụng chain)"}


GATEWAY_REFS_KEEP = 100      # gateway trả tối đa ~20 biên nhận/lần -> 100 là dư


def record_gateway_spend(amount, memo, ref: str, token: str = None, *, path: str = None,
                         log=print) -> dict:
    """Ghi sổ một lần chi ĐÃ XẢY RA ở gateway FLOP (api.flop.network trừ quota; `ref` = id
    phản hồi làm bằng chứng). Khác spend(): agent không tự gửi giao dịch — gateway đã chi —
    nên chỉ ghi nhận. CHỈ ở chế độ testnet: devnet/mô phỏng không bao giờ vào sổ thật.
    Số dư kẹp ở 0 (quota gateway có thể không trùng số dư faucet trong sổ)."""
    token = (token or default_token())
    amt = _parse_amount(amount)
    if ledger_mode() != "testnet":
        return {"outcome": "skipped_simulation", "reason": "chỉ ghi sổ chi gateway khi TESTNET_ENABLED"}
    if amt is None or amt <= 0 or not ref:
        return {"outcome": "skipped_insufficient", "reason": f"thiếu số lượng/bằng chứng ({amount!r}, {ref!r})"}
    state = load_ledger(path)
    # Chống ghi TRÙNG ngay trong sổ (bền qua KV): danh sách "biên nhận đã ghi" của gateway nằm
    # trong state.json (Actions cache) — cache mất thì lượt sau thấy lại tới 20 biên nhận cũ và
    # ghi lần hai, cộng VĨNH VIỄN vào tổng chi (chỉ tăng). Giữ dấu băm của GATEWAY_REFS_KEEP ref.
    h = hashlib.sha256(str(ref).encode("utf-8")).hexdigest()[:16]
    refs = [x for x in (state.get("gateway_refs") or []) if isinstance(x, str)]
    if h in refs:
        return {"outcome": "duplicate", "reason": f"ref {str(ref)[:24]} đã ghi sổ trước đó"}
    state["gateway_refs"] = (refs + [h])[-GATEWAY_REFS_KEEP:]
    cur = _parse_amount(state["balances"].get(token, "0")) or Decimal(0)
    new_bal = _fmt(max(Decimal(0), cur - amt))
    state["balances"][token] = new_bal
    state["entries"].append({
        "kind": "spend", "token": token, "amount": _fmt(amt), "memo": memo, "mode": "testnet",
        "balance_after": new_bal, "tx_hash": str(ref)[:80], "via": "gateway",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    _accrue_unlock(state, token, amt)
    _accrue_cumulative(state, token, amt)
    _accrue_day(state, token, amt)
    if is_session_spend(memo):
        _accrue_session(state, token, amt)
    _save_ledger(state, path)
    log(f"[ledger] gateway spent {_fmt(amt)} {token} for {memo} (ref {str(ref)[:12]}…)")
    return {"outcome": "spent_onchain", "amount": _fmt(amt), "balance_after": new_bal, "tx_hash": ref}


# --- Envelope chống sybil + thống kê hoạt động (đọc từ chính sổ cái) ---------------

def _parse_ts(s) -> int:
    """ISO UTC '%Y-%m-%dT%H:%M:%SZ' -> epoch giây. None nếu không parse được (không raise)."""
    try:
        return calendar.timegm(time.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def max_spends_per_hour():
    """Trần số lần CHI mỗi giờ trượt (envelope chống sybil, độc lập với pacer). Đếm từ
    entries của sổ cái nên đúng cả khi pacer tắt. Rỗng/không hợp lệ/không dương -> None."""
    raw = os.environ.get("FLOP_MAX_SPENDS_PER_HOUR", "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if v > 0 else None


def _recent_spend_count(token: str, now: int, window: int = 3600, path: str = None) -> int:
    """Số lần chi `token` trong `window` giây gần đây (mặc định 1 giờ).

    Entries phủ HẾT cửa sổ (có bản ghi cũ hơn mốc cắt) -> đếm chính xác từ entries. Ngược
    lại log có thể đã bị cắt (KV chỉ giữ 20 bản ghi) -> dùng bộ đếm theo giờ, tính cả giờ
    chứa mốc cắt nên là CẬN TRÊN (an toàn cho trần tần suất: thà chặn sớm hơn đếm hụt)."""
    cutoff = now - window
    led = load_ledger(path)
    n, covered = 0, False
    for e in led.get("entries", []):
        if not isinstance(e, dict) or e.get("kind") != "spend" or e.get("token") != token:
            continue
        ts = _parse_ts(e.get("ts"))
        if ts is None:
            continue
        if ts >= cutoff:
            n += 1
        else:
            covered = True
    if covered:
        return n
    by = led.get("spend_count_by_hour")
    tok = by.get(token) if isinstance(by, dict) else None
    if not isinstance(tok, dict):
        return n
    start = cutoff - cutoff % 3600
    est = 0
    for h, v in tok.items():
        try:
            if calendar.timegm(time.strptime(h, "%Y-%m-%dT%H")) >= start:
                est += int(v)
        except (TypeError, ValueError):
            continue
    return max(n, est)


def available_today(token: str = None, now: int = None, path: str = None) -> str:
    """Tổng token KHẢ DỤNG trong ngày UTC hiện tại = số dư còn lại + ĐÃ chi trong ngày.

    Dùng cho FLOP_DAILY_BUDGET=auto. Vì sao cộng cả phần đã chi: nếu ngân sách chỉ bằng
    số dư hiện tại thì mỗi lần chi lại kéo ngân sách xuống, mục tiêu tụt theo và ví
    KHÔNG BAO GIỜ tiêu hết (tiệm cận 0). Cộng phần đã chi làm ngân sách ĐỨNG YÊN trong
    ngày, và chỉ tăng khi faucet nạp thêm — đúng ý "hôm nay có bao nhiêu, tiêu bấy nhiêu".

    Chỉ tính entry 'spend' có ts nằm trong NGÀY UTC hiện tại; entry thiếu/hỏng ts bị bỏ
    qua (không đoán thời điểm)."""
    token = (token or default_token())
    bal = _parse_amount(check_balance(token, path)) or Decimal(0)
    return _fmt(max(Decimal(0), bal) + spent_today_amount(token, now, path))


def spent_today_amount(token: str = None, now: int = None, path: str = None) -> Decimal:
    """Tổng đã CHI trong NGÀY UTC hiện tại, đọc từ SỔ CÁI.

    Vì sao đọc từ sổ cái: nhịp chi của pacer nằm trong `flop_pacer.json`, mà file đó
    KHÔNG được cache qua run (workflow chỉ cache `state.json`) — nên mỗi run pacer tưởng
    hôm nay chưa chi gì và không bao giờ hãm lại. Sổ cái thì bền qua KV, nên nó mới là
    nguồn đúng để biết "hôm nay đã chi bao nhiêu".

    Entry thiếu/hỏng `ts` bị bỏ qua (không đoán thời điểm)."""
    token = (token or default_token())
    now = int(now if now is not None else time.time())
    led = load_ledger(path)
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    by = (led.get("spent_by_day") or {}).get(token) if isinstance(led.get("spent_by_day"), dict) else None
    if isinstance(by, dict) and day in by:
        return _parse_amount(by.get(day)) or Decimal(0)   # tổng chạy: chính xác kể cả khi cắt log
    # Đường lui cho sổ cái CŨ (chưa có spent_by_day): cộng từ entries.
    start = now - (now % 86400)                      # 00:00 UTC hôm nay
    spent = Decimal(0)
    for e in led.get("entries", []):
        if not isinstance(e, dict) or e.get("kind") != "spend" or e.get("token") != token:
            continue
        ts = _parse_ts(e.get("ts"))
        if ts is None or ts < start:
            continue
        spent += _parse_amount(e.get("amount")) or Decimal(0)
    return spent


def spend_stats(token: str = None, now: int = None, path: str = None) -> dict:
    """Ảnh chụp hoạt động chi (để audit/telemetry chống sybil): tổng số lần chi và số
    lần chi trong 24h gần đây. Auditor đối chiếu chuỗi này với số lượt trả lời thật
    theo thời gian -> farm sẽ lộ (chi tách rời hoạt động). Thuần, đọc từ sổ cái."""
    token = (token or default_token())
    now = int(now if now is not None else time.time())
    led = load_ledger(path)
    entries = led.get("entries", [])
    total = sum(1 for e in entries if e.get("kind") == "spend" and e.get("token") == token)
    cc = led.get("spend_count_cumulative")
    try:                                     # tổng chạy thắng: entries đã bị cắt còn 20
        total = max(total, int((cc or {}).get(token, 0)))
    except (TypeError, ValueError, AttributeError):
        pass
    last_24h = _recent_spend_count(token, now, 86400, path)
    return {"token": token, "spend_count_total": total, "spend_count_24h": last_24h}


# --- Hạt giống MÔ PHỎNG: để chạy thử đường ống khi faucet thật chưa tồn tại --------

def maybe_sim_credit(path: str = None, log=print, now: int = None) -> dict:
    """(GATED, CHỈ simulation) Nạp một hạt giống MÔ PHỎNG khi số dư = 0.

    Vì sao cần: faucet THẬT chưa có spec (FLOP chưa công bố), nên số dư luôn 0 và MỌI
    lần metering trả skipped_insufficient -> không có cách nào chạy thử chuỗi
    chi -> pacer -> báo cáo trước ngày testnet mở. Hạt giống này lấp đúng chỗ đó.

    BA chốt chặn, theo thứ tự:
      1. Phải đặt FLOP_SIM_CREDIT (mặc định rỗng -> tắt hoàn toàn).
      2. TỪ CHỐI CỨNG khi testnet_enabled() — hạt giống mô phỏng KHÔNG BAO GIỜ
         được chạm vào sổ cái testnet thật, kể cả khi ai đó quên tắt cờ.
      3. Chỉ nạp khi số dư <= 0 -> không chồng thêm vào ví đang có tiền.

    memo ghi RÕ đây là hạt giống mô phỏng: sổ cái được publish công khai, nên một dòng
    credit không nhãn sẽ bị đọc thành nạp thật."""
    raw = os.environ.get("FLOP_SIM_CREDIT", "").strip()
    if not raw:
        return {"outcome": "skipped_disabled", "reason": "FLOP_SIM_CREDIT chưa đặt"}
    if testnet_enabled():
        return {"outcome": "skipped_testnet",
                "reason": "TESTNET_ENABLED bật — từ chối nạp hạt giống mô phỏng"}
    amt = _parse_amount(raw)
    if amt is None or amt <= 0:
        return {"outcome": "skipped_bad_amount", "reason": f"FLOP_SIM_CREDIT không hợp lệ: {raw!r}"}
    token = default_token()
    cur = _parse_amount(check_balance(token, path)) or Decimal(0)
    # Nạp khi số dư TỤT DƯỚI NGƯỠNG, không phải khi bằng đúng 0. Qua mốc nửa đêm ví còn
    # 0.205 FLOP bụi — "> 0" nên không bao giờ nạp lại, và ngân sách ngày mới teo theo
    # (budget = số dư + đã chi hôm nay). Mặc định ngưỡng = 1% lượng nạp.
    below = _parse_amount(os.environ.get("FLOP_SIM_CREDIT_BELOW", "").strip())
    if below is None or below < 0:
        below = amt / Decimal(100)
    if cur > below:
        return {"outcome": "skipped_has_balance",
                "reason": f"số dư {_fmt(cur)} > ngưỡng {_fmt(below)}"}
    # TRẦN MỖI NGÀY — faucet THẬT có FLOP_FAUCET_MAX_PER_DAY, hạt giống mô phỏng thì
    # không, nên pacer rút cạn ví rồi nạp lại ngay TRONG ngày: đo được 2 lần nạp/ngày,
    # tổng chi 115 thay vì 100. Ngân sách `auto` = số dư + đã chi hôm nay, nên mỗi lần
    # nạp lại NÂNG ngân sách của chính ngày đó -> chi trong ngày không còn trần.
    day = time.strftime("%Y-%m-%d", time.gmtime(int(now if now is not None else time.time())))
    cap = _parse_amount(os.environ.get("FLOP_SIM_CREDIT_MAX_PER_DAY", "").strip())
    cap = 1 if (cap is None or cap < 0) else int(cap)
    st = load_ledger(path).get("sim_credit_count") or {}
    used = 0
    if isinstance(st, dict) and st.get("day") == day:
        try:
            used = int(st.get("n") or 0)
        except (TypeError, ValueError):
            used = 0
    if cap and used >= cap:
        return {"outcome": "skipped_daily_cap",
                "reason": f"đã nạp {used} lần hôm nay (trần {cap})"}
    res = credit(amt, token=token, memo="SIMULATION seed (khong phai faucet that)", path=path)
    if res.get("ok"):
        led = load_ledger(path)                      # ghi mốc SAU khi credit đã lưu
        led["sim_credit_count"] = {"day": day, "n": used + 1}
        _save_ledger(led, path)
        log(f"[sim-credit] nạp hạt giống MÔ PHỎNG {_fmt(amt)} {token} "
            f"(sổ cái mode=simulation, không phải token thật)")
        return {"outcome": "credited", "amount": _fmt(amt), "token": token}
    return {"outcome": "error_credit", "reason": str(res)}


# --- Tiện ích: đo phí 1 lần suy luận LLM (ví dụ dùng trong agent, GATED) -----------

def meter_inference(amount: str = None, memo: str = "Gemini Inference",
                    token: str = None, path: str = None, event_id: str = None,
                    now: int = None, **kw) -> dict:
    """Ghi nhận 'trả token cho 1 lần suy luận LLM'. Chỉ chạy khi FLOP_METER_ENABLED
    bật (mặc định TẮT -> agent 24/7 không đổi hành vi). Số tiền lấy từ
    FLOP_INFERENCE_COST (mặc định 0.001). Bọc kín: mọi lỗi -> {'outcome':'skipped_off'}
    để không bao giờ làm sập luồng trả lời của agent.

    Bất biến CHỐNG SYBIL (mặc định BẬT, xem organic_only()): khi FLOP_ORGANIC_ONLY bật, chỉ chi khi có
    `event_id` — dấu vết một sự kiện THẬT (tin nhắn user @mention) đã kích hoạt lần
    suy luận này. Thiếu event_id -> skipped_synthetic. Đây là bảo hiểm để KHÔNG ai
    (kể cả cấu hình sai) nối được một vòng lặp tự-gọi đốt token không gắn hoạt động
    thật — thứ mà sybil filter lọc thẳng.

    Envelope CHỐNG SYBIL (tùy chọn): FLOP_MAX_SPENDS_PER_HOUR đặt trần số lần chi mỗi
    giờ trượt (đếm từ sổ cái) — dù cấu hình sai cũng không spike lên tần suất bot.
    Chạm trần -> skipped_rate_cap (không chi)."""
    if not _env_flag("FLOP_METER_ENABLED"):
        return {"outcome": "skipped_off", "reason": "FLOP_METER_ENABLED tắt"}
    if testnet_enabled():
        # Metering là PROXY mô phỏng (trừ FLOP cho mỗi lần gọi LLM bên ngoài). Ở testnet,
        # spend() là giao dịch ON-CHAIN thật: memo "gemini kibble" sẽ đốt FLOP thật mà
        # không được tính (chỉ phiên đã settle mới tính), và giành ngân sách của phiên.
        return {"outcome": "skipped_testnet",
                "reason": "testnet: metering proxy không chi on-chain — chi thật chỉ qua phiên"}
    if organic_only() and not (event_id and str(event_id).strip()):
        return {"outcome": "skipped_synthetic",
                "reason": ("FLOP_ORGANIC_ONLY: từ chối chi cho inference không gắn sự kiện "
                           "thật (thiếu event_id) — chống burn-loop tổng hợp")}
    token = (token or default_token())

    # Envelope tần suất: chặn trước khi chi nếu đã chạm trần lần chi/giờ.
    cap = max_spends_per_hour()
    if cap is not None:
        nowv = int(now if now is not None else time.time())
        recent = _recent_spend_count(token, nowv, 3600, path)
        if recent >= cap:
            return {"outcome": "skipped_rate_cap",
                    "reason": f"đã chi {recent}/{cap} lần trong giờ qua — chạm trần tần suất"}

    # Dynamic Spend Rate: nếu có FLOP_DAILY_BUDGET, để pacer quyết lượng chi (rải đều,
    # chống dump). Trả None -> pacer tắt -> dùng phí cố định. "0" -> chưa tới nhịp.
    if amount is None:
        try:
            import flop_pacer
            # `path` ở đây là SỔ CÁI, không phải file nhịp của pacer -> truyền đúng chỗ,
            # nếu không pacer sẽ ghi trạng thái đè lên file sổ cái.
            # Truyền ĐƯỜNG DẪN ĐÃ GIẢI (không phải None): pacer chỉ đọc sổ cái khi được
            # nêu rõ, nên phải nói rõ là sổ cái NÀO.
            paced = flop_pacer.next_spend_amount(token=token,
                                                 ledger_path=ledger_path(path))
        except Exception as e:
            print(f"[meter] pacer lỗi ({e}), dùng phí cố định")
            paced = None
        if paced is not None:
            p = _parse_amount(paced)
            if p is None or p <= 0:
                return {"outcome": "skipped_paced",
                        "reason": "chưa tới nhịp chi theo lịch rải đều (giữ để không dump)"}
            amount = paced

    amt = amount or os.environ.get("FLOP_INFERENCE_COST", "").strip() or "0.001"
    # Ở testnet, payload cần chữ ký -> tự nạp khóa agent nếu caller không truyền.
    if kw.get("private_key") is None and kw.get("did") is None:
        pk, did = _agent_key()
        if pk is not None:
            kw["private_key"], kw["did"] = pk, did
    try:
        result = spend(amt, memo, token=token, path=path, **kw)
    except Exception as e:                       # tuyệt đối không làm sập caller
        return {"outcome": "error_meter", "reason": str(e)}
    # Chi OK -> báo pacer trừ vào ngân sách ngày (không làm sập nếu pacer lỗi).
    if result.get("outcome") in ("spent_simulated", "spent_onchain"):
        try:
            import flop_pacer
            flop_pacer.record_spend(result.get("amount", amt), token=token)
        except Exception:
            pass
    return result


# --- Demo offline (không key, không mạng) -----------------------------------------

def _demo() -> None:
    import tempfile
    path = os.path.join(tempfile.mkdtemp(prefix="flop-ledger-demo-"), "ledger.json")

    # Khóa/DID là TÙY CHỌN cho demo — nếu có AGENT_PRIVATE_KEY hợp lệ thì ký thật.
    pk = did = None
    if load_private_key is not None:
        try:
            pk = load_private_key()
            did = did_of(pk)
        except Exception:
            pk = did = None

    print("— FLOP token ledger (demo offline) —")
    print(f"mode  : {ledger_mode()} (TESTNET_ENABLED chưa bật — đặt 'true' để chuyển testnet thật)")
    c = credit("100", memo="faucet credit", path=path)
    print(f"credit: {c['amount']} {c['token']} -> số dư {c.get('balance_after')}")
    s = spend("0.001", "Gemini Inference", path=path, private_key=pk, did=did)
    print(f"spend : {s['outcome']} · số dư còn {s.get('balance_after')} "
          f"· {'đã ký Ed25519' if s.get('signed') else 'không ký (chưa có khóa)'}")

    # Mở khóa mainnet 3:1 — simulation KHÔNG tích lũy (chống farm bằng tiền giả);
    # chỉ chi testnet THẬT mới cộng. Minh họa bằng 1 submit_tx GIẢ (không mạng).
    us = unlock_status(path=path)
    print(f"unlock: sau chi mô phỏng -> spent_testnet={us['spent_testnet']} "
          f"(đúng: mô phỏng không tính) · ratio 1/{us['ratio']}")
    prev = os.environ.get("TESTNET_ENABLED")
    os.environ["TESTNET_ENABLED"] = "true"
    fake_submit = lambda tx: {"tx_hash": "0xDEMOtxhash0001"}
    for _ in range(3):     # chi 3 x 3 = 9 FLOP testnet "thật" -> mở khóa 9/3 = 3
        spend("3", "demo testnet spend", path=path, submit_tx=fake_submit,
              rpc="https://rpc.demo.invalid", private_key=pk, did=did, log=lambda m: None)
    if prev is None:
        del os.environ["TESTNET_ENABLED"]
    else:
        os.environ["TESTNET_ENABLED"] = prev
    us = unlock_status(path=path)
    print(f"unlock: sau chi 9 FLOP testnet thật -> spent={us['spent_testnet']} "
          f"· unlocked_mainnet={us['unlocked_mainnet']} · claimable={us['claimable']}")

    print("\nPhạm vi trung thực: simulation KHÔNG lên chain và KHÔNG tích lũy mở khóa. "
          "Chỉ TESTNET_ENABLED=true + FLOP_RPC_URL + submit_tx thật mới chi thật & tích lũy. "
          "Claim FLOP mainnet là hành động tài chính -> qua seam gated claim_mainnet_unlock().")


if __name__ == "__main__":
    _demo()
