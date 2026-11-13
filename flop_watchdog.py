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


_GATEWAY_NEUTRAL = r"gateway/(pace_wait|daily_cap|attribution_hold|overloaded|budget_spent)$"
_GATEWAY_HINT = "→ Xem log run, sửa adapter theo checklist ngày 1 trong STRATEGY.md."
_GATEWAY_RECOVERED = ("✅ TESTNET: agent đã có phiên settle lại sau khoảng {mins} phút gián đoạn (chưa phải bằng "
                      "chứng chain đã ghi cho ví mình).")


def observe(state: dict, *, settled_onchain: int, status: str, spent_today_onchain,
            now: int, balance=None, pending: int = 0, reason: str = "",
            alert_runs: int = 6, realert_sec: int = 6 * 3600, key: str = KEY,
            neutral: str = _GATEWAY_NEUTRAL, title: str = "TESTNET", hint: str = _GATEWAY_HINT,
            recovered: str = _GATEWAY_RECOVERED, pending_label: str = "phiên chờ proof",
            balance_label: str = "Số dư sổ") -> dict:
    """Ghi nhận MỘT run. Trả {"alert": str|None, "recovered": str|None}. `key` = khoá trạng thái (mỗi đường chi
    một khoá, không lẫn đếm); `neutral` = regex trạng thái "không chi nhưng đúng thiết kế" (không đếm, không xoá)."""
    w = state.get(key)
    w = dict(w) if isinstance(w, dict) else {}
    out = {"alert": None, "recovered": None}
    healthy_wait = str(status).endswith("pace-wait") and _num(spent_today_onchain) > 0
    # Đường gateway (flop_gateway): "chưa tới nhịp" là ĐÚNG THIẾT KẾ — phần lớn các lượt là chờ.
    # Trước đây status "…gateway/pace_wait" (gạch dưới) không khớp "pace-wait" -> mỗi lượt chờ bị
    # đếm là "không chi" -> báo động GIẢ sau 6 lượt, lặp mỗi 6h. Coi là TRUNG TÍNH: không đếm lỗi,
    # cũng KHÔNG xoá đếm — nếu settlement hỏng thật, các lượt mua (chưa settle) xen giữa lượt chờ
    # vẫn tích lại và báo động được. daily_cap (phanh operator đặt) và attribution_hold (có Telegram
    # riêng) cũng là trung tính. `exhausted` thì KHÔNG: OpenAPI /enroll/wallet ghi allowance "never
    # renewed", nên hết quota = ngừng mua vĩnh viễn trên đường này — trước đây im lặng mãi.
    # `overloaded` (gateway hết kênh với miner, flop-labs/network#31) và lượt nghỉ ngay sau nó cũng
    # trung tính: lỗi phía FLOP, agent không sửa được, và sẽ tự mua lại khi gateway thông.
    if settled_onchain <= 0 and (
            (neutral and re.search(neutral, str(status)))
            or (str(status).endswith("gateway/backoff") and "overloaded" in str(reason).lower())):
        return out
    if settled_onchain > 0 or healthy_wait:
        if w.get("alerted_ts") and settled_onchain > 0:
            mins = max(0, int((now - int(_num(w.get("since", now)))) / 60))
            # Gateway settle chưa chứng minh chi tiêu đứng tên ví; dòng "đứng tên ví" mới nói điều đó.
            out["recovered"] = recovered.format(mins=mins)
        state[key] = {}
        return out
    w["misses"] = int(_num(w.get("misses"))) + 1
    w.setdefault("since", int(now))
    bd = dict(w.get("breakdown") or {})
    sk = str(status or "?")[:60]
    bd[sk] = int(_num(bd.get(sk))) + 1
    if len(bd) > BREAKDOWN_CAP:
        bd = dict(sorted(bd.items(), key=lambda kv: -kv[1])[:BREAKDOWN_CAP])
    w["breakdown"] = bd
    w["last_reason"] = str(reason or "")[:200]
    last_alert = int(_num(w.get("alerted_ts")))
    if w["misses"] >= max(1, int(alert_runs)) and (not last_alert or now - last_alert >= realert_sec):
        mins = max(0, int((now - int(w["since"])) / 60))
        top = ", ".join(f"{k} ×{v}" for k, v in sorted(bd.items(), key=lambda kv: -kv[1])[:3])
        lines = [f"⚠️ {title}: {w['misses']} run liền (~{mins} phút) KHÔNG có phiên chi thật nào.",
                 f"Kẹt ở: {top}"]
        if w["last_reason"]:
            lines.append(f"Lý do gần nhất: {w['last_reason']}")
        lines.append(f"{balance_label}: {balance if balance is not None else '?'} FLOP · "
                     f"{pending_label}: {pending}")
        lines.append(hint)
        out["alert"] = "\n".join(lines)
        w["alerted_ts"] = int(now)
    state[key] = w
    return out


def unmark_alert(state: dict, key: str, prev) -> None:
    """Gửi cảnh báo THẤT BẠI: trả mốc "đã báo" về giá trị trước lượt này, để lượt sau (đủ ngưỡng) báo lại thay vì im
    tới hết realert_sec (tới 6 giờ). Đếm lượt trượt giữ nguyên."""
    w = state.get(key)
    if not isinstance(w, dict):
        return
    if prev:
        w["alerted_ts"] = prev
    else:
        w.pop("alerted_ts", None)


DIRECT_KEY = "real_spend_watch_direct"
# Mua trực tiếp: "đã mở phiên, chờ explorer ghi PAID" (opened), chạm nhịp trải đều (paced) và trần ngày
# (daily_cap, phanh operator đặt) là ĐÚNG THIẾT KẾ. Còn lại — low_balance (ví hết FLOP), no_offer (miner chưa/không
# còn offer), pending (đủ kênh chờ explorer), run_budget, backoff, hold_unknown, error, unconfigured... — kéo dài
# là không chi được: phải báo. "opened" mãi mà không PAID thì kênh chờ dồn tới FLOP_DIRECT_MAX_PENDING -> "pending".
_DIRECT_NEUTRAL = r"direct/(opened|paced|daily_cap)$"
_DIRECT_HINT = ("→ Xem dòng [direct] trong log run. low_balance = cần nhận faucet; no_offer = miner chưa/không còn "
                "công bố offer; pending = kênh chờ explorer ghi PAID quá nhiều.")


def observe_direct(state: dict, result: dict, *, now: int, network: str, balance=None,
                   alert_runs: int = 6, realert_sec: int = 6 * 3600) -> dict:
    """Bộ canh cho đường MUA TRỰC TIẾP (flop_direct): đếm lượt liền không có kênh nào PAID trên explorer
    (`settled` của run_step — không phụ thuộc ghi sổ, nên chạy cả khi tập trên devnet)."""
    r = result if isinstance(result, dict) else {}
    pend = len(((state.get("flop_direct") or {}).get("pending")) or [])
    return observe(state, settled_onchain=int(_num(r.get("settled"))), status=f"direct/{r.get('outcome') or '?'}",
                   spent_today_onchain=0, now=now, balance=balance, pending=pend,
                   reason=str(r.get("reason") or ""), alert_runs=alert_runs, realert_sec=realert_sec,
                   key=DIRECT_KEY, neutral=_DIRECT_NEUTRAL, title=f"MUA TRỰC TIẾP ({network})",
                   hint=_DIRECT_HINT, pending_label="kênh chờ explorer", balance_label="Số dư ví (explorer)",
                   recovered=("✅ MUA TRỰC TIẾP (" + network + "): đã có kênh đứng tên ví mình PAID lại sau khoảng "
                              "{mins} phút gián đoạn."))
