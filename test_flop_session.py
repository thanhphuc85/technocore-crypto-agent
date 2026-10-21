"""
Test cho flop_session.py — mua suy luận (đường earn airdrop chính, 3:1).
Sổ cái dùng file tạm (path=). KHÔNG chạm mạng: simulation dùng thợ đào giả; testnet dùng
submit_fn/submit_tx được tiêm.
"""
import random as _random

import flop_session as fsess
import token_manager as tm


def _ledger(tmp_path):
    return str(tmp_path / "ledger.json")


def test_build_request_has_five_fields_and_stable_id():
    r1 = fsess.build_request("abcd" * 8, 2000, 1_000_000, ["confidential"], "0.05",
                             did="did:key:zX", nonce="111")
    for k in ("model_hash", "max_latency_ms", "flops", "security_flags", "fee"):
        assert k in r1
    assert r1["flops"] == 1_000_000 and r1["fee"] == "0.05"
    r2 = fsess.build_request("abcd" * 8, 2000, 1_000_000, ["confidential"], "0.05",
                             did="did:key:zX", nonce="111")
    assert r1["id"] == r2["id"]                       # cùng canonical -> cùng id
    r3 = fsess.build_request("abcd" * 8, 2000, 1_000_000, ["confidential"], "0.05",
                             did="did:key:zX", nonce="222")
    assert r3["id"] != r1["id"]                       # nonce khác -> id khác


def test_build_request_rejects_bad_input():
    import pytest
    with pytest.raises(ValueError):
        fsess.build_request("", 2000, 1000, [], "0.01")
    with pytest.raises(ValueError):
        fsess.build_request("h", 2000, 0, [], "0.01")     # flops <= 0
    with pytest.raises(ValueError):
        fsess.build_request("h", 0, 1000, [], "0.01")     # latency <= 0


def test_verify_poui_linkage():
    req = fsess.build_request("h" * 8, 1000, 1000, [], "0.01", nonce="1")
    good = {"session_id": req["id"], "miner_did": "did:key:zM",
            "commitment": "c0ffee", "miner_sig": "sig"}
    assert fsess.verify_poui(req, good)["ok"] is True
    assert fsess.verify_poui(req, {**good, "session_id": "other"})["ok"] is False   # lệch phiên
    assert fsess.verify_poui(req, {**good, "commitment": ""})["ok"] is False         # thiếu commit
    assert fsess.verify_poui(req, None)["ok"] is False


def test_run_session_simulation_happy_path(tmp_path):
    p = _ledger(tmp_path)
    tm.credit("10", path=p)
    out = fsess.run_inference_session("a" * 16, 2000, 1_000_000_000, ["confidential"],
                                      "0.05", path=p)
    assert out["stage"] == "settle" and out["outcome"] == "spent_simulated"
    assert out["verified"] is True and out["fee"] == "0.05"
    # Simulation KHÔNG tích lũy mở khóa 3:1 (chỉ chi THẬT mới tính).
    assert tm.unlock_status(path=p)["spent_testnet"] == "0"


def test_run_session_disputes_on_bad_poui(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    tm.credit("10", path=p)
    monkeypatch.setattr(fsess, "verify_poui", lambda req, poui, *a: {"ok": False, "reason": "sai"})
    out = fsess.run_inference_session("a" * 16, 2000, 1000, [], "0.05", path=p)
    assert out["stage"] == "verify" and out["outcome"] == "disputed"
    # Khiếu nại -> KHÔNG thanh toán -> số dư giữ nguyên.
    assert tm.check_balance("FLOP", path=p) == "10"


def test_settle_accrues_unlock_3to1_on_testnet(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    tm.credit("100", path=p)
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_SUBMIT_URL", "https://relay.invalid")
    req = fsess.build_request("a" * 16, 2000, 1000, [], "9", nonce="1")
    paid = fsess.settle(req, path=p, submit_tx=lambda tx: {"tx_hash": "0xSESS01"})
    assert paid["outcome"] == "spent_onchain"
    us = tm.unlock_status(path=p)
    assert us["spent_testnet"] == "9" and us["unlocked_mainnet"] == "3"   # 9/3 = 3


def test_submit_request_testnet_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.delenv("FLOP_MEMPOOL_URL", raising=False)
    monkeypatch.delenv("FLOP_SUBMIT_URL", raising=False)
    monkeypatch.delenv("FLOP_RPC_URL", raising=False)
    req = fsess.build_request("a" * 16, 2000, 1000, [], "0.05", nonce="1")
    assert fsess.submit_request(req)["outcome"] == "skipped_unconfigured"


def test_maybe_run_session_gated(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    tm.credit("10", path=p)
    monkeypatch.delenv("FLOP_SESSION_ENABLED", raising=False)
    assert fsess.maybe_run_session("a" * 16, 2000, 1000, [], "0.05",
                                   path=p)["outcome"] == "skipped_off"
    monkeypatch.setenv("FLOP_SESSION_ENABLED", "true")
    out = fsess.maybe_run_session("a" * 16, 2000, 1000, [], "0.05", path=p)
    assert out["outcome"] == "spent_simulated"


# --- Đa dạng hóa request (chống bot/sybil) ---------------------------------------
def test_vary_request_no_jitter_keeps_base():
    m, fl, lt, fe = fsess.vary_request(["m1"], 1000, 2000, "0.05", jitter_pct=0)
    assert m == "m1" and fl == 1000 and lt == 2000 and fe == "0.05"


def test_vary_request_empty_models_returns_none():
    m, _fl, _lt, _fe = fsess.vary_request([], 1000, 2000, "0.05", jitter_pct=20)
    assert m is None


def test_vary_request_rotates_model_from_list():
    rng = _random.Random(0)
    picks = {fsess.vary_request(["a", "b", "c"], 1000, 2000, jitter_pct=0, rng=rng)[0]
             for _ in range(40)}
    assert picks == {"a", "b", "c"}          # xoay đủ cả list qua nhiều phiên


def test_vary_request_jitter_within_bounds():
    rng = _random.Random(1)
    for _ in range(60):
        _m, fl, lt, fe = fsess.vary_request(["m"], 1000, 2000, "0.10", jitter_pct=20, rng=rng)
        assert 800 <= fl <= 1200             # ±20% quanh 1000
        assert 1600 <= lt <= 2400            # ±20% quanh 2000
        assert 0.08 - 1e-9 <= float(fe) <= 0.12 + 1e-9
        assert fl >= 1 and lt >= 1


def test_vary_request_produces_variety():
    rng = _random.Random(2)
    flops_set = {fsess.vary_request(["m"], 1000, 2000, "0.10", jitter_pct=20, rng=rng)[1]
                 for _ in range(20)}
    assert len(flops_set) > 1                # KHÔNG phải N phiên giống hệt


# --- Chốt chặn miner-fleet (chống self-deal khi match tự nhiên) --------------------
def test_run_session_skips_fleet_miner(tmp_path):
    p = _ledger(tmp_path)
    tm.credit("10", path=p)
    MOCK_MINER = "did:key:zMOCKminer000000000000000000000000000000"   # thợ đào giả ở simulation
    # miner match nằm trong deny (fleet) -> KHÔNG settle
    out = fsess.run_inference_session("a" * 16, 2000, 1000, [], "0.05", path=p,
                                      deny_dids={MOCK_MINER})
    assert out["stage"] == "match" and out["outcome"] == "skipped_fleet_miner"
    # miner match KHÔNG trong deny -> settle bình thường
    out2 = fsess.run_inference_session("a" * 16, 2000, 1000, [], "0.05", path=p,
                                       deny_dids={"did:key:zSomeoneElse"})
    assert out2["outcome"] == "spent_simulated"
    # deny rỗng/None -> settle bình thường (tương thích cũ)
    out3 = fsess.run_inference_session("a" * 16, 2000, 1000, [], "0.05", path=p)
    assert out3["outcome"] == "spent_simulated"


# --- chạy thử phiên ở SIMULATION: hash mô phỏng có chốt cứng -------------------------

def test_real_hash_always_wins():
    assert fsess.resolve_model_hashes("h1,h2", "sim", False) == (["h1", "h2"], "real")
    assert fsess.resolve_model_hashes("h1|h2", "sim", True) == (["h1", "h2"], "real")


def test_sim_hash_refused_when_testnet_is_on():
    """CHỐT QUAN TRỌNG NHẤT: hash giả không bao giờ được chạm mempool thật."""
    assert fsess.resolve_model_hashes("", "simA,simB", True) == ([], "none-testnet")


def test_sim_hash_used_only_when_testnet_is_off():
    assert fsess.resolve_model_hashes("", "simA|simB", False) == (["simA", "simB"], "sim")


def test_no_hash_anywhere_returns_empty():
    assert fsess.resolve_model_hashes("", "", False) == ([], "none")
    assert fsess.resolve_model_hashes("   ", "  ", False) == ([], "none")


def test_mock_miner_default_is_unchanged():
    """Mặc định (pool=1) phải giữ NGUYÊN DID giả cũ — không đổi hành vi sẵn có."""
    assert fsess._mock_miner("bất kỳ", 1) == "did:key:zMOCKminer000000000000000000000000000000"
    assert fsess._mock_miner("bất kỳ", 0) == fsess._mock_miner("bất kỳ", 1)


def test_mock_miner_pool_spreads_deterministically():
    ids = [f"req{i:04d}" for i in range(300)]
    got = {fsess._mock_miner(i, 8) for i in ids}
    assert len(got) == 8
    assert all(d.startswith(fsess.SIM_MINER_PREFIX) for d in got)       # luôn lộ rõ là MOCK
    assert len({len(d) for d in got}) == 1                            # cùng độ dài
    assert fsess._mock_miner("req0001", 8) == fsess._mock_miner("req0001", 8)   # tất định


def test_mock_miner_tolerates_bad_pool():
    for bad in ("x", None, -3):
        assert fsess._mock_miner("r", bad) == fsess._mock_miner("r", 1)


# --- plan_session_spend: chi theo ngân sách, ĐƠN GIÁ không đổi ----------------------
from decimal import Decimal as _D


def _unit(fee, flops):
    return _D(fee) / _D(flops)


def test_plan_below_price_floor_waits():
    assert fsess.plan_session_spend("0.0005", "0.001", 10**9, max_sessions=3) == []


def test_plan_spends_exactly_due_at_constant_unit_price():
    rng = _random.Random(1)
    plan = fsess.plan_session_spend("347.2", "0.001", 10**9, max_sessions=3, rng=rng)
    assert len(plan) == 3
    assert sum(_D(f) for f, _ in plan) == _D("347.2")              # chi ĐÚNG phần đến hạn
    base = _unit("0.001", 10**9)
    for f, fl in plan:                                            # mua thêm FLOPs, KHÔNG trả giá cao hơn
        assert abs(_unit(f, fl) - base) / base < _D("0.000001")
    assert len({f for f, _ in plan}) == 3                         # không 3 phiên giống hệt


def test_plan_each_session_at_least_price_floor():
    for due in ("0.0015", "0.0035", "0.01", "1"):
        for seed in range(20):
            plan = fsess.plan_session_spend(due, "0.001", 10**9, max_sessions=5,
                                            rng=_random.Random(seed))
            assert plan and all(_D(f) >= _D("0.001") for f, _ in plan), (due, seed, plan)


def test_plan_respects_max_flops_cap_and_leaves_rest_for_pacer():
    plan = fsess.plan_session_spend("347.2", "0.001", 10**9, max_sessions=3,
                                    max_flops=5 * 10**11, rng=_random.Random(2))
    assert len(plan) == 3
    assert all(fl <= 5 * 10**11 and _D(f) <= _D("0.5") for f, fl in plan)
    assert sum(_D(f) for f, _ in plan) <= _D("1.5")               # phần dư -> run sau


def test_plan_rejects_garbage():
    for args in [("x", "0.001", 10**9), ("1", "0", 10**9), ("1", "0.001", 0),
                 ("1", "0.001", "nope"), (None, "0.001", 10**9)]:
        assert fsess.plan_session_spend(*args) == []


# --- Ngày đầu testnet: proof theo mã phiên MẠNG cấp + thuật ngữ tài liệu ---------------
_REQ = {"id": "our-req-1"}
_DOC_POUI = {"session_id": "net-7f3a", "activation_commitments": ["ab12"],
             "transcript_root": "cd34", "cosig": "sig"}


def test_verify_accepts_network_session_id_and_doc_terms():
    assert fsess.verify_poui(_REQ, _DOC_POUI, "net-7f3a")["ok"]


def test_verify_still_accepts_our_own_id_and_mock_poui():
    mock = {"session_id": "our-req-1", "commitment": "c", "miner_sig": "s"}
    assert fsess.verify_poui(_REQ, mock)["ok"]
    assert fsess.verify_poui(_REQ, mock, "net-other")["ok"]


def test_verify_rejects_foreign_session_and_reports_real_keys():
    r = fsess.verify_poui(_REQ, dict(_DOC_POUI, session_id="net-OTHER"), "net-7f3a")
    assert not r["ok"] and "lệch" in r["reason"] and "activation_commitments" in r["reason"]
    r = fsess.verify_poui(_REQ, {"session_id": "net-7f3a", "blob": "x"}, "net-7f3a")
    assert not r["ok"] and "keys: ['blob', 'session_id']" in r["reason"]


def test_day_one_doc_shaped_proof_now_settles_onchain(tmp_path, monkeypatch):
    # Kịch bản 2 từng ra verify/disputed và 0 giao dịch thật.
    p = _ledger(tmp_path)
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_MEMPOOL_URL", "http://mempool.test")
    monkeypatch.setenv("FLOP_RPC_URL", "http://rpc.test")
    tm.credit("50", path=p)
    paid = []
    out = fsess.run_inference_session(
        "real-model", 2000, 10**9, [], "0.5", path=p,
        submit_fn=lambda req: {"session_id": "net-7f3a", "miner_did": "did:key:zM",
                               "poui": _DOC_POUI},
        submit_tx=lambda tx: paid.append(tx) or {"tx_hash": "0x" + "c" * 62},
        log=lambda *a: None)
    assert out["stage"] == "settle" and out["outcome"] == "spent_onchain" and len(paid) == 1


def test_day_one_async_proof_is_pending_with_diagnostics(tmp_path, monkeypatch):
    # Kịch bản 1: mempool nhận phiên, proof đến sau -> pending, kèm tên trường thật để sửa.
    p = _ledger(tmp_path)
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_MEMPOOL_URL", "http://mempool.test")
    tm.credit("50", path=p)
    out = fsess.run_inference_session(
        "real-model", 2000, 10**9, [], "0.5", path=p,
        submit_fn=lambda req: {"session_id": "net-1", "miner_did": "did:key:zM",
                               "_raw_keys": ["channel", "miner", "session"]},
        log=lambda *a: None)
    assert out["stage"] == "await_poui" and out["outcome"] == "pending"
    assert out["session_id"] == "net-1" and "channel" in out["reason"]


# --- mạng ÉP phí cố định mỗi phiên (FLOP_SESSION_FIXED_FEE) -----------------------------
def test_fixed_fee_every_session_pays_exactly_that():
    plan = fsess.plan_session_spend("1.05", "0.001", 1_000_000, max_sessions=20, fixed_fee="0.1")
    assert plan == [("0.1", 1_000_000)] * 10                # phần lẻ 0.05 để run sau


def test_fixed_fee_capped_by_sessions_per_run_and_below_price():
    assert len(fsess.plan_session_spend("5", "0.001", 10, max_sessions=4, fixed_fee="0.1")) == 4
    assert fsess.plan_session_spend("0.05", "0.001", 10, max_sessions=4, fixed_fee="0.1") == []
    assert fsess.plan_session_spend("1", "0.001", 10, max_sessions=4, fixed_fee="junk") == []


def test_blank_fixed_fee_keeps_unit_price_plan():
    a = fsess.plan_session_spend("1", "0.1", 100, max_sessions=3, fixed_fee="")
    assert a and abs(sum(float(f) for f, _ in a) - 1.0) < 1e-6



def test_burst_of_identical_sessions_gets_unique_ids_and_nonces(monkeypatch):
    # Phí cố định + 1 model: 3 phiên trong CÙNG một mili-giây trước đây trùng id lẫn nonce.
    monkeypatch.setattr(fsess.time, "time", lambda: 1_800_000_000.0)
    monkeypatch.setattr(tm.time, "time", lambda: 1_800_000_000.0)
    reqs = [fsess.build_request("a" * 64, 2000, 10**9, fee="0.1") for _ in range(3)]
    assert len({r["id"] for r in reqs}) == 3
    nonces = [int(r["nonce"]) for r in reqs]
    assert nonces == sorted(nonces) and len(set(nonces)) == 3



def test_plan_never_emits_zero_flops_sessions():
    # Trần FLOPs/phiên = đúng 1 đơn vị nền -> trọng số ±20% đẩy phiên dưới 1 đơn vị.
    import random
    r = random.Random(1)
    for due in ("80", "99", "242.391641"):
        for fee in ("0.1", "1", "0.001"):
            plan = fsess.plan_session_spend(due, fee, 1, max_sessions=10, max_flops=1, rng=r)
            assert all(fl >= 1 and float(f) > 0 for f, fl in plan)
            for f, fl in plan:
                fsess.build_request("a" * 64, 2000, fl, fee=f)        # không còn ValueError



def test_session_not_posted_when_balance_cannot_cover_fee(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOP_SESSION_ENABLED", "true")
    p = str(tmp_path / "l.json")
    posted = []
    sub = lambda req: posted.append(1) or {"session_id": "s", "miner_did": "did:key:zM"}
    r = fsess.maybe_run_session("a" * 64, 2000, 10**9, fee="5", path=p, submit_fn=sub)
    assert r["outcome"] == "skipped_insufficient" and r["stage"] == "precheck" and posted == []
    tm.credit("5", path=p)
    r2 = fsess.run_inference_session("a" * 64, 2000, 10**9, fee="5", path=p)
    assert r2["stage"] == "settle" and r2["outcome"] == "spent_simulated"     # đủ tiền -> chạy bình thường
