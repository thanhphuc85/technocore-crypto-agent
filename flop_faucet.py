"""
flop_faucet.py — "Auto-Cycle Faucet" (scaffold, GATED). Kiểm số dư testnet và khi tới
lượt claim, gọi `claim_fn` được tiêm để nhận FLOP testnet rồi credit vào sổ cái.

Vì spec faucet của FLOP CHƯA công bố, module này KHÔNG POST tới endpoint đoán mò:
thiếu cờ/URL/seam thì trả skipped_* và không gửi gì (đúng ethos phần còn lại của repo).
Đến ngày FLOP mở faucet: bật FLOP_FAUCET_ENABLED, đặt FLOP_FAUCET_URL, và tiêm `claim_fn`
hiện thực theo scheme thật của họ (ký DID -> POST/GET faucet -> trả {"amount":..}).

Cặp đôi với flop_pacer: faucet NẠP đều token vào ví, pacer CHI đều token ra 24/7 ->
tối đa hóa "Tổng testnet đã chi" (tử số của công thức mở khóa mainnet 3:1) một cách
hợp lệ, không dump.

Env:
  FLOP_FAUCET_ENABLED       bật (mặc định tắt -> skipped_disabled).
  FLOP_FAUCET_URL           endpoint faucet (điền khi FLOP công bố). Rỗng -> unconfigured.
  FLOP_FAUCET_AMOUNT        lượng dự kiến mỗi claim (mặc định 100).
  FLOP_FAUCET_COOLDOWN_HOURS khoảng tối thiểu giữa 2 claim (mặc định 24).
  FLOP_FAUCET_REFILL_BELOW  (tùy chọn) chỉ claim khi số dư testnet < ngưỡng này.
  FLOP_FAUCET_STATE         nơi lưu mốc claim gần nhất (mặc định flop_faucet.json).
"""

import os
import json
import time

import token_manager as tm


def _flag(n: str) -> bool:
    return os.environ.get(n, "").strip().lower() in ("1", "true", "on", "yes")


def faucet_enabled() -> bool:
    return _flag("FLOP_FAUCET_ENABLED")


def faucet_url() -> str:
    return os.environ.get("FLOP_FAUCET_URL", "").strip()


def faucet_amount() -> str:
    return os.environ.get("FLOP_FAUCET_AMOUNT", "").strip() or "100"


def cooldown_hours() -> float:
    try:
        v = float(os.environ.get("FLOP_FAUCET_COOLDOWN_HOURS", "").strip() or 24)
        return v if v >= 0 else 24.0
    except ValueError:
        return 24.0


def state_path(path: str = None) -> str:
    return path or os.environ.get("FLOP_FAUCET_STATE", "").strip() or "flop_faucet.json"


def _load(path: str = None) -> dict:
    try:
        with open(state_path(path), "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[faucet] không đọc được state ({e}); coi như rỗng")
        return {}


def _save(state: dict, path: str = None) -> None:
    try:
        with open(state_path(path), "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[faucet] không lưu được state: {e}")


def run_faucet_cycle(claim_fn=None, token: str = None, now: int = None,
                     ledger_path: str = None, faucet_state: str = None,
                     private_key=None, did: str = None, log=print) -> dict:
    """Một vòng faucet: kiểm cờ/cấu hình/cooldown/ngưỡng, rồi gọi `claim_fn` được tiêm
    để nhận token, và credit vào sổ cái. Mỗi nhánh là kết quả có ghi nhận, không ném lỗi:
      skipped_disabled / skipped_unconfigured / skipped_cooldown / skipped_full /
      error_claim / claimed.
    `claim_fn(req: dict) -> {"amount":..}` là seam THẬT (đợi spec FLOP), req gồm
    {token, amount, faucet_url, signed}."""
    token = (token or tm.default_token())
    if not faucet_enabled():
        return {"outcome": "skipped_disabled", "reason": "FLOP_FAUCET_ENABLED tắt"}

    url = faucet_url()
    if not url or claim_fn is None:
        return {"outcome": "skipped_unconfigured",
                "reason": ("FLOP_FAUCET_URL trống — từ chối claim faucet đoán mò"
                           if not url else "chưa tiêm claim_fn cho faucet")}

    now = int(now if now is not None else time.time())
    st = _load(faucet_state)
    last = 0
    try:
        last = int(st.get(token, {}).get("last_claim_ts", 0))
    except (TypeError, ValueError):
        last = 0
    if now - last < cooldown_hours() * 3600:
        wait_min = int((cooldown_hours() * 3600 - (now - last)) / 60)
        return {"outcome": "skipped_cooldown", "reason": f"còn ~{wait_min} phút tới lượt claim"}

    below = os.environ.get("FLOP_FAUCET_REFILL_BELOW", "").strip()
    if below:
        bal = tm._parse_amount(tm.check_balance(token, path=ledger_path)) or 0
        thr = tm._parse_amount(below)
        if thr is not None and bal >= thr:
            return {"outcome": "skipped_full",
                    "reason": f"số dư {tm._fmt(bal)} >= ngưỡng {below}, chưa cần claim"}

    signed = tm.sign_transaction(private_key, did, token, faucet_amount(), "faucet-claim")
    try:
        result = claim_fn({"token": token, "amount": faucet_amount(),
                           "faucet_url": url, "signed": signed}) or {}
    except Exception as e:
        return {"outcome": "error_claim", "reason": f"claim faucet thất bại: {e}"}

    amt = str(result.get("amount") or faucet_amount())
    cr = tm.credit(amt, token=token, memo="faucet claim", path=ledger_path)
    st.setdefault(token, {})["last_claim_ts"] = now
    _save(st, faucet_state)
    log(f"[faucet] claimed {amt} {token} testnet -> số dư {cr.get('balance_after')}")
    return {"outcome": "claimed", "amount": amt, "balance_after": cr.get("balance_after"),
            "reason": "đã nhận FLOP testnet từ faucet"}
