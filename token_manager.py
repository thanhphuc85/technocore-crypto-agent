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
import json
import time
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


def default_token() -> str:
    """Ký hiệu token mặc định (đổi qua FLOP_TOKEN_SYMBOL nếu cần)."""
    return os.environ.get("FLOP_TOKEN_SYMBOL", "").strip() or DEFAULT_TOKEN


def ledger_path(path: str = None) -> str:
    """Đường dẫn file sổ cái (mặc định token_ledger.json; override TOKEN_LEDGER_FILE)."""
    return path or os.environ.get("TOKEN_LEDGER_FILE", "").strip() or "token_ledger.json"


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
        with open(p, "r") as f:
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
        with open(p, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ledger] không lưu được {p}: {e}")


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
    _save_ledger(state, path)
    return {"ok": True, "token": token, "amount": _fmt(amt), "mode": mode,
            "balance_after": new_bal, "reason": "credited"}


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

def sign_transaction(private_key, did: str, token: str, amount: str, memo: str,
                     nonce: str = None) -> dict:
    """Ký 1 'giao dịch' token bằng Ed25519 (chữ ký THẬT, dùng lại sign_message của
    SDK). KHÔNG gửi đi đâu — gửi là việc của submit_tx ở chế độ testnet. Trả về
    payload đã ký (proof-of-intent), hoặc None nếu SDK ký không khả dụng."""
    if sign_message is None or private_key is None or did is None:
        return None
    nonce = nonce or str(int(time.time() * 1000))
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
        new_bal = _fmt(cur - amt)
        state["balances"][token] = new_bal
        state["entries"].append({
            "kind": "spend", "token": token, "amount": _fmt(amt), "memo": memo,
            "mode": mode, "balance_after": new_bal, "tx_hash": tx_hash,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
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
    _save_ledger(state, path)
    log(f"[SIMULATION] Spent {_fmt(amt)} MOCK_{token} for {memo}")
    return {**base, "outcome": "spent_simulated", "balance_after": new_bal,
            "signed": signed, "reason": "chi tiêu mô phỏng (không đụng chain)"}


# --- Tiện ích: đo phí 1 lần suy luận LLM (ví dụ dùng trong agent, GATED) -----------

def meter_inference(amount: str = None, memo: str = "Gemini Inference",
                    token: str = None, path: str = None, **kw) -> dict:
    """Ghi nhận 'trả token cho 1 lần suy luận LLM'. Chỉ chạy khi FLOP_METER_ENABLED
    bật (mặc định TẮT -> agent 24/7 không đổi hành vi). Số tiền lấy từ
    FLOP_INFERENCE_COST (mặc định 0.001). Bọc kín: mọi lỗi -> {'outcome':'skipped_off'}
    để không bao giờ làm sập luồng trả lời của agent."""
    if not _env_flag("FLOP_METER_ENABLED"):
        return {"outcome": "skipped_off", "reason": "FLOP_METER_ENABLED tắt"}
    amt = amount or os.environ.get("FLOP_INFERENCE_COST", "").strip() or "0.001"
    # Ở testnet, payload cần chữ ký -> tự nạp khóa agent nếu caller không truyền.
    if kw.get("private_key") is None and kw.get("did") is None:
        pk, did = _agent_key()
        if pk is not None:
            kw["private_key"], kw["did"] = pk, did
    try:
        return spend(amt, memo, token=token, path=path, **kw)
    except Exception as e:                       # tuyệt đối không làm sập caller
        return {"outcome": "error_meter", "reason": str(e)}


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
    print("\nPhạm vi trung thực: ở simulation KHÔNG có gì lên chain. Chỉ khi "
          "TESTNET_ENABLED=true + FLOP_RPC_URL + submit_tx được tiêm thì spend mới gửi thật.")


if __name__ == "__main__":
    _demo()
