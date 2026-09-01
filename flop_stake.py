"""
flop_stake.py — Ủy quyền đặt cọc (stake delegation): ĐƯỜNG EARN AIRDROP THỨ HAI.

Theo bản nháp FLOP (intro.flop.network/agent.html, 27/08/2026), airdrop chỉ thưởng
cho (a) chi tiêu suy luận [xem flop_session.py] HOẶC (b) **ủy quyền đặt cọc cho thợ đào/
validator**. File này là scaffold cho (b): ủy quyền FLOP cho một validator, rút về, và
ghi nhận phần thưởng khi có sự kiện THẬT.

Cùng tinh thần token_manager.py: đọc env LIVE, KHÔNG raise ở top-level, mỗi nhánh trả
một outcome rõ ràng (không ném lỗi làm sập agent), Decimal chính xác, ký Ed25519 làm
bằng chứng ý định. Dùng CHUNG token_ledger.json với token_manager (đặt cọc = KHÓA bớt
số dư lỏng vào phần delegated).

CÔNG TẮC:
  TESTNET_ENABLED=false (mặc định) -> SIMULATION: chỉ bookkeeping trên sổ cái mock.
  TESTNET_ENABLED=true             -> TESTNET: gửi giao dịch delegate THẬT, CHỈ khi có
      FLOP_STAKE_URL + submit_fn được tiêm (thiếu -> skipped_unconfigured, không bịa tx).

Gate mức agent: maybe_delegate() chỉ chạy khi FLOP_STAKE_ENABLED bật (mặc định TẮT ->
agent 24/7 không đổi hành vi). delegate()/undelegate() lõi thì luôn gọi được (cho test/
điều khiển tay).

LƯU Ý TRUNG THỰC: FLOP CHƯA công bố công thức thưởng đặt cọc -> file này KHÔNG bịa tỉ lệ
reward. record_reward() chỉ ghi phần thưởng khi có SỰ KIỆN on-chain thật (seam). Chạy
thử offline: python flop_stake.py
"""

import os
import time
from decimal import Decimal, InvalidOperation

import token_manager as tm


# --- Cấu hình (đọc LIVE từ env) ---------------------------------------------------

def stake_enabled() -> bool:
    """Gate mức agent cho auto-delegate (mặc định TẮT)."""
    return os.environ.get("FLOP_STAKE_ENABLED", "").strip().lower() in ("1", "true", "on", "yes")


def stake_url() -> str:
    """Endpoint gửi giao dịch delegate (nạp khi FLOP công bố). Rỗng -> chưa cấu hình."""
    return os.environ.get("FLOP_STAKE_URL", "").strip() or tm.endpoint_url()


# --- Số học Decimal (nội bộ, cùng quy ước với token_manager) -----------------------

def _dec(x):
    try:
        d = Decimal(str(x).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return None if (d.is_nan() or d.is_infinite()) else d


def _fmt(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _stake_state(state: dict) -> dict:
    s = state.setdefault("stake", {})
    s.setdefault("delegated", {})   # {validator_did: "amount"}
    s.setdefault("rewards", "0")    # tổng phần thưởng đặt cọc đã nhận (sự kiện thật)
    return s


# --- Trạng thái đặt cọc ------------------------------------------------------------

def stake_status(token: str = None, path: str = None) -> dict:
    """Ảnh chụp đặt cọc: tổng đã ủy quyền, chi tiết theo validator, phần thưởng, số dư lỏng."""
    token = token or tm.default_token()
    state = tm.load_ledger(path)
    s = _stake_state(state)
    by_val = {v: a for v, a in s["delegated"].items()}
    total = sum((_dec(a) or Decimal(0)) for a in by_val.values())
    liquid = _dec(state.get("balances", {}).get(token, "0")) or Decimal(0)
    return {
        "token": token,
        "total_delegated": _fmt(total),
        "by_validator": by_val,
        "rewards_total": _fmt(_dec(s["rewards"]) or Decimal(0)),
        "liquid_balance": _fmt(liquid),
    }


# --- Ủy quyền đặt cọc (delegate) ---------------------------------------------------

def delegate(amount, validator: str, token: str = None, *, path: str = None,
             submit_fn=None, private_key=None, did: str = None, log=print) -> dict:
    """KHÓA `amount` FLOP lỏng vào phần đặt cọc cho `validator`. Mỗi nhánh trả outcome:
      - skipped_insufficient: amount sai/không dương, thiếu validator, hoặc vượt số dư lỏng
      - skipped_unconfigured: testnet mà thiếu FLOP_STAKE_URL/submit_fn (không bịa tx)
      - error_submit:         submit_fn ném lỗi ở testnet
      - delegated_onchain / delegated_simulated: thành công
    Ký Ed25519 làm bằng chứng ý định ở cả 2 chế độ (nếu có khóa)."""
    token = token or tm.default_token()
    mode = tm.ledger_mode()
    amt = _dec(amount)
    base = {"token": token, "validator": validator, "mode": mode,
            "amount": _fmt(amt) if amt is not None else "0"}

    if amt is None or amt <= 0:
        return {**base, "outcome": "skipped_insufficient",
                "reason": f"amount phải là số dương (nhận {amount!r})"}
    if not (validator and str(validator).strip()):
        return {**base, "outcome": "skipped_insufficient", "reason": "thiếu validator"}

    state = tm.load_ledger(path)
    liquid = _dec(state.get("balances", {}).get(token, "0")) or Decimal(0)
    if liquid < amt:
        return {**base, "outcome": "skipped_insufficient",
                "reason": f"không đủ {token} lỏng: {_fmt(liquid)} < {_fmt(amt)}"}

    signed = tm.sign_transaction(private_key, did, token, _fmt(amt), f"delegate:{validator}")
    tx_hash = None

    if mode == "testnet":
        url = stake_url()
        if not url or submit_fn is None:
            return {**base, "outcome": "skipped_unconfigured", "signed": signed,
                    "reason": ("TESTNET_ENABLED=true nhưng thiếu FLOP_STAKE_URL — từ chối gửi "
                               "delegate tới endpoint đoán mò") if not url else
                              "TESTNET_ENABLED=true nhưng chưa tiêm submit_fn — từ chối bịa giao dịch"}
        try:
            result = submit_fn({"action": "delegate", "token": token, "amount": _fmt(amt),
                                "validator": validator, "url": url, "signed": signed}) or {}
            tx_hash = result.get("tx_hash") or result.get("txHash")
        except Exception as e:
            return {**base, "outcome": "error_submit", "reason": f"gửi delegate testnet thất bại: {e}"}

    # Bookkeeping (cả 2 chế độ): trừ số dư lỏng -> cộng vào delegated[validator].
    s = _stake_state(state)
    state["balances"][token] = _fmt(liquid - amt)
    prev = _dec(s["delegated"].get(validator, "0")) or Decimal(0)
    s["delegated"][validator] = _fmt(prev + amt)
    entry = {"kind": "delegate", "token": token, "amount": _fmt(amt), "validator": validator,
             "mode": mode, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if tx_hash:
        entry["tx_hash"] = tx_hash
    state["entries"].append(entry)
    tm.save_ledger(state, path)

    if mode == "testnet":
        log(f"[stake] delegated {_fmt(amt)} {token} -> {validator} (tx {(tx_hash or '')[:12]}…)")
        outcome = "delegated_onchain"
    else:
        log(f"[SIMULATION] Delegated {_fmt(amt)} MOCK_{token} -> {validator}")
        outcome = "delegated_simulated"
    return {**base, "outcome": outcome, "tx_hash": tx_hash, "signed": signed,
            "liquid_after": state["balances"][token], "delegated_after": s["delegated"][validator]}


def undelegate(amount, validator: str, token: str = None, *, path: str = None,
               submit_fn=None, private_key=None, did: str = None, log=print) -> dict:
    """Rút `amount` FLOP từ phần đặt cọc ở `validator` về số dư lỏng. (Thực tế có thể có
    kỳ unbonding — sim thì tức thời; đặt cờ khi FLOP công bố cơ chế.) Outcome tương tự
    delegate: undelegated_onchain / undelegated_simulated / skipped_insufficient / …"""
    token = token or tm.default_token()
    mode = tm.ledger_mode()
    amt = _dec(amount)
    base = {"token": token, "validator": validator, "mode": mode,
            "amount": _fmt(amt) if amt is not None else "0"}
    if amt is None or amt <= 0:
        return {**base, "outcome": "skipped_insufficient",
                "reason": f"amount phải là số dương (nhận {amount!r})"}

    state = tm.load_ledger(path)
    s = _stake_state(state)
    staked = _dec(s["delegated"].get(validator, "0")) or Decimal(0)
    if staked < amt:
        return {**base, "outcome": "skipped_insufficient",
                "reason": f"đặt cọc ở {validator} chỉ {_fmt(staked)} < {_fmt(amt)}"}

    signed = tm.sign_transaction(private_key, did, token, _fmt(amt), f"undelegate:{validator}")
    tx_hash = None
    if mode == "testnet":
        url = stake_url()
        if not url or submit_fn is None:
            return {**base, "outcome": "skipped_unconfigured", "signed": signed,
                    "reason": "TESTNET_ENABLED=true nhưng thiếu FLOP_STAKE_URL/submit_fn"}
        try:
            result = submit_fn({"action": "undelegate", "token": token, "amount": _fmt(amt),
                                "validator": validator, "url": url, "signed": signed}) or {}
            tx_hash = result.get("tx_hash") or result.get("txHash")
        except Exception as e:
            return {**base, "outcome": "error_submit", "reason": f"gửi undelegate thất bại: {e}"}

    new_staked = staked - amt
    if new_staked > 0:
        s["delegated"][validator] = _fmt(new_staked)
    else:
        s["delegated"].pop(validator, None)
    liquid = _dec(state.get("balances", {}).get(token, "0")) or Decimal(0)
    state["balances"][token] = _fmt(liquid + amt)
    entry = {"kind": "undelegate", "token": token, "amount": _fmt(amt), "validator": validator,
             "mode": mode, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if tx_hash:
        entry["tx_hash"] = tx_hash
    state["entries"].append(entry)
    tm.save_ledger(state, path)
    outcome = "undelegated_onchain" if mode == "testnet" else "undelegated_simulated"
    return {**base, "outcome": outcome, "tx_hash": tx_hash,
            "liquid_after": state["balances"][token]}


def record_reward(amount, token: str = None, validator: str = None, *, path: str = None,
                  log=print) -> dict:
    """Ghi nhận phần thưởng đặt cọc ĐÃ NHẬN (sự kiện on-chain THẬT). Cộng vào tổng rewards
    và vào số dư lỏng. KHÔNG bịa tỉ lệ — chỉ gọi khi có reward thật (seam). Từ chối số
    không dương mà không ném lỗi."""
    token = token or tm.default_token()
    amt = _dec(amount)
    if amt is None or amt <= 0:
        return {"outcome": "skipped_insufficient", "token": token,
                "reason": f"reward phải là số dương (nhận {amount!r})"}
    state = tm.load_ledger(path)
    s = _stake_state(state)
    s["rewards"] = _fmt((_dec(s["rewards"]) or Decimal(0)) + amt)
    liquid = _dec(state.get("balances", {}).get(token, "0")) or Decimal(0)
    state["balances"][token] = _fmt(liquid + amt)
    state["entries"].append({
        "kind": "stake_reward", "token": token, "amount": _fmt(amt),
        "validator": validator, "mode": tm.ledger_mode(),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    tm.save_ledger(state, path)
    log(f"[stake] reward +{_fmt(amt)} {token}" + (f" từ {validator}" if validator else ""))
    return {"outcome": "reward_recorded", "token": token, "amount": _fmt(amt),
            "rewards_total": s["rewards"], "liquid_after": state["balances"][token]}


def maybe_delegate(amount, validator: str, **kw) -> dict:
    """Điểm vào cho AGENT: chỉ delegate khi FLOP_STAKE_ENABLED bật (mặc định TẮT ->
    skipped_off, agent không đổi hành vi). Bọc kín mọi lỗi để không làm sập agent."""
    if not stake_enabled():
        return {"outcome": "skipped_off", "reason": "FLOP_STAKE_ENABLED tắt"}
    try:
        return delegate(amount, validator, **kw)
    except Exception as e:      # tuyệt đối không làm sập caller
        return {"outcome": "error_stake", "reason": str(e)}


# --- Demo offline ------------------------------------------------------------------

def _demo() -> None:
    import tempfile
    path = os.path.join(tempfile.mkdtemp(prefix="flop-stake-demo-"), "ledger.json")
    tm.credit("100", path=path)
    print("— FLOP stake delegation (demo offline) —")
    print(f"mode: {tm.ledger_mode()}")
    d = delegate("40", "validator-alpha", path=path)
    print(f"delegate: {d['outcome']} · lỏng còn {d.get('liquid_after')} · đặt cọc {d.get('delegated_after')}")
    r = record_reward("2.5", validator="validator-alpha", path=path)
    print(f"reward:   {r['outcome']} · tổng thưởng {r.get('rewards_total')} · lỏng {r.get('liquid_after')}")
    u = undelegate("10", "validator-alpha", path=path)
    print(f"undelegate: {u['outcome']} · lỏng {u.get('liquid_after')}")
    print(f"status:   {stake_status(path=path)}")
    print("\nTrung thực: simulation KHÔNG lên chain; reward chỉ ghi khi có sự kiện thật. "
          "FLOP chưa công bố công thức thưởng đặt cọc -> không bịa tỉ lệ ở đây.")


if __name__ == "__main__":
    _demo()
