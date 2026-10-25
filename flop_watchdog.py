# SPDX-License-Identifier: MIT
"""flop_watchdog — canh CHI THẬT trên testnet: báo động khi agent KHÔNG chi được on-chain.

Vì sao cần: đường chi thật (mempool -> proof -> settle) chưa từng chạy trên mạng thật; nó
dựng trên giả định về API mà FLOP chưa công bố. Thử 2 kịch bản sát tài liệu nhất (proof
đến SAU; proof đúng thuật ngữ tài liệu) đều ra 0 giao dịch thật — và dòng status chỉ hiện
"0✓ await_poui/pending" hay "0✓ verify/disputed", KHÔNG có báo động. Lỗi ngày đầu testnet
có thể ăn mất nhiều ngày của "low-supply months". Module này biến im lặng thành tin nhắn.

Một run là "trượt" khi không có phiên nào spent_onchain, TRỪ khi đang chờ nhịp hợp lệ
(pace-wait mà hôm nay đã có chi thật — pacer đi trước mục tiêu là bình thường). Đủ
ALERT_RUNS run trượt liên tiếp -> báo; còn trượt thì nhắc lại sau REALERT_SEC; chi thật
lại được -> báo đã hồi phục.

Thuần & test được: không mạng, không file, không env — caller truyền cấu hình vào.
"""
from __future__ import annotations

import re

KEY = "real_spend_watch"
PENDING_KEY = "flop_pending_sessions"
PENDING_CAP = 200
PENDING_TTL_SEC = 48 * 3600
BREAKDOWN_CAP = 10


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def note_pending(state: dict, result: dict, now: int) -> int:
    """Lưu phiên đã được nhận nhưng CHƯA có proof (stage await_poui) để không bỏ lửng;
    bỏ mục quá PENDING_TTL_SEC, giữ tối đa PENDING_CAP. Trả số phiên đang chờ."""
    items = [p for p in (state.get(PENDING_KEY) or [])
             if isinstance(p, dict) and now - int(_num(p.get("ts"))) <= PENDING_TTL_SEC]
    r = result or {}
    if r.get("stage") == "await_poui" and r.get("outcome") == "pending":
        items.append({"request_id": r.get("request_id"), "session_id": r.get("session_id"),
                      "miner_did": r.get("miner_did"), "ts": int(now)})
    state[PENDING_KEY] = items[-PENDING_CAP:]
    return len(state[PENDING_KEY])


DEFAULT_BLOCK_SEC = 6 * 3600


def pending_block(state: dict, now: int, cap: int, window_sec: int = DEFAULT_BLOCK_SEC) -> tuple:
    """(chặn?, số phiên chờ proof trong cửa sổ). Phiên chờ proof có thể ĐÃ khoá phí (escrow)
    trên chain mà sổ cái chưa ghi -> pacer tưởng chưa chi và cứ đăng phiên mới mỗi run. Đủ
    `cap` phiên chờ TRONG `window_sec` gần nhất thì dừng đăng. Cửa sổ ngắn (mặc định 6h, KHÔNG
    phải TTL 48h của danh sách) để chặn không kéo dài nhiều ngày; danh sách vẫn giữ 48h cho
    việc đối soát sau. cap<=0 = tắt."""
    note_pending(state, {}, now)              # chỉ dọn mục quá hạn, không thêm gì
    recent = [p for p in (state.get(PENDING_KEY) or [])
              if now - int(_num(p.get("ts"))) <= max(0, int(window_sec))]
    n = len(recent)
    return (cap > 0 and n >= cap), n


def observe(state: dict, *, settled_onchain: int, status: str, spent_today_onchain,
            now: int, balance=None, pending: int = 0, reason: str = "",
            alert_runs: int = 6, realert_sec: int = 6 * 3600) -> dict:
    """Ghi nhận MỘT run. Trả {"alert": str|None, "recovered": str|None}."""
    w = state.get(KEY)
    w = dict(w) if isinstance(w, dict) else {}
    out = {"alert": None, "recovered": None}
    healthy_wait = str(status).endswith("pace-wait") and _num(spent_today_onchain) > 0
    # Đường gateway (flop_gateway): "chưa tới nhịp" và "đã chi hết quota" là ĐÚNG THIẾT KẾ — với
    # faucet 25 FLOP/ngày phần lớn các lượt là chờ. Trước đây status "…gateway/pace_wait" (gạch
    # dưới) không khớp "pace-wait" -> mỗi lượt chờ bị đếm là "không chi" -> báo động GIẢ sau 6
    # lượt, lặp mỗi 6h. Coi là TRUNG TÍNH: không đếm lỗi, cũng KHÔNG xoá đếm — nếu settlement hỏng
    # thật, các lượt mua (chưa settle) xen giữa lượt chờ vẫn tích lại và báo động được.
    if settled_onchain <= 0 and re.search(r"gateway/(pace_wait|exhausted)$", str(status)):
        return out
    if settled_onchain > 0 or healthy_wait:
        if w.get("alerted_ts") and settled_onchain > 0:
            mins = max(0, int((now - int(_num(w.get("since", now)))) / 60))
            out["recovered"] = (f"✅ TESTNET: agent đã CHI THẬT lại (spent_onchain) sau khoảng "
                                f"{mins} phút gián đoạn.")
        state[KEY] = {}
        return out
    w["misses"] = int(_num(w.get("misses"))) + 1
    w.setdefault("since", int(now))
    bd = dict(w.get("breakdown") or {})
    key = str(status or "?")[:60]
    bd[key] = int(_num(bd.get(key))) + 1
    if len(bd) > BREAKDOWN_CAP:
        bd = dict(sorted(bd.items(), key=lambda kv: -kv[1])[:BREAKDOWN_CAP])
    w["breakdown"] = bd
    w["last_reason"] = str(reason or "")[:200]
    last_alert = int(_num(w.get("alerted_ts")))
    if w["misses"] >= max(1, int(alert_runs)) and (not last_alert or now - last_alert >= realert_sec):
        mins = max(0, int((now - int(w["since"])) / 60))
        top = ", ".join(f"{k} ×{v}" for k, v in sorted(bd.items(), key=lambda kv: -kv[1])[:3])
        lines = [f"⚠️ TESTNET: {w['misses']} run liền (~{mins} phút) KHÔNG có phiên chi thật nào.",
                 f"Kẹt ở: {top}"]
        if w["last_reason"]:
            lines.append(f"Lý do gần nhất: {w['last_reason']}")
        lines.append(f"Số dư sổ: {balance if balance is not None else '?'} FLOP · "
                     f"phiên chờ proof: {pending}")
        lines.append("→ Xem log run, sửa adapter theo checklist ngày 1 trong STRATEGY.md.")
        out["alert"] = "\n".join(lines)
        w["alerted_ts"] = int(now)
    state[KEY] = w
    return out
