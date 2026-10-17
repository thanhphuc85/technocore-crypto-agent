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


def _fmt(d: Decimal) -> str:
    """Bỏ số 0 thừa ở đuôi, giữ dạng người đọc được ('12.4' chứ không '12.4000')."""
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


def format_report(s: dict, *, date_label: str = None, extras: dict = None) -> str:
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
    for k, v in (extras or {}).items():
        lines.append(f"{k}: {v}")
    return "\n".join(lines)
