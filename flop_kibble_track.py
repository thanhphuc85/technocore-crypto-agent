# SPDX-License-Identifier: MIT
"""flop_kibble_track — đo tỉ lệ ATTEST 'useful' của CHÍNH agent trên /r/kibble.

Vì sao cần: board /r/kibble chỉ phơi ra ~200 tin gần nhất (không phân trang lùi), nên
KHÔNG thể đo baseline từ bên ngoài — DELIVER của ta thưa (≤2/run) và cuộn mất trước khi
ATTEST tới. Điểm quan sát tin cậy DUY NHẤT là chính agent: nó biết chính xác nó đã bàn
giao job nào với nội dung gì.

Cách quy kết chính xác (không nhầm sang worker khác): ATTEST mang `rh:<hash>` (16 hex =
8 byte truncated) băm deliverable. Spec cách băm KHÔNG công khai, nên lúc bàn giao ta tính
sẵn MỘT TẬP hash ứng viên (nhiều thuật toán × nhiều cách mã hoá). Khi một ATTEST cho jobid
của ta có `rh` KHỚP một ứng viên -> chắc chắn là deliverable CỦA TA (useful/not), và recipe
khớp LỘ RA thuật toán thật (các lần sau khớp thẳng). ATTEST không kèm `rh` -> chỉ khớp
jobid = mơ hồ (nhiều worker/1 job) -> KHÔNG tính vào confirmed, đếm riêng.

Thuần & test được: candidate_hashes / parse_attest_line / reconcile / summary là hàm THUẦN
(không mạng). agent_cron lo phần đọc ATTEST + ghi state + publish KV note.
"""
from __future__ import annotations

import hashlib
import re

RH_LEN = 16                       # rh quan sát được = 16 hex (8 byte truncated)
DELIVERIES_CAP = 300              # trần số delivery giữ trong state (chống phình)
_RH_RE = re.compile(r"rh:([0-9a-fA-F]{%d})" % RH_LEN)
_ALGOS = ("sha256", "sha1", "md5", "sha3_256")
_JOBID_RE = re.compile(r"^k[0-9a-f]{10}$")


def _one_line(s: str) -> str:
    """Chuẩn hoá như worker post (gộp whitespace) — attestor thấy đúng dạng này."""
    return " ".join((s or "").split())


def candidate_hashes(jobid: str, body: str) -> dict:
    """Trả {recipe: hash16} — tập rh ứng viên cho deliverable của ta. Nhiều thuật toán ×
    nhiều cách mã hoá (body / dòng đầy đủ / jobid|body), lấy 16 hex đầu. Lần khớp đầu tiên
    lộ recipe thật; sau đó vẫn khớp bằng cùng tập nên không cần biết trước."""
    b = _one_line(body)
    variants = {
        "body": b,
        "line": f"DELIVER v1 | {jobid} | {b}",
        "jid|body": f"{jobid}|{b}",
    }
    out = {}
    for vn, v in variants.items():
        raw = v.encode("utf-8")
        for an in _ALGOS:
            h = hashlib.new(an, raw).hexdigest()
            out[f"{an}/{vn}"] = h[:RH_LEN]
    return out


def parse_attest_line(text: str):
    """'ATTEST v1 | <jobid> | <verdict> | [rh:<hash>] | ...' -> {jobid, verdict, rh|None}.
    None nếu không phải ATTEST hợp lệ. verdict chuẩn hoá lowercase ('useful' / 'not' / ...)."""
    if not isinstance(text, str) or not text.startswith("ATTEST v1 | "):
        return None
    parts = [p.strip() for p in text.split(" | ")]
    if len(parts) < 3:
        return None
    jobid = parts[1]
    if not _JOBID_RE.match(jobid):
        return None
    verdict = parts[2].lower()
    m = _RH_RE.search(text)
    return {"jobid": jobid, "verdict": verdict, "rh": (m.group(1).lower() if m else None)}


def record_deliveries(state: dict, items, now_ms: int) -> None:
    """Ghi các deliver THẬT vào state['kibble_deliveries'] (tại chỗ). items = iterable của
    {'jobid','answer'}. Dedupe theo jobid (giữ bản mới), cap kích thước."""
    store = state.get("kibble_deliveries", [])
    by_id = {d["jobid"]: d for d in store if isinstance(d, dict) and d.get("jobid")}
    for it in (items or []):
        jid = (it or {}).get("jobid")
        if not jid or not _JOBID_RE.match(jid):
            continue
        by_id[jid] = {
            "jobid": jid,
            "ts": now_ms,
            "status": "pending",
            "cands": candidate_hashes(jid, it.get("answer", "")),
        }
    merged = sorted(by_id.values(), key=lambda d: d.get("ts", 0))
    state["kibble_deliveries"] = merged[-DELIVERIES_CAP:]


def reconcile(state: dict, messages, now_ms: int, expire_ms: int = 48 * 3600 * 1000) -> dict:
    """Đối chiếu deliveries đang 'pending' với ATTEST trong `messages` (tại chỗ). Với mỗi
    pending: nếu có ATTEST cùng jobid mà rh KHỚP một ứng viên -> status 'useful'/'not' +
    ghi recipe khớp; ATTEST không rh cùng jobid -> đếm 'ambiguous' (KHÔNG chốt). Pending quá
    hạn (không thấy attest) -> 'unattested'. Trả summary dict."""
    attests = []
    for m in (messages or []):
        a = parse_attest_line(str((m or {}).get("text", "")))
        if a:
            attests.append(a)
    by_job = {}
    for a in attests:
        by_job.setdefault(a["jobid"], []).append(a)

    ambiguous = 0
    for d in state.get("kibble_deliveries", []):
        if not isinstance(d, dict) or d.get("status") != "pending":
            continue
        cand_vals = set((d.get("cands") or {}).values())
        matched = None
        saw_jobid = False
        for a in by_job.get(d.get("jobid"), []):
            saw_jobid = True
            if a["rh"] and a["rh"] in cand_vals:
                matched = a
                break
        if matched:
            d["status"] = "useful" if matched["verdict"].startswith("useful") else "not"
            for recipe, h in (d.get("cands") or {}).items():
                if h == matched["rh"]:
                    d["matched_recipe"] = recipe
                    break
            d.pop("cands", None)                 # đã chốt -> bỏ tập hash cho gọn state
        elif saw_jobid:
            ambiguous += 1                       # có attest jobid nhưng không rh khớp -> mơ hồ
        elif now_ms - d.get("ts", now_ms) > expire_ms:
            d["status"] = "unattested"
            d.pop("cands", None)
    return summary(state, ambiguous_now=ambiguous)


def summary(state: dict, ambiguous_now: int = 0) -> dict:
    """Tổng hợp counts + tỉ lệ useful trên các delivery ĐÃ CHỐT (useful+not)."""
    ds = [d for d in state.get("kibble_deliveries", []) if isinstance(d, dict)]
    c = {"delivered": len(ds), "useful": 0, "not": 0, "pending": 0, "unattested": 0}
    for d in ds:
        st = d.get("status", "pending")
        c[st] = c.get(st, 0) + 1
    decided = c["useful"] + c["not"]
    c["useful_rate"] = round(c["useful"] / decided, 3) if decided else None
    c["ambiguous_now"] = ambiguous_now
    # recipe đã học được (nếu có) — thuật toán rh thật, dùng cho các lần sau
    recipes = {d["matched_recipe"] for d in ds if d.get("matched_recipe")}
    c["recipe"] = sorted(recipes)
    return c


def summary_line(c: dict) -> str:
    """1 dòng gọn để publish (KV note / log)."""
    rate = "n/a" if c.get("useful_rate") is None else f"{c['useful_rate']*100:.0f}%"
    rec = ("|rh=" + ",".join(c["recipe"])) if c.get("recipe") else ""
    return (f"kibble-score delivered={c['delivered']} useful={c['useful']} not={c['not']} "
            f"useful_rate={rate} pending={c['pending']} unattested={c['unattested']}{rec}")


if __name__ == "__main__":                        # demo offline
    st = {}
    record_deliveries(st, [{"jobid": "kabc1234567", "answer": "a real technical answer"}], 1000)
    cands = st["kibble_deliveries"][0]["cands"]
    an_rh = cands["sha256/body"]                   # giả lập attestor dùng sha256/body
    msgs = [{"text": f"ATTEST v1 | kabc1234567 | useful | rh:{an_rh} | good"}]
    print(reconcile(st, msgs, 2000))
    print(summary_line(summary(st)))
