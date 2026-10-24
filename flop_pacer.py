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
                        Rỗng -> MẶC ĐỊNH 20; đặt 0 -> tắt (nhịp tuyến tính). Kẹp [0,100].
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

def daily_budget(token: str = None, ledger_path: str = None):
    """Ngân sách FLOP cho hôm nay. Rỗng/không hợp lệ -> None (pacer TẮT).

    `auto` -> suy từ VÍ THẬT: số dư còn lại + đã chi trong ngày UTC. Vì sao cần: faucet
    nạp mỗi ngày một lượng khác nhau, mà ngân sách cố định thì phải chỉnh tay sau mỗi lần
    nạp — không ai làm nổi hằng ngày, và token thừa nằm im là phân bổ bị mất
    ("Holding test tokens earns nothing"). `auto` làm việc đó tự động."""
    raw = os.environ.get("FLOP_DAILY_BUDGET", "").strip()
    if raw.lower() == "auto":
        try:
            import token_manager as _tm        # import TRỄ: tránh vòng lặp import
            d = _dec(_tm.available_today(token=token, path=ledger_path))
        except Exception:
            return None                        # không đọc được ví -> pacer tắt, không đoán
        return d if (d is not None and d > 0) else None
    d = _dec(raw)
    return d if (d is not None and d > 0) else None


def max_per_run(budget=None):
    """Trần cho MỘT lần chi. Nhận hai dạng:

      "2.5"  -> số TUYỆT ĐỐI (hành vi cũ)
      "2%"   -> PHẦN TRĂM ngân sách ngày

    Vì sao cần dạng %: trần tuyệt đối không co giãn. Đặt 0.5 cho ví 100 FLOP là hợp lý,
    nhưng khi faucet cấp 100.000/ngày thì nó bóp xuống còn 576/ngày (0,576% ngân sách) —
    và bóp trong IM LẶNG, cuối ngày mới biết thiếu 99%.

    Rỗng -> KHÔNG trần (giữ đúng hợp đồng cũ của module). Lưu ý vận hành: không trần
    nghĩa là sau một sự cố dài, phần thiếu dồn lại có thể bị chi hết trong MỘT lần —
    đúng hình dạng dump mà pacer sinh ra để tránh. Hãy luôn đặt một trần."""
    raw = os.environ.get("FLOP_MAX_PER_RUN", "").strip()
    if raw.endswith("%"):
        pct = _dec(raw[:-1])
        if pct is None or pct <= 0 or not budget or budget <= 0:
            return None
        return budget * pct / Decimal(100)
    d = _dec(raw)
    if d is not None and d > 0:
        return d
    return None                               # rỗng, hoặc rác/không dương -> không trần


def min_spend() -> Decimal:
    d = _dec(os.environ.get("FLOP_MIN_SPEND", ""))
    return d if (d is not None and d > 0) else Decimal("0.0001")


DEFAULT_JITTER_PCT = Decimal(20)


def jitter_pct() -> Decimal:
    """Biên nhiễu ±X% cho nhịp chi (chống "chi đúng mục tiêu = bot"). Rỗng -> MẶC ĐỊNH
    20 (FLOP đã công bố phạt "manufactured activity"); 0/âm -> tắt tường minh. Kẹp trần
    100 để jitter không lật dấu lượng chi."""
    raw = os.environ.get("FLOP_PACE_JITTER_PCT", "").strip()
    if not raw:
        return DEFAULT_JITTER_PCT
    d = _dec(raw)
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
        with open(pacer_path(path), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[pacer] không đọc được state ({e}); coi như rỗng")
        return {}


def _save(state: dict, path: str = None) -> None:
    try:
        with open(pacer_path(path), "w", encoding="utf-8") as f:
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

def _spent_today(rec: dict, token: str, now: int, ledger_path: str = None) -> Decimal:
    """Đã chi hôm nay = MAX(bản ghi của pacer, tổng chi hôm nay trong SỔ CÁI).

    Vì sao lấy MAX chứ không chỉ đọc file pacer: `flop_pacer.json` KHÔNG được cache qua
    run (workflow chỉ cache `state.json`), nên trên runner nó luôn rỗng -> pacer tưởng
    hôm nay chưa chi gì -> `due` luôn bằng cả mục tiêu -> chi kịch trần MỌI run và KHÔNG
    BAO GIỜ hãm. Đo được: ở mốc giữa trưa ngân sách 24, bản nhớ dừng đúng 12.0 còn bản
    quên chi 20.0 (vượt mục tiêu 67%).

    Sổ cái bền qua KV nên nó là nguồn đúng; vẫn lấy MAX để bản ghi pacer (khi có, vd khi
    chạy trên VPS có đĩa bền) không bị bỏ qua."""
    mine = _dec(rec.get("spent_today", "0")) or Decimal(0)
    if not ledger_path:
        # KHÔNG tự đi tìm sổ cái mặc định. Caller phải NÊU RÕ sổ cái nào — nếu không,
        # hàm thuần này lại phụ thuộc vào một file toàn cục trong thư mục làm việc,
        # khiến kết quả đổi theo việc test nào chạy trước (đúng lỗi đã làm CI đỏ).
        return mine
    try:
        import token_manager as _tm        # import TRỄ: tránh vòng lặp import
        led = _tm.spent_today_amount(token=token, now=now, path=ledger_path)
    except Exception:
        return mine                        # không đọc được sổ cái -> dùng bản ghi pacer
    return mine if mine > led else led


def next_spend_amount(token: str = None, now: int = None, path: str = None, rng=None,
                      ledger_path: str = None, budget=None):
    """Số FLOP NÊN chi lần chạy này để bám lịch rải đều. Trả:
       None  -> pacer tắt (FLOP_DAILY_BUDGET chưa đặt) -> caller dùng phí cố định;
       "0"   -> chưa tới nhịp (đã bám kịp mục tiêu, hoặc phần đến hạn < FLOP_MIN_SPEND);
       "x.y" -> lượng nên chi để đuổi kịp mục tiêu tuyến tính, đã kẹp trần/lần.
    `rng` (tùy chọn) là nguồn ngẫu nhiên cho jitter, tiêm được để test tất định.
    `budget` (tùy chọn) ĐÈ FLOP_DAILY_BUDGET: để phiên inference dùng pacer mà không phải
    bật FLOP_DAILY_BUDGET — biến đó còn điều khiển giá metering, và bật nó từng làm MỘT
    lần suy luận kibble bị tính 2 FLOP thay vì 0.001 (chi phồng, không phải mua thêm)."""
    if budget is not None:
        budget = _dec(budget)
        if budget is None or budget <= 0:
            return None
    else:
        budget = daily_budget(token, ledger_path)
    if budget is None:
        return None
    now = int(now if now is not None else time.time())
    token = _token(token)
    rec = _rec_for(_load(path), token, now)
    spent_today = _spent_today(rec, token, now, ledger_path)

    # mục tiêu tuyến tính tới thời điểm này = budget * (giây đã trôi trong ngày / 86400)
    target = budget * Decimal(_seconds_into_day(now)) / Decimal(86400)
    due = target - spent_today
    if due <= 0:
        return "0"
    cap = max_per_run(budget)
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


def pacing_status(token: str = None, now: int = None, path: str = None,
                  ledger_path: str = None) -> dict:
    """Ảnh chụp nhịp hôm nay (để log/telemetry): ngân sách, đã chi, mục tiêu, đến hạn."""
    budget = daily_budget(token, ledger_path)
    now = int(now if now is not None else time.time())
    token = _token(token)
    rec = _rec_for(_load(path), token, now)
    spent = _spent_today(rec, token, now, ledger_path)
    target = (budget * Decimal(_seconds_into_day(now)) / Decimal(86400)) if budget else Decimal(0)
    return {
        "token": token, "enabled": budget is not None,
        "daily_budget": _fmt(budget) if budget else None,
        "spent_today": _fmt(spent), "target_now": _fmt(target),
        # Telemetry: báo lượng đến hạn TRƯỚC jitter (rng=0.5 -> hệ số 1.0) để status
        # ổn định/đọc được; jitter chỉ áp ở lần chi thật qua next_spend_amount().
        "next_due": next_spend_amount(token, now, path, rng=lambda: 0.5,
                                      ledger_path=ledger_path),
    }
