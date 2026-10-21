# SPDX-License-Identifier: MIT
"""flop_kibble_track — đo tỉ lệ ATTEST 'useful' của CHÍNH agent trên /r/kibble.

Vì sao cần: board /r/kibble chỉ phơi ra ~200 tin gần nhất (không phân trang lùi), nên
KHÔNG thể đo baseline từ bên ngoài — DELIVER của ta thưa (≤2/run) và cuộn mất trước khi
ATTEST tới. Điểm quan sát tin cậy DUY NHẤT là chính agent: nó biết chính xác nó đã bàn
giao job nào với nội dung gì.

Cách quy kết: theo JOBID. /r/kibble khoá 1 job cho 1 worker (đo live: mọi job chỉ có đúng
1 DELIVER), nên ATTEST cùng jobid CHẮC CHẮN nói về deliverable của ta — jobid là khoá không
mơ hồ. Nhiều ATTEST/1 jobid -> lấy đa số phiếu useful/not; hoà -> chưa chốt (giữ pending).
`rh:<hash16>` (nếu có) chỉ là tín hiệu CỘNG THÊM: khớp một ứng viên hash -> xác nhận danh
tính + LỘ recipe băm thật. QUAN TRỌNG: board thật BỎ `rh` ở verdict 'useful' (chỉ 'not' mới
kèm rh), nên KHÔNG được dùng rh làm cổng — làm vậy thì 'useful' không bao giờ chốt được (đó
là bug cũ khiến useful_rate kẹt mãi ở n/a). Ta vẫn tính sẵn tập hash ứng viên (candidate_hashes)
chỉ để học recipe khi rh tình cờ khớp.

Ngoài tỉ lệ theo CỬA SỔ (useful_rate, chỉ DELIVERIES_CAP bản gần nhất = vài giờ), module
còn giữ bộ đếm CỘNG DỒN (lifetime) để đọc chất lượng thật mà không bị chu kỳ ngày-đêm
của attestor làm nhiễu — xem lifetime().

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
LIFETIME_KEY = "kibble_lifetime"   # bộ đếm CỘNG DỒN (không theo cửa sổ) trong state
_LT_FIELDS = ("useful", "not")
# Bộ đếm "SẠCH" song song (bộ cũ GIỮ NGUYÊN để so với mốc cũ). Đo 2026-10-21 trên board:
#  - 37/37 job của ta CÒN bài của worker khác (bot nộp câu mẫu "Completed work on ...");
#    phiếu quy kết theo jobid nên chấm bài của họ bị tính vào ta: trong phiếu 'not' có rh,
#    chỉ 1/13 khớp bài của ta.
# Bộ sạch CHỈ bỏ phiếu 'not' có rh KHÔNG khớp bài của ta — bằng chứng chắc chắn phiếu đó
# chấm bài khác. KHÔNG lọc attester "chỉ chấm một loại": đã thử trên board thật và nó gạt cả
# người chấm nghiêm túc (board ~nửa là rác bot nên 20 phiếu 'not' liền nhau là bình thường)
# lẫn phiếu 'useful' — kết quả sai hơn bộ cũ.
# Còn lệch LÊN: phiếu 'useful' không kèm rh nên không tách được phiếu chấm bài người khác.
# So hai giai đoạn trên CÙNG bộ đếm thì độ lệch triệt tiêu; đọc con số tuyệt đối thì thận trọng.
CLEAN_KEY = "kibble_lifetime_clean"


def _as_count(v) -> int:
    """int không âm, chịu được None / chuỗi / rác trong state."""
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _bump_lifetime(state: dict, verdict: str, key: str = LIFETIME_KEY) -> None:
    """+1 cho bộ đếm cộng dồn khi MỘT delivery vừa chốt. Gọi đúng 1 lần/delivery vì
    reconcile chỉ xử lý bản 'pending' rồi đổi status ngay sau đó."""
    if verdict not in _LT_FIELDS:
        return
    lt = state.get(key)
    lt = dict(lt) if isinstance(lt, dict) else {}
    lt[verdict] = _as_count(lt.get(verdict)) + 1
    state[key] = lt


def lifetime(state: dict, key: str = LIFETIME_KEY) -> dict:
    """{useful, not, n, rate} từ bộ đếm cộng dồn. rate=None khi chưa có mẫu nào chốt.

    Vì sao cần: `useful_rate` ở summary() chỉ tính trên DELIVERIES_CAP bản gần nhất —
    với nhịp giao hiện tại đó là cửa sổ vài GIỜ, nên nó dao động theo việc attestor nào
    đang thức (có attestor chấm 'not' toàn tập). Bộ đếm này cộng dồn qua mọi cửa sổ nên
    phản ánh chất lượng THẬT theo thời gian, không bị chu kỳ ngày-đêm làm nhiễu."""
    lt = state.get(key)
    lt = lt if isinstance(lt, dict) else {}
    u, n = _as_count(lt.get("useful")), _as_count(lt.get("not"))
    tot = u + n
    return {"useful": u, "not": n, "n": tot,
            "rate": (round(u / tot, 3) if tot else None)}


def merge_lifetime(state: dict, remote, key: str = LIFETIME_KEY) -> None:
    """Hợp nhất bộ đếm từ nguồn khác (KV) vào state — lấy MAX từng ô vì đếm CHỈ TĂNG.
    Dùng khi cache runner mất state.json: không để bản rỗng/cũ nuốt mất số đã tích."""
    if not isinstance(remote, dict):
        return
    cur = state.get(key)
    cur = dict(cur) if isinstance(cur, dict) else {}
    for f in _LT_FIELDS:
        m = max(_as_count(cur.get(f)), _as_count(remote.get(f)))
        if m:
            cur[f] = m
    if cur:
        state[key] = cur


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
            "clean": "pending",                  # quyết định cho bộ đếm SẠCH (song song)
            "cands": candidate_hashes(jid, it.get("answer", "")),
        }
    merged = sorted(by_id.values(), key=lambda d: d.get("ts", 0))
    state["kibble_deliveries"] = merged[-DELIVERIES_CAP:]


def reconcile(state: dict, messages, now_ms: int, expire_ms: int = 48 * 3600 * 1000) -> dict:
    """Đối chiếu deliveries đang 'pending' với ATTEST trong `messages` (tại chỗ), quy kết theo
    JOBID. /r/kibble khoá 1 job cho 1 worker (đo live: 0 job có >1 DELIVER), nên ATTEST cùng
    jobid CHẮC CHẮN nói về deliverable CỦA TA — jobid là khoá quy kết không mơ hồ. `rh` chỉ là
    tín hiệu CỘNG THÊM (xác nhận danh tính + lộ recipe), KHÔNG phải cổng: board thật BỎ `rh` ở
    verdict 'useful' (chỉ 'not' mới kèm rh), nên gate-theo-rh khiến 'useful' không bao giờ chốt
    được. Nhiều ATTEST/1 jobid -> lấy đa số phiếu (useful vs not); HOÀ phiếu -> để 'pending'
    (chưa ngã ngũ). Pending không thấy attest quá `expire_ms` -> 'unattested'. Trả summary dict."""
    attests = []
    for m in (messages or []):
        a = parse_attest_line(str((m or {}).get("text", "")))
        if a:
            attests.append(a)
    by_job = {}
    for a in attests:
        by_job.setdefault(a["jobid"], []).append(a)

    unresolved = 0                               # có ATTEST nhưng hoà phiếu -> chưa chốt
    for d in state.get("kibble_deliveries", []):
        if not isinstance(d, dict):
            continue
        old_pending = d.get("status") == "pending"
        clean_pending = d.get("clean") == "pending"      # bản ghi cũ (không có khoá) -> bỏ
        if not (old_pending or clean_pending):
            continue
        job_atts = by_job.get(d.get("jobid"), [])
        if not job_atts:                         # chưa thấy attest nào cho jobid này
            if now_ms - d.get("ts", now_ms) > expire_ms:
                if old_pending:
                    d["status"] = "unattested"
                if clean_pending:
                    d["clean"] = "unattested"
                d.pop("cands", None)
            continue
        cand = d.get("cands") or {}
        cand_vals = set(cand.values())
        if old_pending:
            # rh khớp ứng viên -> lộ recipe thật + đánh dấu xác nhận bằng rh (bonus)
            for a in job_atts:
                if a["rh"] and a["rh"] in cand_vals:
                    for recipe, h in cand.items():
                        if h == a["rh"]:
                            d["matched_recipe"] = recipe
                            break
                    d["matched_by"] = "rh"
                    break
            # đa số phiếu trên MỌI attest của jobid (bộ đếm cũ — giữ nguyên để so mốc)
            useful_votes = sum(1 for a in job_atts if a["verdict"].startswith("useful"))
            not_votes = sum(1 for a in job_atts if a["verdict"].startswith("not"))
            if useful_votes == not_votes:        # hoà (kể cả 0-0: verdict lạ) -> chưa chốt
                unresolved += 1
            else:
                d["status"] = "useful" if useful_votes > not_votes else "not"
                _bump_lifetime(state, d["status"])
                d.setdefault("matched_by", "jobid")
        if clean_pending:
            votes = [a for a in job_atts                 # bỏ phiếu 'not' chấm bài KHÁC
                     if not (a["verdict"].startswith("not") and a["rh"]
                             and cand_vals and a["rh"] not in cand_vals)]
            cu = sum(1 for a in votes if a["verdict"].startswith("useful"))
            cn = sum(1 for a in votes if a["verdict"].startswith("not"))
            if cu != cn:
                d["clean"] = "useful" if cu > cn else "not"
                _bump_lifetime(state, d["clean"], CLEAN_KEY)
        if d.get("status") != "pending" and d.get("clean") != "pending":
            d.pop("cands", None)                  # cả hai đã chốt -> bỏ tập hash cho gọn
    return summary(state, ambiguous_now=unresolved)


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
    lt = lifetime(state)                      # cộng dồn: không bị cửa sổ vài giờ làm nhiễu
    c["lt_useful"], c["lt_not"], c["lt_n"] = lt["useful"], lt["not"], lt["n"]
    c["lifetime_rate"] = lt["rate"]
    cl = lifetime(state, CLEAN_KEY)           # SẠCH: bỏ phiếu 'not' chấm bài người khác
    c["clean_useful"], c["clean_not"], c["clean_n"] = cl["useful"], cl["not"], cl["n"]
    c["clean_rate"] = cl["rate"]
    # recipe đã học được (nếu có) — thuật toán rh thật, dùng cho các lần sau
    recipes = {d["matched_recipe"] for d in ds if d.get("matched_recipe")}
    c["recipe"] = sorted(recipes)
    return c


def summary_line(c: dict) -> str:
    """1 dòng gọn để publish (KV note / log)."""
    rate = "n/a" if c.get("useful_rate") is None else f"{c['useful_rate']*100:.0f}%"
    rec = ("|rh=" + ",".join(c["recipe"])) if c.get("recipe") else ""
    lt = ("" if c.get("lifetime_rate") is None else
          f" lifetime={c['lifetime_rate']*100:.0f}%/n={c.get('lt_n', 0)}")
    cl = ("" if c.get("clean_rate") is None else
          f" clean={c['clean_rate']*100:.0f}%/n={c.get('clean_n', 0)}")
    return (f"kibble-score delivered={c['delivered']} useful={c['useful']} not={c['not']} "
            f"useful_rate={rate} pending={c['pending']} unattested={c['unattested']}{lt}{cl}{rec}")


if __name__ == "__main__":                        # demo offline
    st = {}
    record_deliveries(st, [{"jobid": "kabc1234567", "answer": "a real technical answer"}], 1000)
    cands = st["kibble_deliveries"][0]["cands"]
    an_rh = cands["sha256/body"]                   # giả lập attestor dùng sha256/body
    msgs = [{"text": f"ATTEST v1 | kabc1234567 | useful | rh:{an_rh} | good"}]
    print(reconcile(st, msgs, 2000))
    print(summary_line(summary(st)))
