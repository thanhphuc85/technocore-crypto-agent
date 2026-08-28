"""
flop_pacer.py — "Dynamic Spend Rate": rải đều ngân sách FLOP/ngày qua 24 giờ.

Mục tiêu (theo guide săn airdrop): thay vì xả hết token faucet trong 1 phút (rất dễ
bị hệ thống gạch tên vì Spam/Bot), tính lượng NÊN chi mỗi lần chạy để bám một lịch
tuyến tính theo thời gian trong ngày — không dump, không bỏ lỡ lượt, giữ nhịp "xanh
đèn" 24/7. Kèm với việc gắn mỗi lần chi vào 1 hoạt động THẬT (1 lần suy luận LLM),
đây là cách tối đa hóa tiêu thụ HỢP LỆ mà không tạo churn giả.

Thuần + kiểm thử được: `next_spend_amount()` (nên chi bao nhiêu lúc này) và
`record_spend()` (ghi nhận đã chi) đọc/ghi một file trạng thái nhỏ. Tắt (trả None)
khi FLOP_DAILY_BUDGET chưa đặt -> caller dùng phí cố định như cũ.

Env:
  FLOP_DAILY_BUDGET   ngân sách FLOP mỗi ngày (bắt buộc để bật pacer). Rỗng -> tắt.
  FLOP_MAX_PER_RUN    trần chi mỗi lần chạy (tùy chọn).
  FLOP_MIN_SPEND      dưới mức này thì đợi tích thêm, tránh chi "bụi" (mặc định 0.0001).
  FLOP_PACER_FILE     nơi lưu nhịp chi trong ngày (mặc định flop_pacer.json).
  FLOP_PACE_JITTER_PCT  (chống sybil) nhiễu ngẫu nhiên ±X% quanh lượng đến hạn, để nhịp
                        chi KHÔNG rơi đúng mục tiêu tuyến tính tất định (dấu hiệu bot).
                        Rỗng/0 -> tắt (nhịp tuyến tính như cũ). Kẹp về [0,100].
"""

import os
import json
import time
import random
from decimal import Decimal, InvalidOperation


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


# --- config (đọc live từ env) -----------------------------------------------------

def daily_budget():
    d = _dec(os.environ.get("FLOP_DAILY_BUDGET", ""))
    return d if (d is not None and d > 0) else None


def max_per_run():
    d = _dec(os.environ.get("FLOP_MAX_PER_RUN", ""))
    return d if (d is not None and d > 0) else None


def min_spend() -> Decimal:
    d = _dec(os.environ.get("FLOP_MIN_SPEND", ""))
    return d if (d is not None and d > 0) else Decimal("0.0001")


def jitter_pct() -> Decimal:
    """Biên nhiễu ±X% cho nhịp chi (chống "chi đúng mục tiêu = bot"). Rỗng/không
    dương -> 0 (tắt). Kẹp trần 100 để jitter không lật dấu lượng chi."""
    d = _dec(os.environ.get("FLOP_PACE_JITTER_PCT", ""))
    if d is None or d <= 0:
        return Decimal(0)
    return d if d <= 100 else Decimal(100)


def _apply_jitter(due: Decimal, rng=None) -> Decimal:
    """Nhân `due` với hệ số ngẫu nhiên trong [1-pct%, 1+pct%]. `rng()` trả float ∈ [0,1)
    (tiêm được để test tất định; mặc định random.random). Không bao giờ trả số âm."""
    pct = jitter_pct()
    if pct <= 0 or due <= 0:
        return due
    r = Decimal(str((rng or random.random)()))       # [0,1)
    factor = Decimal(1) + (r * 2 - 1) * pct / Decimal(100)
    out = due * factor
    return out if out > 0 else Decimal(0)


def pacer_path(path: str = None) -> str:
    return path or os.environ.get("FLOP_PACER_FILE", "").strip() or "flop_pacer.json"


def _token(token: str = None) -> str:
    return token or os.environ.get("FLOP_TOKEN_SYMBOL", "").strip() or "FLOP"


# --- state (JSON nhỏ, tách khỏi sổ cái) -------------------------------------------

def _load(path: str = None) -> dict:
    try:
        with open(pacer_path(path), "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[pacer] không đọc được state ({e}); coi như rỗng")
        return {}


def _save(state: dict, path: str = None) -> None:
    try:
        with open(pacer_path(path), "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[pacer] không lưu được state: {e}")


def _day(now: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _seconds_into_day(now: int) -> int:
    t = time.gmtime(now)
    return t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec


def _rec_for(state: dict, token: str, now: int) -> dict:
    rec = state.get(token, {})
    if rec.get("day") != _day(now):                 # sang ngày mới -> reset nhịp
        rec = {"day": _day(now), "spent_today": "0"}
    return rec


# --- API --------------------------------------------------------------------------

def next_spend_amount(token: str = None, now: int = None, path: str = None, rng=None):
    """Số FLOP NÊN chi lần chạy này để bám lịch rải đều. Trả:
       None  -> pacer tắt (FLOP_DAILY_BUDGET chưa đặt) -> caller dùng phí cố định;
       "0"   -> chưa tới nhịp (đã bám kịp mục tiêu, hoặc phần đến hạn < FLOP_MIN_SPEND);
       "x.y" -> lượng nên chi để đuổi kịp mục tiêu tuyến tính, đã kẹp trần/lần.
    `rng` (tùy chọn) là nguồn ngẫu nhiên cho jitter, tiêm được để test tất định."""
    budget = daily_budget()
    if budget is None:
        return None
    now = int(now if now is not None else time.time())
    token = _token(token)
    rec = _rec_for(_load(path), token, now)
    spent_today = _dec(rec.get("spent_today", "0")) or Decimal(0)

    # mục tiêu tuyến tính tới thời điểm này = budget * (giây đã trôi trong ngày / 86400)
    target = budget * Decimal(_seconds_into_day(now)) / Decimal(86400)
    due = target - spent_today
    if due <= 0:
        return "0"
    cap = max_per_run()
    if cap is not None and due > cap:
        due = cap
    due = _apply_jitter(due, rng)                    # chống sybil: phá nhịp tuyến tính robot
    if cap is not None and due > cap:                # jitter lên có thể vượt trần -> kẹp lại
        due = cap
    if due < min_spend():
        return "0"                                  # đợi tích thêm, tránh chi bụi liên tục
    return _fmt(due)


def record_spend(amount, token: str = None, now: int = None, path: str = None) -> None:
    """Ghi nhận đã chi `amount` để pacer trừ vào ngân sách ngày (gọi sau khi chi OK)."""
    amt = _dec(amount)
    if amt is None or amt <= 0:
        return
    now = int(now if now is not None else time.time())
    token = _token(token)
    state = _load(path)
    rec = _rec_for(state, token, now)
    rec["spent_today"] = _fmt((_dec(rec.get("spent_today", "0")) or Decimal(0)) + amt)
    rec["last_ts"] = now
    state[token] = rec
    _save(state, path)


def pacing_status(token: str = None, now: int = None, path: str = None) -> dict:
    """Ảnh chụp nhịp hôm nay (để log/telemetry): ngân sách, đã chi, mục tiêu, đến hạn."""
    budget = daily_budget()
    now = int(now if now is not None else time.time())
    token = _token(token)
    rec = _rec_for(_load(path), token, now)
    spent = _dec(rec.get("spent_today", "0")) or Decimal(0)
    target = (budget * Decimal(_seconds_into_day(now)) / Decimal(86400)) if budget else Decimal(0)
    return {
        "token": token, "enabled": budget is not None,
        "daily_budget": _fmt(budget) if budget else None,
        "spent_today": _fmt(spent), "target_now": _fmt(target),
        # Telemetry: báo lượng đến hạn TRƯỚC jitter (rng=0.5 -> hệ số 1.0) để status
        # ổn định/đọc được; jitter chỉ áp ở lần chi thật qua next_spend_amount().
        "next_due": next_spend_amount(token, now, path, rng=lambda: 0.5),
    }
