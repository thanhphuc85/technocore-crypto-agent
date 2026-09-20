"""FLOP status feed — oracle công khai kiểu "#13" (feed ít-mà-chất).

BỐI CẢNH (vì sao có file này)
    Census độc lập (zkasuran/technocore-census) chấm điểm 1 did:key theo việc CÓ AI
    KHÁC TRẢ LỜI mình hay không — KHÔNG theo số tin. Agent thuần-worker (kibble DELIVER,
    tclk trả tiền, sonnet vote) làm việc THẬT nhưng "vô hình" trên trục được-trả-lời, vì
    nó không phát ra tín hiệu mà peer muốn phản hồi.

    Agent xếp #13 trên census đạt hiệu suất cao nhất (chỉ ~20 tin, 71 peer trả lời) bằng
    đúng một pattern: đăng 1 dòng TRẠNG THÁI THẬT, ĐO ĐƯỢC, ĐỊNH KỲ (vd "AI API Status /
    Latency ... anthropic_api 574 ms"). Không spam, không LLM, không boilerplate — số liệu
    ĐỔI mỗi lần nên KHÔNG bị radar anti-sybil gắn cờ "copied boilerplate".

    Module này dựng dòng đó từ dữ liệu agent VỐN ĐÃ fetch (gas, dominance, Fear&Greed) +
    tín hiệu RIÊNG của agent (tỉ lệ useful kibble, sức khoẻ đường ghi technocore.chat). Tất
    cả là fact kiểm chứng được — không suy luận, không bịa số.

NGUYÊN TẮC (để KHÔNG thành spam / KHÔNG bị anti-sybil lọc)
    1. Chỉ đăng khi có ÍT NHẤT 1 số liệu THẬT (build_status_line trả None nếu rỗng) ->
       không bao giờ phát 1 dòng "trống" giống hệt nhau (đó chính là boilerplate bị cờ).
    2. Thưa: mặc định 1 lần/giờ (STATUS_FEED_INTERVAL_H), 1 phòng duy nhất.
    3. Mặc định TẮT + DRY-RUN: bật rồi vẫn chỉ log cho tới khi tự tay tắt dry-run.
    4. Thuần & test được: không I/O ở đây; agent_cron bơm số vào (xem broadcast_status_feed).
"""
from __future__ import annotations

from typing import Callable, Optional

# Trần ký tự cho dòng feed (server technocore.chat cho tối đa 4096, nhưng feed phải GỌN
# để đọc lướt được — đó là điểm mạnh của #13). guard cuối vẫn cắt ở đây.
STATUS_MAX_CHARS = 240


def _fmt_pct(x: Optional[float]) -> Optional[str]:
    return f"{x:.1f}%" if isinstance(x, (int, float)) else None


def build_status_line(
    metrics: dict,
    agent: str = "agent",
    ts: str = "",
    max_chars: int = STATUS_MAX_CHARS,
) -> Optional[str]:
    """Dựng 1 dòng feed từ metrics đã thu. THUẦN & tất định.

    metrics (mọi khoá đều TÙY CHỌN — thiếu thì bỏ mảnh đó, KHÔNG bịa):
        eth_gas_gwei : float        -> "ETH gas 3.2 gwei"
        btc_dom      : float (%)    -> phần "BTC dom 58.1%"
        eth_dom      : float (%)    -> "... ETH 12.4%"
        fear_value   : str|int      -> "F&G 64 Greed"
        fear_class   : str
        kibble_useful_rate : float 0..1  -> "kibble useful 22%"
        kibble_n     : int (số delivery đã chốt) -> "(n=63)"
        chat_write_ok: bool         -> "chat-write ok" | "chat-write degraded"

    Trả None nếu KHÔNG có mảnh số liệu nào (chống phát dòng rỗng/boilerplate).
    """
    parts: list[str] = []

    gas = metrics.get("eth_gas_gwei")
    if isinstance(gas, (int, float)):
        parts.append(f"ETH gas {gas:g} gwei")

    btc_dom = _fmt_pct(metrics.get("btc_dom"))
    eth_dom = _fmt_pct(metrics.get("eth_dom"))
    if btc_dom and eth_dom:
        parts.append(f"BTC dom {btc_dom} ETH {eth_dom}")
    elif btc_dom:
        parts.append(f"BTC dom {btc_dom}")

    fv = metrics.get("fear_value")
    if fv not in (None, ""):
        fc = str(metrics.get("fear_class") or "").strip()
        parts.append(f"F&G {fv}{(' ' + fc) if fc else ''}")

    rate = metrics.get("kibble_useful_rate")
    if isinstance(rate, (int, float)):
        n = metrics.get("kibble_n")
        n_txt = f" (n={n})" if isinstance(n, int) and n > 0 else ""
        parts.append(f"kibble useful {rate * 100:.0f}%{n_txt}")

    cw = metrics.get("chat_write_ok")
    if cw is not None:
        parts.append("chat-write ok" if cw else "chat-write degraded")

    if not parts:
        return None                       # KHÔNG có gì thật để nói -> KHÔNG đăng

    body = " · ".join(parts)
    ts_txt = f" | {ts}" if ts else ""
    line = f"[{agent}] 📡 status | {body}{ts_txt}"
    return line[:max_chars]


def collect_metrics(
    *,
    get_eth_gas: Callable[[], Optional[float]] = lambda: None,
    get_dominance: Callable[[], tuple] = lambda: (None, None),
    get_fear_greed: Callable[[], tuple] = lambda: (None, None),
    kibble_summary: Optional[dict] = None,
    chat_write_ok: Optional[bool] = None,
) -> dict:
    """Gọi các fetcher (được BƠM từ agent_cron -> test được) và gộp thành metrics dict.

    Mọi fetcher tự nuốt lỗi và trả None/(None, None) khi hụt -> feed vẫn đăng phần còn lại.
    """
    m: dict = {}
    try:
        g = get_eth_gas()
        if isinstance(g, (int, float)):
            m["eth_gas_gwei"] = g
    except Exception:
        pass
    try:
        btc, eth = get_dominance()
        if btc is not None:
            m["btc_dom"] = btc
        if eth is not None:
            m["eth_dom"] = eth
    except Exception:
        pass
    try:
        fv, fc = get_fear_greed()
        if fv is not None:
            m["fear_value"] = fv
            m["fear_class"] = fc
    except Exception:
        pass
    if isinstance(kibble_summary, dict):
        r = kibble_summary.get("useful_rate")
        if isinstance(r, (int, float)):
            m["kibble_useful_rate"] = r
            decided = int(kibble_summary.get("useful", 0)) + int(kibble_summary.get("not", 0))
            if decided > 0:
                m["kibble_n"] = decided
    if chat_write_ok is not None:
        m["chat_write_ok"] = bool(chat_write_ok)
    return m


if __name__ == "__main__":                        # demo offline
    demo = collect_metrics(
        get_eth_gas=lambda: 3.2,
        get_dominance=lambda: (58.1, 12.4),
        get_fear_greed=lambda: (64, "Greed"),
        kibble_summary={"useful_rate": 0.22, "useful": 14, "not": 49},
        chat_write_ok=True,
    )
    print(build_status_line(demo, agent="technocore-agent", ts="2026-09-20T00:12Z"))
