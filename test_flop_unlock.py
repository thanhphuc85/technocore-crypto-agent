"""
Test theo dõi chi tiêu testnet + số tham khảo 3:1 — python -m pytest test_flop_unlock.py -q
"""

import pytest
import token_manager as tm


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("TESTNET_ENABLED", "FLOP_RPC_URL", "FLOP_SUBMIT_URL", "FLOP_TX_MODE",
              "FLOP_UNLOCK_RATIO", "FLOP_TOKEN_SYMBOL", "FLOP_DAILY_BUDGET"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def ledger(tmp_path):
    return str(tmp_path / "ledger.json")


FAKE_SUBMIT = lambda tx: {"tx_hash": "0xfeedface0001"}


def _testnet_spend(ledger, monkeypatch, amount):
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_RPC_URL", "https://rpc.test")
    return tm.spend(amount, "x", path=ledger, submit_tx=FAKE_SUBMIT, log=lambda m: None)


def test_ratio_default_and_override(monkeypatch):
    assert tm.unlock_ratio() == 3
    monkeypatch.setenv("FLOP_UNLOCK_RATIO", "4")
    assert tm.unlock_ratio() == 4
    monkeypatch.setenv("FLOP_UNLOCK_RATIO", "0")     # không dương -> 3
    assert tm.unlock_ratio() == 3


def test_simulation_spend_does_not_accrue(ledger):
    tm.credit("100", path=ledger)
    tm.spend("9", "sim", path=ledger, log=lambda m: None)   # simulation
    st = tm.unlock_status(path=ledger)
    assert st["spent_testnet"] == "0" and st["unlock_estimate"] == "0"


def test_testnet_spend_is_tracked_as_an_estimate_only(ledger, monkeypatch):
    tm.credit("100", path=ledger)
    _testnet_spend(ledger, monkeypatch, "9")
    st = tm.unlock_status(path=ledger)
    assert st["spent_testnet"] == "9" and st["unlock_estimate"] == "3"     # 9 / 3
    assert st["basis"] == "estimate-not-entitlement"
    assert not {"unlocked_mainnet", "claimable", "claimed_mainnet"} & set(st)


def test_custom_ratio(ledger, monkeypatch):
    monkeypatch.setenv("FLOP_UNLOCK_RATIO", "5")
    tm.credit("100", path=ledger)
    _testnet_spend(ledger, monkeypatch, "10")
    assert tm.unlock_status(path=ledger)["unlock_estimate"] == "2"         # 10 / 5
