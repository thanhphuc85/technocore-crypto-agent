# SPDX-License-Identifier: MIT
"""flop_outcomes — tính năng nào ĐĂNG TIN thì phải có KẾT QUẢ; không có thì tự tạm dừng.

Vì sao cần (đo 2026-10-21): tclk gửi ~1.728 ACCEPT/ngày mà 0 lần payer khoá tiền suốt >=2
ngày; engage hỏi 823 agent lạ trong 2,8 ngày, 3 người trả lời (0,4%). Cả hai chạy hàng tuần
vì không gì đo kết quả. "Rải nhiều, không ai đáp" là đúng mẫu bộ lọc spam bắt — và nếu sau
này bật lại (vd FLOP tính giao dịch giữa các agent), lớp này để nó không lặp lại.

Cũng chứa phép đo DẤU CHÂN cho báo cáo ngày: thị phần của ta trong N tin gần nhất mỗi phòng.

Thuần & test được: không mạng, không env.
"""
from __future__ import annotations

import time

KEY = "feature_outcomes"
# Ngưỡng mặc định: đủ lượt để kết luận, và tỉ lệ thấp hơn mức bình thường của board nhiều lần
# (board tclk: 15,8% accept được khoá tiền; engage lành mạnh phải được trả lời vài %).
DEFAULTS = {
    "tclk": {"min_actions": 200, "min_rate": 0.01},
    "engage": {"min_actions": 100, "min_rate": 0.02},
}
PAUSE_SEC = 7 * 86400
REPLY_WINDOW_SEC = 6 * 3600


def _feat(state: dict, name: str) -> dict:
    allf = state.get(KEY)
    allf = dict(allf) if isinstance(allf, dict) else {}
    f = allf.get(name)
    f = dict(f) if isinstance(f, dict) else {}
    for k in ("actions", "wins", "paused_until"):
        try:
            f[k] = int(f.get(k) or 0)
        except (TypeError, ValueError):
            f[k] = 0
    allf[name] = f
    state[KEY] = allf
    return f


def record(state: dict, name: str, actions: int = 0, wins: int = 0) -> dict:
    f = _feat(state, name)
    f["actions"] += max(0, int(actions or 0))
    f["wins"] += max(0, int(wins or 0))
    state[KEY][name] = f
    return f


def paused_until(state: dict, name: str, now) -> int:
    """>0 = đang tạm dừng tới thời điểm này (epoch); 0 = được chạy."""
    until = _feat(state, name)["paused_until"]
    return until if until > int(now) else 0


def evaluate(state: dict, name: str, now, min_actions: int = None, min_rate: float = None,
             pause_sec: int = PAUSE_SEC) -> str:
    """Đủ `min_actions` lượt thì chấm: tỉ lệ kết quả < `min_rate` -> TẠM DỪNG `pause_sec` và
    trả chuỗi báo động; đạt thì mở cửa sổ đo mới. Chưa đủ lượt -> None."""
    d = DEFAULTS.get(name, {})
    min_actions = int(min_actions if min_actions is not None else d.get("min_actions", 100))
    min_rate = float(min_rate if min_rate is not None else d.get("min_rate", 0.02))
    f = _feat(state, name)
    if f["actions"] < max(1, min_actions):
        return None
    rate = f["wins"] / f["actions"]
    acts, wins = f["actions"], f["wins"]
    f["actions"] = f["wins"] = 0                      # cửa sổ đo mới
    alert = None
    if rate < min_rate:
        f["paused_until"] = int(now) + int(pause_sec)
        until = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(f["paused_until"]))
        alert = (f"⏸ Tự TẠM DỪNG '{name}': {acts} lượt mà chỉ {wins} kết quả ({rate:.1%}), "
                 f"dưới ngưỡng {min_rate:.0%}. Dừng tới {until}. Kiểu 'rải nhiều, không ai đáp' "
                 f"là đúng mẫu bộ lọc spam bắt — xem lại tính năng trước khi bật tiếp.")
    state[KEY][name] = f
    return alert


def new_engage_replies(messages, engaged: dict, mention_tokens, now,
                       counted: dict, window_s: int = REPLY_WINDOW_SEC) -> list:
    """DID đã được engage hỏi (trong `window_s`) vừa ĐĂNG tin nhắc tới ta -> một lượt trả lời.
    `counted` {did: ts} để mỗi người chỉ tính 1 lần/lượt hỏi; mục quá hạn bị dọn. Trả DID mới."""
    toks = [t.lower() for t in (mention_tokens or []) if t]
    for d in [d for d, ts in counted.items() if now - int(ts or 0) > window_s]:
        counted.pop(d, None)
    out = []
    for m in messages or []:
        who = (m or {}).get("from")
        asked = (engaged or {}).get(who)
        if not who or who in counted or not asked:
            continue
        try:
            if now - int(asked) > window_s:
                continue
        except (TypeError, ValueError):
            continue
        text = str((m or {}).get("text", "")).lower()
        if any(t in text for t in toks):
            counted[who] = int(now)
            out.append(who)
    return out


def room_share(messages, my_did: str) -> tuple:
    """(số tin của ta, tổng số tin) trong lô tin gần nhất của một phòng."""
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    return sum(1 for m in msgs if m.get("from") == my_did), len(msgs)


def footprint_line(counts: dict, caps: dict, shares: dict, warn_share: float = 0.05) -> tuple:
    """Dòng 'dấu chân' cho báo cáo ngày + cảnh báo nếu thị phần phòng nào vượt `warn_share`.
    counts {room: n hôm nay}, caps {room: trần}, shares {room: (mine, total)}."""
    parts, warns = [], []
    for room in sorted(set(counts) | set(shares)):
        n = int(counts.get(room, 0))
        cap = caps.get(room, caps.get("*"))
        seg = f"{room} {n}" + (f"/{cap}" if cap is not None else "")
        mine, total = shares.get(room, (0, 0))
        if total:
            pct = mine / total
            seg += f" ({pct:.1%} phòng)"
            if pct > warn_share:
                warns.append(f"r/{room}: ta chiếm {pct:.1%} trong {total} tin gần nhất (> {warn_share:.0%})")
        parts.append(seg)
    return " · ".join(parts), ("; ".join(warns) or None)
