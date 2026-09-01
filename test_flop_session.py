"""
Test cho flop_session.py — mua suy luận (đường earn airdrop chính, 3:1).
Sổ cái dùng file tạm (path=). KHÔNG chạm mạng: simulation dùng thợ đào giả; testnet dùng
submit_fn/submit_tx được tiêm.
"""
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
    monkeypatch.setattr(fsess, "verify_poui", lambda req, poui: {"ok": False, "reason": "sai"})
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
