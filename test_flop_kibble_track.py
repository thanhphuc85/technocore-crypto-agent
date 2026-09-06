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


def test_reconcile_ambiguous_no_rh():
    st = {}
    kt.record_deliveries(st, [{"jobid": JID, "answer": BODY}], 1000)
    msgs = [{"text": f"ATTEST v1 | {JID} | useful | some note without rh"}]
    sm = kt.reconcile(st, msgs, 2000)
    # jobid khớp nhưng không rh -> KHÔNG chốt, đếm ambiguous, vẫn pending
    assert sm["useful"] == 0 and sm["pending"] == 1 and sm["ambiguous_now"] == 1


def test_reconcile_rh_of_other_worker_not_ours():
    st = {}
    kt.record_deliveries(st, [{"jobid": JID, "answer": BODY}], 1000)
    msgs = [{"text": f"ATTEST v1 | {JID} | useful | rh:ffffffffffffffff | attesting someone else"}]
    sm = kt.reconcile(st, msgs, 2000)
    # rh không khớp ứng viên nào của ta -> mơ hồ (là deliverable của worker khác), không tính useful
    assert sm["useful"] == 0 and sm["pending"] == 1 and sm["ambiguous_now"] == 1


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
