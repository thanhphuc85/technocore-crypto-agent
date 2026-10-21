# SPDX-License-Identifier: MIT
"""flop_miners — đo ĐỘ PHÂN TÁN thợ đào mà agent đã mua compute (thuần, không mạng).

Vì sao cần: luật airdrop ghi "Independent demand only — spend routed to a miner under
common control ... is not credited as demand". FLOP cho phép agent CHỌN thợ đào ("buyers
choose their miners" — FAQ testnet; "pick a capable miner" — trang agent), nhưng API trả
danh sách/giá/trạng thái thợ đào CHƯA công bố (docs.flop.network/networks/testnet:
"Unavailable"). Tới lúc đó scaffold để mempool ghép rồi trả `miner_did` về, nên việc làm
được là ĐO: mua của bao nhiêu người khác nhau, và phần tập trung nhất chiếm bao nhiêu.
Bộ chọn thợ đào: xem STRATEGY.md, checklist ngày 1, mục 7.

Nếu phần lớn chi tiêu rơi vào MỘT thợ đào, nó trông giống quan hệ cặp đôi kể cả khi ta
không hề chọn — và nếu không đo thì tới lúc bị hỏi sẽ không có gì để trả lời. Đếm số lần
BỊ CHẶN vì trúng miner fleet cũng ở đây: chốt chặn đó hiện chạy âm thầm.

Thuần & test được: mọi hàm nhận/trả dict, không đọc env, không mạng, không file.
"""
from __future__ import annotations

MINER_CAP = 200          # trần số thợ đào khác nhau giữ trong state (chống phình)


def _clean(stats) -> dict:
    """Chuẩn hoá state về {counts:{did:int}, denied:int}; rác -> rỗng."""
    s = stats if isinstance(stats, dict) else {}
    counts = s.get("counts")
    out = {}
    if isinstance(counts, dict):
        for k, v in counts.items():
            if not k or not isinstance(k, str):
                continue
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n > 0:
                out[k] = n
    try:
        denied = max(0, int(s.get("denied") or 0))
    except (TypeError, ValueError):
        denied = 0
    return {"counts": out, "denied": denied}


def record_settled(stats, miner_did: str) -> dict:
    """+1 cho thợ đào vừa settle. Trả dict MỚI (không sửa tại chỗ).

    Chạm trần MINER_CAP: chỉ từ chối thêm thợ đào MỚI, vẫn đếm tiếp người đã có — nếu
    không, trần sẽ âm thầm làm lệch tỉ lệ tập trung."""
    s = _clean(stats)
    did = (miner_did or "").strip()
    if not did:
        return s
    if did not in s["counts"] and len(s["counts"]) >= MINER_CAP:
        return s
    s["counts"][did] = s["counts"].get(did, 0) + 1
    return s


def record_denied(stats) -> dict:
    """+1 lần BỊ CHẶN vì mempool ghép trúng miner fleet nhà."""
    s = _clean(stats)
    s["denied"] += 1
    return s


def summarize(stats) -> dict:
    """{distinct, total, denied, top_did, top_share}. top_share là 0..1, None khi chưa
    có phiên nào (không bịa 0% — 0% và 'chưa có dữ liệu' là hai chuyện khác nhau)."""
    s = _clean(stats)
    total = sum(s["counts"].values())
    top_did, top_n = None, 0
    for did, n in sorted(s["counts"].items()):      # sorted -> tất định khi hoà
        if n > top_n:
            top_did, top_n = did, n
    return {"distinct": len(s["counts"]), "total": total, "denied": s["denied"],
            "top_did": top_did,
            "top_share": (top_n / total) if total else None}


def format_line(summ: dict) -> str:
    """Một dòng gọn cho tin nhắn."""
    s = summ if isinstance(summ, dict) else {}
    if not s.get("total"):
        return f"chưa có phiên nào · fleet bị chặn {s.get('denied', 0)}"
    share = s.get("top_share")
    return (f"{s['distinct']} người/{s['total']} phiên · "
            f"tập trung cao nhất {share * 100:.0f}% · "
            f"fleet bị chặn {s.get('denied', 0)}")


def concentration_warning(summ: dict, threshold: float = 0.5, min_total: int = 10):
    """Cảnh báo khi quá nhiều chi tiêu dồn vào một thợ đào. None nếu không đáng báo.

    `min_total` tránh kêu oan lúc mới chạy: 2 phiên cùng một người là 100% nhưng chẳng
    nói lên điều gì."""
    s = summ if isinstance(summ, dict) else {}
    share = s.get("top_share")
    if share is None or s.get("total", 0) < max(1, int(min_total)):
        return None
    if share < threshold:
        return None
    return (f"{share * 100:.0f}% chi tiêu dồn vào 1 thợ đào "
            f"({str(s.get('top_did'))[:20]}…) — rủi ro bị đọc là quan hệ cặp đôi")
