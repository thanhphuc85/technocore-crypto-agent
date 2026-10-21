# SPDX-License-Identifier: MIT
"""flop_governor — PHANH CỨNG số tin chat công khai theo phòng mỗi ngày UTC.

Vì sao cần: các lớp chống sybil "đặt luật trước" (jitter, organic-only, cooldown, xoay
câu chữ) KHÔNG ngăn được agent thành tài khoản đăng nhiều nhất phòng `flop` (608 tin/ngày
trong 15.136 DID, đo 2026-10-21). Nguyên nhân là một bug (#154: workflow_dispatch bị coi là
"chạy tay" nên telemetry đăng mỗi 5 phút bất chấp cấu hình 6 giờ) cộng hai tính năng chạy
hàng tuần với 0% kết quả — và KHÔNG có gì đo. Module này là lưới an toàn bắt được cả bug
chưa ai biết: vượt trần thì CHẶN và báo, thay vì đăng tiếp hàng tuần.

Mọi tin chat đi qua MỘT chỗ (agent_cron.post_message) nên chỉ cần gắn ở đó.
Thuần & test được: không mạng, không env — caller truyền trần + trạng thái vào.
"""
from __future__ import annotations

import time

KEY = "post_counts"
# Mức đo thật sau khi sửa (2026-10-21): flop ~28/ngày, kibble ~1.150/ngày, tclk 0.
# Trần có khoảng dư nhỏ — muốn tăng thì phải CỐ Ý đổi (FLOP_POST_CAPS), không lặng lẽ trôi.
DEFAULT_CAPS = {"flop": 40, "kibble": 1300, "tclk-offers": 50, "lobby": 20, "meta": 20, "*": 30}


def parse_caps(raw: str, base: dict = None) -> dict:
    """'flop=40,kibble=1300,*=30' -> dict, đè lên base. Mục rác bị bỏ qua (không làm hỏng
    phanh); số âm bị bỏ; 0 = chặn hẳn phòng đó."""
    caps = dict(base if base is not None else DEFAULT_CAPS)
    for part in (raw or "").split(","):
        if "=" not in part:
            continue
        room, _, val = part.partition("=")
        room = room.strip()
        try:
            n = int(val.strip())
        except ValueError:
            continue
        if room and n >= 0:
            caps[room] = n
    return caps


def cap_for(caps: dict, room: str) -> int:
    return int(caps.get(room, caps.get("*", DEFAULT_CAPS["*"])))


def _today(now) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(int(now)))


def _fresh(state: dict, now) -> dict:
    pc = state.get(KEY)
    pc = dict(pc) if isinstance(pc, dict) else {}
    if pc.get("day") != _today(now):                 # sang ngày UTC mới -> đếm lại từ 0
        pc = {"day": _today(now), "rooms": {}, "alerted": []}
    pc["rooms"] = {k: int(v) for k, v in (pc.get("rooms") or {}).items()
                   if isinstance(v, (int, float)) and v >= 0}
    pc["alerted"] = [r for r in (pc.get("alerted") or []) if isinstance(r, str)]
    state[KEY] = pc
    return pc


def check(state: dict, room: str, caps: dict, now) -> dict:
    """Được đăng thêm 1 tin vào `room` không? KHÔNG tăng đếm (chỉ tăng khi đăng thành công).
    Trả {allowed, count, cap, alert}: `alert` là chuỗi báo động khi phòng VỪA chạm trần lần
    đầu trong ngày (gửi đúng 1 lần/phòng/ngày), ngược lại None."""
    pc = _fresh(state, now)
    n, cap = int(pc["rooms"].get(room, 0)), cap_for(caps, room)
    if n < cap:
        return {"allowed": True, "count": n, "cap": cap, "alert": None}
    alert = None
    if room not in pc["alerted"]:
        pc["alerted"].append(room)
        alert = (f"⛔ Chặn đăng: phòng r/{room} đã đạt trần {cap} tin hôm nay ({pc['day']} UTC). "
                 f"Agent sẽ không đăng thêm vào phòng này tới hết ngày. Có thể có bug hoặc một "
                 f"tính năng đang đăng quá nhiều — xem log run (dòng [post]/[governor]).")
    return {"allowed": False, "count": n, "cap": cap, "alert": alert}


def record(state: dict, room: str, now) -> int:
    """Ghi 1 tin đã đăng THÀNH CÔNG. Trả số tin của phòng hôm nay."""
    pc = _fresh(state, now)
    pc["rooms"][room] = int(pc["rooms"].get(room, 0)) + 1
    return pc["rooms"][room]


def merge(state: dict, remote, now) -> None:
    """Hợp nhất bản đếm từ KV (cache runner mất): CÙNG ngày thì lấy MAX từng phòng — để mất
    cache không biến thành 'xoá đếm' rồi đăng lại từ 0. Khác ngày thì bỏ bản remote."""
    if not isinstance(remote, dict) or remote.get("day") != _today(now):
        return
    pc = _fresh(state, now)
    for room, v in (remote.get("rooms") or {}).items():
        if isinstance(v, (int, float)) and v > pc["rooms"].get(room, 0):
            pc["rooms"][room] = int(v)
    for r in remote.get("alerted") or []:
        if isinstance(r, str) and r not in pc["alerted"]:
            pc["alerted"].append(r)


def summary_line(state: dict, caps: dict, now) -> str:
    """'flop 12/40 · kibble 480/1300' — để in ra log/Telegram."""
    pc = _fresh(state, now)
    return " · ".join(f"{r} {n}/{cap_for(caps, r)}" for r, n in sorted(pc["rooms"].items()))
