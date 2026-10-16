"""Tests cho flop_kibble_track.py — tracker ATTEST 'useful' (thuần, không mạng)."""

import hashlib
import flop_kibble_track as kt

JID = "kabc1234567"
BODY = "a real technical   answer\nwith newlines"     # có whitespace để test one-line


def test_candidate_hashes_shape_and_normalize():
    c = kt.candidate_hashes(JID, BODY)
    # 4 thuật toán × 3 biến thể = 12 ứng viên, mỗi cái 16 hex
    assert len(c) == 12
    assert all(len(v) == 16 and all(ch in "0123456789abcdef" for ch in v) for v in c.values())
    # sha256/body tính trên bản one-lined (gộp whitespace)
    norm = " ".join(BODY.split())
    assert c["sha256/body"] == hashlib.sha256(norm.encode()).hexdigest()[:16]


def test_parse_attest_line():
    a = kt.parse_attest_line(f"ATTEST v1 | {JID} | useful | rh:0123456789abcdef | good work")
    assert a == {"jobid": JID, "verdict": "useful", "rh": "0123456789abcdef"}
    a2 = kt.parse_attest_line(f"ATTEST v1 | {JID} | not | thin boilerplate")   # không rh
    assert a2["verdict"] == "not" and a2["rh"] is None
    assert kt.parse_attest_line("DELIVER v1 | kabc1234567 | x") is None
    assert kt.parse_attest_line("ATTEST v1 | badid | useful") is None


def test_record_deliveries_dedupe_and_cap():
    st = {}
    kt.record_deliveries(st, [{"jobid": JID, "answer": "x"}], 1000)
    kt.record_deliveries(st, [{"jobid": JID, "answer": "y"}], 2000)   # cùng jobid -> ghi đè
    assert len(st["kibble_deliveries"]) == 1
    assert st["kibble_deliveries"][0]["ts"] == 2000
    kt.record_deliveries(st, [{"jobid": "kbadid", "answer": "z"}], 3000)  # jobid sai -> bỏ
    assert len(st["kibble_deliveries"]) == 1


def test_reconcile_useful_match_reveals_recipe():
    st = {}
    kt.record_deliveries(st, [{"jobid": JID, "answer": BODY}], 1000)
    rh = st["kibble_deliveries"][0]["cands"]["sha256/body"]     # attestor giả dùng sha256/body
    msgs = [{"text": f"ATTEST v1 | {JID} | useful | rh:{rh} | solid"}]
    sm = kt.reconcile(st, msgs, 2000)
    assert sm["useful"] == 1 and sm["not"] == 0
    assert sm["useful_rate"] == 1.0
    assert sm["recipe"] == ["sha256/body"]                     # LỘ thuật toán rh
    assert st["kibble_deliveries"][0]["status"] == "useful"
    assert "cands" not in st["kibble_deliveries"][0]           # đã chốt -> bỏ tập hash


def test_reconcile_not_verdict():
    st = {}
    kt.record_deliveries(st, [{"jobid": JID, "answer": BODY}], 1000)
    rh = st["kibble_deliveries"][0]["cands"]["md5/line"]
    msgs = [{"text": f"ATTEST v1 | {JID} | not | rh:{rh} | echoes the job"}]
    sm = kt.reconcile(st, msgs, 2000)
    assert sm["not"] == 1 and sm["useful"] == 0 and sm["useful_rate"] == 0.0


def test_reconcile_useful_no_rh_confirms_by_jobid():
    # board thật: verdict 'useful' KHÔNG kèm rh. 1 worker/job -> jobid đủ để chốt.
    st = {}
    kt.record_deliveries(st, [{"jobid": JID, "answer": BODY}], 1000)
    msgs = [{"text": f"ATTEST v1 | {JID} | useful | Comprehensive analysis, verified"}]
    sm = kt.reconcile(st, msgs, 2000)
    assert sm["useful"] == 1 and sm["pending"] == 0 and sm["useful_rate"] == 1.0
    assert st["kibble_deliveries"][0]["matched_by"] == "jobid"    # không rh -> quy kết bằng jobid


def test_reconcile_jobid_match_unknown_recipe():
    # rh có mặt nhưng không khớp tập ứng viên (recipe lạ) -> vẫn là job CỦA TA (1 worker/job),
    # chốt bằng jobid; recipe chưa lộ nên không set matched_recipe.
    st = {}
    kt.record_deliveries(st, [{"jobid": JID, "answer": BODY}], 1000)
    msgs = [{"text": f"ATTEST v1 | {JID} | not | rh:ffffffffffffffff | thin"}]
    sm = kt.reconcile(st, msgs, 2000)
    assert sm["not"] == 1 and sm["pending"] == 0
    assert st["kibble_deliveries"][0]["matched_by"] == "jobid"
    assert "matched_recipe" not in st["kibble_deliveries"][0]


def test_reconcile_majority_vote():
    # 2 'not' + 1 'useful' cho cùng jobid -> đa số 'not'.
    st = {}
    kt.record_deliveries(st, [{"jobid": JID, "answer": BODY}], 1000)
    msgs = [
        {"text": f"ATTEST v1 | {JID} | not | echoes the job"},
        {"text": f"ATTEST v1 | {JID} | not | boilerplate only"},
        {"text": f"ATTEST v1 | {JID} | useful | looks fine"},
    ]
    sm = kt.reconcile(st, msgs, 2000)
    assert sm["not"] == 1 and sm["useful"] == 0


def test_reconcile_tie_stays_pending():
    # hoà phiếu 1-1 -> chưa ngã ngũ, giữ pending, đếm ambiguous_now.
    st = {}
    kt.record_deliveries(st, [{"jobid": JID, "answer": BODY}], 1000)
    msgs = [
        {"text": f"ATTEST v1 | {JID} | useful | good"},
        {"text": f"ATTEST v1 | {JID} | not | bad"},
    ]
    sm = kt.reconcile(st, msgs, 2000)
    assert sm["pending"] == 1 and sm["useful"] == 0 and sm["not"] == 0
    assert sm["ambiguous_now"] == 1


def test_reconcile_expire_unattested():
    st = {}
    kt.record_deliveries(st, [{"jobid": JID, "answer": BODY}], 1000)
    sm = kt.reconcile(st, [], 1000 + 49 * 3600 * 1000)         # quá 48h, không attest
    assert sm["unattested"] == 1 and sm["pending"] == 0


def test_summary_line_format():
    st = {"kibble_deliveries": [
        {"jobid": "k0000000001", "status": "useful", "matched_recipe": "sha256/body"},
        {"jobid": "k0000000002", "status": "not", "matched_recipe": "sha256/body"},
        {"jobid": "k0000000003", "status": "pending"},
    ]}
    line = kt.summary_line(kt.summary(st))
    assert "delivered=3" in line and "useful=1" in line and "not=1" in line
    assert "useful_rate=50%" in line and "rh=sha256/body" in line


# --- bộ đếm CỘNG DỒN (lifetime): miễn nhiễm cửa sổ DELIVERIES_CAP ----------------------

def _settle(st, jid, verdict, ts=1000):
    """Giao 1 job rồi chốt nó bằng 1 ATTEST — dùng để tích bộ đếm cộng dồn."""
    kt.record_deliveries(st, [{"jobid": jid, "answer": BODY}], ts)
    return kt.reconcile(st, [{"text": f"ATTEST v1 | {jid} | {verdict} | x"}], ts + 1)


def test_lifetime_empty_state():
    lt = kt.lifetime({})
    assert lt == {"useful": 0, "not": 0, "n": 0, "rate": None}


def test_lifetime_counts_accumulate_across_reconciles():
    st = {}
    _settle(st, "k0000000001", "useful")
    _settle(st, "k0000000002", "useful")
    _settle(st, "k0000000003", "not")
    assert kt.lifetime(st) == {"useful": 2, "not": 1, "n": 3, "rate": 0.667}


def test_lifetime_bumps_once_per_delivery():
    # reconcile chạy lại với CÙNG attest -> bản đã chốt bị bỏ qua, không đếm thêm lần nữa
    st = {}
    _settle(st, "k0000000001", "useful")
    kt.reconcile(st, [{"text": "ATTEST v1 | k0000000001 | useful | x"}], 5000)
    kt.reconcile(st, [{"text": "ATTEST v1 | k0000000001 | useful | x"}], 9000)
    assert kt.lifetime(st)["useful"] == 1


def test_lifetime_survives_window_eviction():
    """Điểm cốt lõi: bản giao bị trần DELIVERIES_CAP đẩy ra -> useful_rate cửa sổ đổi,
    nhưng bộ đếm cộng dồn GIỮ nguyên lịch sử."""
    st = {}
    _settle(st, "k0000000001", "useful")
    # nhồi đầy trần bằng các bản pending -> bản đã chốt ở trên bị đẩy khỏi cửa sổ
    kt.record_deliveries(st, [{"jobid": "k%010x" % (i + 2), "answer": BODY}
                              for i in range(kt.DELIVERIES_CAP)], 2000)
    sm = kt.reconcile(st, [], 3000)
    assert sm["delivered"] == kt.DELIVERIES_CAP
    assert sm["useful"] == 0 and sm["useful_rate"] is None      # cửa sổ đã quên
    assert sm["lifetime_rate"] == 1.0 and sm["lt_n"] == 1       # cộng dồn thì không


def test_lifetime_ignores_unattested_and_ambiguous():
    st = {}
    # hoà phiếu -> chưa chốt; quá hạn không ai attest -> unattested. Cả hai KHÔNG vào bộ đếm.
    kt.record_deliveries(st, [{"jobid": "k0000000001", "answer": BODY}], 1000)
    kt.reconcile(st, [{"text": "ATTEST v1 | k0000000001 | useful | x"},
                      {"text": "ATTEST v1 | k0000000001 | not | x"}], 2000)
    kt.record_deliveries(st, [{"jobid": "k0000000002", "answer": BODY}], 1000)
    kt.reconcile(st, [], 1000 + 49 * 3600 * 1000)
    assert kt.lifetime(st)["n"] == 0


def test_lifetime_tolerates_garbage_in_state():
    assert kt.lifetime({"kibble_lifetime": "nope"})["n"] == 0
    assert kt.lifetime({"kibble_lifetime": {"useful": None, "not": "x"}})["n"] == 0
    assert kt.lifetime({"kibble_lifetime": {"useful": -5, "not": 2}}) == {
        "useful": 0, "not": 2, "n": 2, "rate": 0.0}


def test_merge_lifetime_takes_max_per_field():
    st = {"kibble_lifetime": {"useful": 10, "not": 3}}
    kt.merge_lifetime(st, {"useful": 4, "not": 9})          # KV cao hơn ở 'not'
    assert st["kibble_lifetime"] == {"useful": 10, "not": 9}


def test_merge_lifetime_restores_after_cache_loss():
    st = {}                                                  # state.json mất sạch
    kt.merge_lifetime(st, {"useful": 120, "not": 40})
    assert kt.lifetime(st)["n"] == 160
    kt.merge_lifetime(st, None)                              # KV rỗng -> không phá
    assert kt.lifetime(st)["n"] == 160


def test_summary_line_shows_lifetime():
    st = {}
    _settle(st, "k0000000001", "useful")
    _settle(st, "k0000000002", "not")
    _settle(st, "k0000000003", "useful")
    line = kt.summary_line(kt.reconcile(st, [], 9000))
    assert "lifetime=67%/n=3" in line


def test_summary_line_omits_lifetime_when_empty():
    assert "lifetime=" not in kt.summary_line(kt.summary({}))
