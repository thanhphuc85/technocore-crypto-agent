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
  FLOP_FAUCET_ADDRESS       địa chỉ SS58 nhận FLOP. Có -> gửi đúng định dạng faucet.flop.network
                            (`POST /fund {"address": ...}`) và học chu kỳ từ <origin>/health.
                            Kiểm checksum trước khi gửi (sai 1 ký tự = mất FLOP).
  FLOP_FAUCET_TIER          quick (mặc định, 25 FLOP) | linked (verified human, 500-2000)
                            | manual (operator tự xin Verified bằng MetaMask -> agent KHÔNG xin:
                            chu kỳ 24h tính chung mọi mức, lượt quick sẽ chặn lượt Verified).
  FLOP_FAUCET_PROVIDER      với linked: brightid | self — chứng minh người thật đã được NGƯỜI
                            gắn vào địa chỉ này. MetaMask/SIWE cần người ký mỗi lần -> agent từ chối.
  FLOP_FAUCET_AMOUNT        lượng dự kiến mỗi claim (mặc định 100).
  FLOP_FAUCET_COOLDOWN_HOURS khoảng tối thiểu giữa 2 claim (mặc định 24). 'auto' = THĂM DÒ:
                            thử mỗi AUTO_PROBE_HOURS (1h); faucet trả thời điểm claim kế tiếp
                            (Retry-After / next_claim / retry_after) thì CHỜ đúng tới đó — tự
                            khớp chu kỳ thật (giờ hay ngày) mà không gõ faucet dồn dập.
  FLOP_FAUCET_REFILL_BELOW  (tùy chọn) chỉ claim khi số dư testnet < ngưỡng này.
  FLOP_FAUCET_STATE         nơi lưu mốc claim gần nhất (mặc định flop_faucet.json).
  FLOP_FAUCET_DEMAND_ONLY   (chống sybil) chỉ claim khi số dư CẠN vì chi thật — bắt
                            buộc phải có FLOP_FAUCET_REFILL_BELOW, nếu không -> skipped_demand
                            (từ chối claim theo lịch, tránh round-trip faucet->dump).
  FLOP_FAUCET_JITTER_MIN    (chống sybil) cộng thêm 0..N phút ngẫu nhiên vào cooldown để
                            claim KHÔNG rơi đúng ranh giới cooldown (dấu hiệu bot 24/7).
                            Offset ổn định trong 1 chu kỳ (seed theo mốc claim trước).
                            Rỗng -> MẶC ĐỊNH 30; đặt 0 -> tắt.
  FLOP_FAUCET_MAX_PER_DAY   (chống sybil) trần số claim mỗi ngày UTC; chạm trần ->
                            skipped_daily_cap. Rỗng -> không giới hạn.
"""

import os
import json
import time
import random

import token_manager as tm


def _flag(n: str) -> bool:
    return os.environ.get(n, "").strip().lower() in ("1", "true", "on", "yes")


def faucet_enabled() -> bool:
    return _flag("FLOP_FAUCET_ENABLED")


def faucet_url() -> str:
    return os.environ.get("FLOP_FAUCET_URL", "").strip()


def faucet_amount() -> str:
    return os.environ.get("FLOP_FAUCET_AMOUNT", "").strip() or "100"


AUTO_PROBE_HOURS = 1.0
# Claim LỖI (mạng, 4xx/5xx, thân báo lỗi) cũng phải lùi: không thì run 5 phút sau gõ lại ngay,
# tức ~12 lần/giờ vào đúng lúc faucet đang từ chối.
ERROR_BACKOFF_SEC = 3600


def cooldown_auto() -> bool:
    return os.environ.get("FLOP_FAUCET_COOLDOWN_HOURS", "").strip().lower() == "auto"


def cooldown_hours() -> float:
    if cooldown_auto():
        return AUTO_PROBE_HOURS
    try:
        v = float(os.environ.get("FLOP_FAUCET_COOLDOWN_HOURS", "").strip() or 24)
        return v if v >= 0 else 24.0
    except ValueError:
        return 24.0


def demand_only() -> bool:
    return _flag("FLOP_FAUCET_DEMAND_ONLY")


def max_per_day():
    """Trần số claim faucet mỗi ngày (UTC) — envelope chống sybil để dù cấu hình sai
    cũng không spike lên vùng bot. Rỗng/không hợp lệ/không dương -> None (không giới hạn)."""
    raw = os.environ.get("FLOP_FAUCET_MAX_PER_DAY", "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if v > 0 else None


def _day(now: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


DEFAULT_JITTER_MIN = 30.0


def jitter_minutes() -> float:
    """Rỗng -> MẶC ĐỊNH 30 phút: không có nó, mọi fork claim đúng cùng ranh giới cooldown
    mỗi ngày — một nhịp chung, đúng kiểu dấu hiệu "common control". 0 -> tắt tường minh."""
    raw = os.environ.get("FLOP_FAUCET_JITTER_MIN", "").strip()
    if not raw:
        return DEFAULT_JITTER_MIN
    try:
        v = float(raw)
        return v if v > 0 else 0.0
    except ValueError:
        return 0.0


def _cooldown_seconds(last: int) -> float:
    """Cooldown hiệu dụng = cooldown_hours + (0..JITTER_MIN phút). Offset ngẫu nhiên
    nhưng TẤT ĐỊNH trong một chu kỳ (seed theo mốc claim trước) -> claim không rơi đúng
    ranh giới cooldown, cũng không nhấp nháy giữa các lần chạy. last=0 (chưa claim) ->
    không jitter để lần claim đầu không bị trì hoãn vô cớ."""
    cd = cooldown_hours() * 3600.0
    # Jitter tối đa 10% chu kỳ: chu kỳ 1 giờ mà cộng tới 30 phút thì mất ~1/3 số lượt claim.
    jmin = min(jitter_minutes(), cooldown_hours() * 6.0)
    if jmin > 0 and last > 0:
        cd += random.Random(last).random() * jmin * 60.0
    return cd


def seconds_until_next(token: str = None, now: int = None, faucet_state: str = None):
    """Còn bao nhiêu giây tới lượt claim kế tiếp (theo hẹn của faucet và cooldown+jitter);
    0 = được claim ngay; None = chưa từng claim."""
    token = token or tm.default_token()
    now = int(now if now is not None else time.time())
    rec = _load(faucet_state).get(token) or {}
    try:
        last = int(rec.get("last_claim_ts") or 0)
        nxt = int(rec.get("next_claim_ts") or 0)
    except (TypeError, ValueError):
        return None
    if not last and not nxt:
        return None
    due = max(nxt, last + int(_cooldown_seconds(last)) if last else 0)
    return max(0, due - now)


def merge_state(local, remote) -> dict:
    """Hợp nhất trạng thái faucet local với bản KV (runner mới có file rỗng): mốc claim và
    mốc chờ lấy MAX, đếm claim CÙNG ngày lấy MAX. Mất file không được biến thành 'chưa claim
    bao giờ' — nếu không, mỗi run 5 phút đều gõ faucet và trần ngày vô tác dụng."""
    out = {}
    for src in (local, remote):
        if not isinstance(src, dict):
            continue
        for tok, rec in src.items():
            if not isinstance(rec, dict):
                continue
            o = out.setdefault(tok, {})
            now = int(time.time())
            for k, ahead in (("last_claim_ts", 600), ("next_claim_ts", 2 * 86400)):
                try:
                    v = int(rec.get(k) or 0)
                    if v > now + ahead:                   # mốc quá xa ở tương lai = rác -> bỏ
                        continue
                    o[k] = max(int(o.get(k) or 0), v)
                except (TypeError, ValueError):
                    pass
            cl = rec.get("claims")
            if isinstance(cl, dict):
                oc = o.setdefault("claims", {})
                for d, n in cl.items():
                    try:
                        oc[d] = max(int(oc.get(d) or 0), int(n))
                    except (TypeError, ValueError):
                        pass
                for d in sorted(oc)[:-2]:            # chỉ giữ 2 ngày gần nhất
                    oc.pop(d, None)
    return out


def _learned_next(result: dict, now: int):
    """Mốc claim kế tiếp do faucet báo (giây tuyệt đối) hoặc None."""
    if not isinstance(result, dict):
        return None
    try:
        if result.get("retry_after_s") is not None:
            return now + max(0, int(float(result["retry_after_s"])))
        if result.get("next_claim_ts") is not None:
            v = int(float(result["next_claim_ts"]))
            return v // 1000 if v > 10**11 else v          # nhận cả mili-giây
    except (TypeError, ValueError):
        return None
    return None


def state_path(path: str = None) -> str:
    return path or os.environ.get("FLOP_FAUCET_STATE", "").strip() or "flop_faucet.json"


def _load(path: str = None) -> dict:
    try:
        with open(state_path(path), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[faucet] không đọc được state ({e}); coi như rỗng")
        return {}


def _save(state: dict, path: str = None) -> None:
    try:
        with open(state_path(path), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[faucet] không lưu được state: {e}")


def run_faucet_cycle(claim_fn=None, token: str = None, now: int = None,
                     ledger_path: str = None, faucet_state: str = None,
                     private_key=None, did: str = None, log=print) -> dict:
    """Một vòng faucet: kiểm cờ/cấu hình/cooldown/ngưỡng, rồi gọi `claim_fn` được tiêm
    để nhận token, và credit vào sổ cái. Mỗi nhánh là kết quả có ghi nhận, không ném lỗi:
      skipped_disabled / skipped_unconfigured / skipped_demand / skipped_cooldown /
      skipped_full / skipped_daily_cap / error_claim / claimed.
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
    try:
        nxt = int(st.get(token, {}).get("next_claim_ts", 0))
    except (TypeError, ValueError):
        nxt = 0
    if nxt > now:                                    # faucet đã báo giờ mở lượt kế tiếp
        return {"outcome": "skipped_cooldown",
                "reason": f"faucet hẹn lượt kế tiếp sau ~{int((nxt - now) / 60)} phút"}
    cd = _cooldown_seconds(last)                     # cooldown + jitter (chống claim đúng ranh giới)
    if now - last < cd:
        wait_min = int((cd - (now - last)) / 60)
        return {"outcome": "skipped_cooldown", "reason": f"còn ~{wait_min} phút tới lượt claim"}

    # Faucet theo NHU CẦU (chống sybil): chỉ claim khi số dư cạn vì chi thật. Bật
    # DEMAND_ONLY mà thiếu ngưỡng REFILL_BELOW -> từ chối claim theo lịch (calendar).
    below = os.environ.get("FLOP_FAUCET_REFILL_BELOW", "").strip()
    if demand_only() and not below:
        return {"outcome": "skipped_demand",
                "reason": ("FLOP_FAUCET_DEMAND_ONLY bật nhưng thiếu FLOP_FAUCET_REFILL_BELOW "
                           "— từ chối claim theo lịch (chỉ claim khi số dư cạn vì chi thật)")}
    if below:
        bal = tm._parse_amount(tm.check_balance(token, path=ledger_path)) or 0
        thr = tm._parse_amount(below)
        if thr is not None and bal >= thr:
            return {"outcome": "skipped_full",
                    "reason": f"số dư {tm._fmt(bal)} >= ngưỡng {below}, chưa cần claim"}

    # Envelope chống sybil: trần số claim/ngày (UTC). Đếm theo ngày, kẹp trần tin được.
    cap = max_per_day()
    day = _day(now)
    claims_today = 0
    if cap is not None:
        try:
            claims_today = int(st.get(token, {}).get("claims", {}).get(day, 0))
        except (TypeError, ValueError):
            claims_today = 0
        if claims_today >= cap:
            return {"outcome": "skipped_daily_cap",
                    "reason": f"đã claim {claims_today}/{cap} lần hôm nay ({day}) — chạm trần ngày"}

    signed = tm.sign_transaction(private_key, did, token, faucet_amount(), "faucet-claim")
    try:
        result = claim_fn({"token": token, "amount": faucet_amount(),
                           "faucet_url": url, "signed": signed}) or {}
    except Exception as e:
        rec = st.setdefault(token, {})
        backoff = int(min(ERROR_BACKOFF_SEC, max(300.0, cooldown_hours() * 3600)))
        rec["next_claim_ts"] = now + backoff
        _save(st, faucet_state)
        return {"outcome": "error_claim",
                "reason": f"claim faucet thất bại: {e} — thử lại sau ~{backoff // 60} phút"}

    raw_amt = result.get("amount")
    amt = str(faucet_amount() if raw_amt is None else raw_amt)
    parsed = tm._parse_amount(amt)
    nxt = _learned_next(result, now)
    if parsed is None or parsed <= 0:
        # Faucet trả 0/rác: KHÔNG ghi sổ, KHÔNG tính là 1 lần claim (không đốt trần ngày).
        # Vẫn đặt mốc thời gian để không gõ lại faucet mỗi 5 phút.
        st.setdefault(token, {})["last_claim_ts"] = now
        if nxt:
            st[token]["next_claim_ts"] = nxt
        _save(st, faucet_state)
        return {"outcome": "claimed_zero", "amount": "0",
                "reason": f"faucet trả số lượng {raw_amt!r} — không ghi sổ"}
    cr = tm.credit(amt, token=token, memo="faucet claim", path=ledger_path)
    rec = st.setdefault(token, {})
    rec["last_claim_ts"] = now
    if nxt:
        rec["next_claim_ts"] = nxt
    # Đếm claim trong ngày LUÔN (kể cả khi chưa đặt trần) -> báo cáo/merge có số đúng.
    if cap is None:
        try:
            claims_today = int(rec.get("claims", {}).get(day, 0))
        except (TypeError, ValueError, AttributeError):
            claims_today = 0
    rec["claims"] = {day: claims_today + 1}
    _save(st, faucet_state)
    log(f"[faucet] claimed {amt} {token} testnet -> số dư {cr.get('balance_after')}")
    return {"outcome": "claimed", "amount": amt, "balance_after": cr.get("balance_after"),
            "reason": "đã nhận FLOP testnet từ faucet"}
