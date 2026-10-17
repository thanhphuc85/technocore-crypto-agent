# SPDX-License-Identifier: MIT
"""flop_report — báo cáo CHI TIÊU FLOP định kỳ (thuần, không mạng).

Vì sao cần: thước đo phân bổ airdrop của agent là "compute purchased in settled sessions"
(flop.finance/testnet), nên "đã chi bao nhiêu" là con số DUY NHẤT thật sự tính điểm. Cảnh
báo hiện có chỉ chạy `if: failure()` — nó báo lúc HỎNG, không báo lúc CHẠY TỐT. Module này
gom sổ cái thành một bản tóm tắt để gửi đi (Telegram), tách bạch:

  - CHI (kind='spend')   -> tử số của chỉ số airdrop
  - NẠP (kind='credit')  -> faucet; TỰ NÓ không sinh phân bổ nào
  - mode                 -> 'simulation' hay 'testnet'. LUÔN in ra: một bản báo cáo
                            mô phỏng mà không ghi rõ là mô phỏng thì chính là số liệu
                            gây hiểu nhầm.

Thuần & test được: summarize/format_report không chạm mạng, không đọc env, không đọc file —
agent_cron lo phần đọc sổ cái, chặn thời gian, và gửi.
"""
from __future__ import annotations

import calendar
import re
import time
from decimal import Decimal, InvalidOperation

WINDOW_S = 24 * 3600


def _dec(v) -> Decimal:
    """Số thập phân an toàn: rác/None -> 0 (sổ cái là dữ liệu, không tin mù quáng)."""
    try:
        return Decimal(str(v if v not in (None, "") else "0"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _ts(v):
    """'2026-10-17T01:23:45Z' -> epoch giây. None nếu không đọc được."""
    if not isinstance(v, str):
        return None
    try:
        return calendar.timegm(time.strptime(v.strip(), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


_DISPLAY_DP = Decimal("0.000001")      # 6 chữ số thập phân là quá đủ để đọc


def _fmt(d: Decimal) -> str:
    """Số để ĐỌC: làm tròn 6 chữ số thập phân rồi bỏ 0 thừa ('12.4' chứ không '12.4000').

    Chỉ ảnh hưởng HIỂN THỊ. Sổ cái và pacer vẫn giữ Decimal đầy đủ — phép chia của
    pacer sinh ra những số như 95.90277777777777777777777778, không ai đọc nổi trong
    một tin nhắn, nhưng làm tròn ở tầng kế toán thì sẽ trôi số dư."""
    try:
        d = d.quantize(_DISPLAY_DP)
    except (InvalidOperation, ValueError):
        pass
    s = format(d.normalize(), "f")
    return s if s != "-0" else "0"


def summarize(ledger, now: int, token: str = "FLOP", window_s: int = WINDOW_S,
              mode: str = None) -> dict:
    """Gom sổ cái thành số liệu của CỬA SỔ `window_s` giây gần nhất + tổng tích luỹ.

    Entry thiếu/hỏng `ts` KHÔNG bị tính vào cửa sổ (không đoán thời điểm), nhưng VẪN
    vào tổng tích luỹ — nếu không, cắt log sẽ làm tổng tụt một cách âm thầm."""
    led = ledger if isinstance(ledger, dict) else {}
    entries = led.get("entries") or []
    cutoff = int(now) - max(0, int(window_s))
    out = {
        "token": token,
        "mode": mode or led.get("mode") or "?",
        "balance": _fmt(_dec((led.get("balances") or {}).get(token, "0"))),
        "spent": Decimal(0), "spend_count": 0,
        "credited": Decimal(0), "credit_count": 0,
        "spend_count_total": 0, "spent_total": Decimal(0),
    }
    for e in entries:
        if not isinstance(e, dict) or e.get("token") != token:
            continue
        kind, amt = e.get("kind"), _dec(e.get("amount"))
        if kind == "spend":
            out["spend_count_total"] += 1
            out["spent_total"] += amt
        ts = _ts(e.get("ts"))
        if ts is None or ts < cutoff:
            continue
        if kind == "spend":
            out["spent"] += amt
            out["spend_count"] += 1
        elif kind == "credit":
            out["credited"] += amt
            out["credit_count"] += 1
    for k in ("spent", "credited", "spent_total"):
        out[k] = _fmt(out[k])
    return out


_K_DELIVERED = re.compile(r"(\d+)\u2713")
_K_RATE = re.compile(r"useful_rate=(\d+%|n/a)")
_K_LIFETIME = re.compile(r"lifetime=(\d+)%/n=(\d+)")


def compact_kibble(status: str) -> str:
    """Rút dòng kibble dài cho TIN NHẮN; log Actions vẫn giữ nguyên bản đầy đủ.

    Bản đầy đủ ~140 ký tự nên trên điện thoại nó vỡ thành 4 dòng và chôn mất phần
    đáng đọc. Giữ đúng 3 số có ý nghĩa: đã giao, tỉ lệ cửa sổ, tỉ lệ cộng dồn.

      live 2✓/0skip/200scan | kibble-score delivered=300 useful=44 not=33
      useful_rate=57% pending=223 unattested=0 lifetime=58%/n=48
        -> 2✓ · useful 57% · lifetime 58% (n=48)

    KHÔNG nhận diện được mẫu nào -> trả NGUYÊN chuỗi: thà dài còn hơn mất thông tin
    (vd 'off', 'skip-outage', 'error' phải đi qua y nguyên)."""
    txt = str(status or "")
    parts = []
    m = _K_DELIVERED.search(txt)
    if m:
        # Giữ tiền tố 'dry': bỏ nó đi thì '3✓' trông như đã giao THẬT trong khi
        # dry-run không đăng gì cả — rút gọn không được phép đổi nghĩa.
        parts.append(("dry " if txt.strip().lower().startswith("dry") else "")
                     + f"{m.group(1)}✓")
    m = _K_RATE.search(txt)
    if m:
        parts.append(f"useful {m.group(1)}")
    m = _K_LIFETIME.search(txt)
    if m:
        parts.append(f"lifetime {m.group(1)}% (n={m.group(2)})")
    return " \u00b7 ".join(parts) if parts else txt


def spend_health(summary: dict, pacing: dict = None) -> dict:
    """Tín hiệu "có đang KIẾM HỤT không" — khác hẳn "đã chi bao nhiêu".

    Vì sao đây mới là chỉ số tối ưu hoá: trang chính thức ghi "Holding test tokens earns
    nothing", nên FLOP nằm im trong ví là **phân bổ đang mất**, không phải tài sản. Tổng
    đã chi chỉ kể quá khứ; cái cần biết là hôm nay còn dư bao nhiêu và nhịp chi có kịp
    tiêu hết trong ngày không.

    Trả {idle, idle_warn, pace, elapsed_pct, spent_pct, budget}. `pace`:
      off       — chưa đặt FLOP_DAILY_BUDGET (pacer tắt) -> không đánh giá nhịp
      behind    — chi chậm hơn mốc >10 điểm % -> nhiều khả năng dư token cuối ngày
      on-track  — trong khoảng ±10 điểm %
      ahead     — chi nhanh hơn mốc >10 điểm % (không phải lỗi, nhưng dễ cạn sớm)
    """
    s = summary if isinstance(summary, dict) else {}
    p = pacing if isinstance(pacing, dict) else {}
    idle = _dec(s.get("balance"))
    out = {"idle": _fmt(idle), "idle_warn": False, "pace": "off",
           "elapsed_pct": None, "spent_pct": None, "budget": None}
    budget = _dec(p.get("daily_budget")) if p.get("enabled") else Decimal(0)
    if p.get("enabled") and budget > 0:
        out["budget"] = _fmt(budget)
        elapsed = _dec(p.get("target_now")) / budget * 100
        spent = _dec(p.get("spent_today")) / budget * 100
        out["elapsed_pct"] = int(elapsed)
        out["spent_pct"] = int(spent)
        diff = spent - elapsed
        out["pace"] = "behind" if diff < -10 else ("ahead" if diff > 10 else "on-track")
    # Cảnh báo chỉ khi THẬT SỰ đang bỏ phí: còn token mà nhịp chi đang chậm, hoặc
    # pacer tắt và 24h qua không chi đồng nào. Không kêu khi chỉ đơn giản là còn số dư —
    # lúc nào cũng còn số dư, cảnh báo như vậy thành tiếng ồn.
    if idle > 0:
        if out["pace"] == "behind":
            out["idle_warn"] = True
        elif out["pace"] == "off" and int(s.get("spend_count", 0) or 0) == 0:
            out["idle_warn"] = True
    return out


def format_report(s: dict, *, date_label: str = None, extras: dict = None,
                  pacing: dict = None) -> str:
    """Một tin gọn để gửi Telegram. `extras` là các trường tuỳ chọn của agent
    (faucet/session/kibble...). Luôn nêu rõ mode."""
    s = s if isinstance(s, dict) else {}
    tok = s.get("token", "FLOP")
    head = f"Technocore agent — {date_label or time.strftime('%Y-%m-%d', time.gmtime())}"
    if (s.get("mode") or "") != "testnet":
        head += f"  [{s.get('mode', '?')} — KHÔNG phải chi tiêu thật]"
    lines = [
        head,
        f"chi 24h: {s.get('spent', '0')} {tok} ({s.get('spend_count', 0)} lần)",
        f"nạp 24h: {s.get('credited', '0')} {tok} ({s.get('credit_count', 0)} lần)",
        f"số dư: {s.get('balance', '0')} {tok}",
        f"tổng đã chi: {s.get('spent_total', '0')} {tok} "
        f"({s.get('spend_count_total', 0)} lần)",
    ]
    h = spend_health(s, pacing)
    if h["idle_warn"]:
        lines.append(f"⚠️ nhàn rỗi: {h['idle']} {tok} — giữ token KHÔNG sinh phân bổ")
    if h["pace"] != "off":
        label = {"behind": "ĐANG CHẬM (dễ dư cuối ngày)",
                 "on-track": "đúng nhịp",
                 "ahead": "nhanh hơn mốc (dễ cạn sớm)"}[h["pace"]]
        lines.append(f"nhịp: {h['elapsed_pct']}% ngày đã trôi · "
                     f"{h['spent_pct']}% ngân sách đã chi -> {label}")
    for k, v in (extras or {}).items():
        lines.append(f"{k}: {v}")
    return "\n".join(lines)
