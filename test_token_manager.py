"""
Test cho token_manager.py — chạy: python -m pytest test_token_manager.py -q

Bao phủ: công tắc mode, số học Decimal, credit, spend (simulation / insufficient /
testnet-unconfigured / testnet-onchain / error_submit), đọc file hỏng, và
meter_inference gating.
"""

import json
import pytest

import token_manager as tm


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Mỗi test khởi đầu ở simulation, không dính env FLOP_/TESTNET_ của máy chạy."""
    for k in ("TESTNET_ENABLED", "FLOP_RPC_URL", "FLOP_SUBMIT_URL", "FLOP_TX_MODE",
              "FLOP_TOKEN_SYMBOL", "TOKEN_LEDGER_FILE", "FLOP_METER_ENABLED",
              "FLOP_INFERENCE_COST", "FLOP_ORGANIC_ONLY", "FLOP_MAX_SPENDS_PER_HOUR"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def ledger(tmp_path):
    return str(tmp_path / "ledger.json")


# --- mode + config ---------------------------------------------------------------

def test_mode_defaults_to_simulation(monkeypatch):
    assert tm.ledger_mode() == "simulation"
    assert tm.testnet_enabled() is False
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    assert tm.ledger_mode() == "testnet"
    monkeypatch.setenv("TESTNET_ENABLED", "yes")
    assert tm.ledger_mode() == "testnet"
    monkeypatch.setenv("TESTNET_ENABLED", "off")
    assert tm.ledger_mode() == "simulation"


# --- số học Decimal --------------------------------------------------------------

def test_decimal_helpers():
    assert tm._fmt(tm._parse_amount("0.0010")) == "0.001"      # bỏ số 0 thừa
    assert tm._parse_amount("nope") is None
    assert tm._parse_amount("") is None
    # cộng/trừ không trôi float
    assert tm._fmt(tm._parse_amount("0.1") + tm._parse_amount("0.2")) == "0.3"


# --- credit ----------------------------------------------------------------------

def test_credit_accumulates(ledger):
    assert tm.credit("100", path=ledger)["balance_after"] == "100"
    assert tm.credit("0.5", path=ledger)["balance_after"] == "100.5"
    assert tm.check_balance(path=ledger) == "100.5"


def test_credit_rejects_bad_amount(ledger):
    assert tm.credit("0", path=ledger)["ok"] is False
    assert tm.credit("junk", path=ledger)["ok"] is False


# --- spend: simulation -----------------------------------------------------------

def test_spend_simulation_logs_and_debits(ledger):
    tm.credit("100", path=ledger)
    lines = []
    r = tm.spend("0.001", "Gemini Inference", path=ledger, log=lines.append)
    assert r["outcome"] == "spent_simulated"
    assert r["balance_after"] == "99.999"
    assert "[SIMULATION] Spent 0.001 MOCK_FLOP for Gemini Inference" in lines
    assert tm.check_balance(path=ledger) == "99.999"


def test_spend_insufficient(ledger):
    tm.credit("0.0005", path=ledger)
    lines = []
    r = tm.spend("0.001", "x", path=ledger, log=lines.append)
    assert r["outcome"] == "skipped_insufficient"
    assert lines == []                              # không log, không chi
    assert tm.check_balance(path=ledger) == "0.0005"


def test_spend_rejects_bad_amount(ledger):
    tm.credit("100", path=ledger)
    assert tm.spend("0", "x", path=ledger)["outcome"] == "skipped_insufficient"
    assert tm.spend("-1", "x", path=ledger)["outcome"] == "skipped_insufficient"
    assert tm.spend("junk", "x", path=ledger)["outcome"] == "skipped_insufficient"


# --- spend: testnet --------------------------------------------------------------

def test_testnet_refuses_without_rpc(ledger, monkeypatch):
    tm.credit("100", path=ledger)                   # nạp khi còn simulation
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    r = tm.spend("1", "x", path=ledger, submit_tx=lambda tx: {"tx_hash": "0xNO"})
    assert r["outcome"] == "skipped_unconfigured"
    assert "FLOP_RPC_URL" in r["reason"]
    assert tm.check_balance(path=ledger) == "100"   # số dư nguyên vẹn


def test_testnet_refuses_when_tx_mode_off(ledger, monkeypatch):
    # Có endpoint nhưng FLOP_TX_MODE=off -> KHÔNG tự nối adapter -> vẫn từ chối gửi.
    tm.credit("100", path=ledger)
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_RPC_URL", "https://rpc.test")
    monkeypatch.setenv("FLOP_TX_MODE", "off")
    r = tm.spend("1", "x", path=ledger)
    assert r["outcome"] == "skipped_unconfigured"
    assert "submit_tx" in r["reason"]
    assert tm.check_balance(path=ledger) == "100"


def test_testnet_submits_and_debits(ledger, monkeypatch):
    tm.credit("100", path=ledger)
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_RPC_URL", "https://rpc.test")
    seen = []
    r = tm.spend("0.001", "Gemini Inference", path=ledger,
                 submit_tx=lambda tx: seen.append(tx) or {"tx_hash": "0xdeadbeefcafe"})
    assert r["outcome"] == "spent_onchain"
    assert r["tx_hash"] == "0xdeadbeefcafe"
    assert r["balance_after"] == "99.999"
    assert seen and seen[0]["amount"] == "0.001" and seen[0]["rpc_url"] == "https://rpc.test"


def test_testnet_submit_error_does_not_debit(ledger, monkeypatch):
    tm.credit("100", path=ledger)
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_RPC_URL", "https://rpc.test")

    def boom(tx):
        raise RuntimeError("rpc down")

    r = tm.spend("1", "x", path=ledger, submit_tx=boom)
    assert r["outcome"] == "error_submit"
    assert "rpc down" in r["reason"]
    assert tm.check_balance(path=ledger) == "100"   # không trừ khi gửi lỗi


# --- store loading ---------------------------------------------------------------

def test_load_missing_and_corrupt(ledger, tmp_path):
    assert tm.load_ledger(ledger) == {"balances": {}, "entries": []}
    assert tm.check_balance(path=ledger) == "0"
    bad = str(tmp_path / "bad.json")
    with open(bad, "w") as f:
        json.dump([1, 2, 3], f)                      # cấu trúc sai -> coi như rỗng
    assert tm.load_ledger(bad) == {"balances": {}, "entries": []}


# --- meter_inference (gated) -----------------------------------------------------

def test_meter_inference_off_by_default(ledger):
    assert tm.meter_inference(path=ledger)["outcome"] == "skipped_off"


def test_meter_inference_on(ledger, monkeypatch):
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    r = tm.meter_inference(path=ledger)
    assert r["outcome"] == "spent_simulated"
    assert r["amount"] == "0.001"                    # mặc định FLOP_INFERENCE_COST


# --- Bất biến organic-only (FLOP_ORGANIC_ONLY) chống burn-loop tổng hợp -----------

def test_meter_organic_only_blocks_without_event(ledger, monkeypatch):
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    monkeypatch.setenv("FLOP_ORGANIC_ONLY", "true")
    r = tm.meter_inference(path=ledger)              # không event_id -> từ chối
    assert r["outcome"] == "skipped_synthetic"
    assert tm.check_balance(path=ledger) == "1"      # không hề chi


def test_meter_organic_only_blank_event_blocked(ledger, monkeypatch):
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    monkeypatch.setenv("FLOP_ORGANIC_ONLY", "true")
    assert tm.meter_inference(path=ledger, event_id="   ")["outcome"] == "skipped_synthetic"


def test_meter_organic_only_allows_with_event(ledger, monkeypatch):
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    monkeypatch.setenv("FLOP_ORGANIC_ONLY", "true")
    r = tm.meter_inference(path=ledger, event_id="alice")
    assert r["outcome"] == "spent_simulated"


def test_meter_event_id_not_required_when_flag_off(ledger, monkeypatch):
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    # ORGANIC_ONLY tắt -> event_id không bắt buộc (hành vi cũ giữ nguyên).
    assert tm.meter_inference(path=ledger)["outcome"] == "spent_simulated"


# --- Envelope tần suất (FLOP_MAX_SPENDS_PER_HOUR) ---------------------------------

def test_rate_cap_blocks_after_limit(ledger, monkeypatch):
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    monkeypatch.setenv("FLOP_MAX_SPENDS_PER_HOUR", "2")
    # 2 lần chi đầu OK; lần 3 trong cùng giờ -> chặn trước khi chi.
    assert tm.meter_inference(path=ledger)["outcome"] == "spent_simulated"
    assert tm.meter_inference(path=ledger)["outcome"] == "spent_simulated"
    r = tm.meter_inference(path=ledger)
    assert r["outcome"] == "skipped_rate_cap"
    assert tm.check_balance(path=ledger) == "0.998"  # đúng 2 lần *0.001, lần 3 không trừ


# --- spend_stats (audit chống sybil) ---------------------------------------------

def test_spend_stats_counts_spends_only(ledger, monkeypatch):
    tm.credit("1", path=ledger)                      # credit KHÔNG tính là spend
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    tm.meter_inference(path=ledger)
    tm.meter_inference(path=ledger)
    st = tm.spend_stats(path=ledger)
    assert st["spend_count_total"] == 2
    assert st["spend_count_24h"] == 2


def test_spend_stats_24h_window(ledger):
    import time as _t
    now = 1_700_000_000
    old_ts = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(now - 200_000))  # ~55h trước
    new_ts = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(now - 100))      # trong 24h
    state = {"balances": {"FLOP": "0"}, "entries": [
        {"kind": "spend", "token": "FLOP", "amount": "0.001", "ts": old_ts},
        {"kind": "spend", "token": "FLOP", "amount": "0.001", "ts": new_ts},
        {"kind": "credit", "token": "FLOP", "amount": "1", "ts": new_ts},
    ]}
    with open(ledger, "w") as f:
        json.dump(state, f)
    st = tm.spend_stats(now=now, path=ledger)
    assert st["spend_count_total"] == 2              # cả 2 spend, bỏ credit
    assert st["spend_count_24h"] == 1                # chỉ entry trong 24h
