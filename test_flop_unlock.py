"""
Test kế toán MỞ KHÓA MAINNET 3:1 + van claim gated — python -m pytest test_flop_unlock.py -q
"""

import pytest
import token_manager as tm


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("TESTNET_ENABLED", "FLOP_RPC_URL", "FLOP_SUBMIT_URL", "FLOP_TX_MODE",
              "FLOP_UNLOCK_RATIO", "FLOP_MAINNET_CLAIM_URL", "FLOP_TOKEN_SYMBOL",
              "FLOP_DAILY_BUDGET"):
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
    assert st["spent_testnet"] == "0"
    assert st["unlocked_mainnet"] == "0"


def test_testnet_spend_accrues_3to1(ledger, monkeypatch):
    tm.credit("100", path=ledger)
    _testnet_spend(ledger, monkeypatch, "9")
    st = tm.unlock_status(path=ledger)
    assert st["spent_testnet"] == "9"
    assert st["unlocked_mainnet"] == "3"     # 9 / 3
    assert st["claimable"] == "3"


def test_custom_ratio_accrual(ledger, monkeypatch):
    monkeypatch.setenv("FLOP_UNLOCK_RATIO", "5")
    tm.credit("100", path=ledger)
    _testnet_spend(ledger, monkeypatch, "10")
    assert tm.unlock_status(path=ledger)["unlocked_mainnet"] == "2"   # 10 / 5


# --- claim seam (gated) ----------------------------------------------------------

def test_claim_nothing_when_zero(ledger):
    r = tm.claim_mainnet_unlock(path=ledger)
    assert r["outcome"] == "skipped_nothing"


def test_claim_refuses_without_url_or_fn(ledger, monkeypatch):
    tm.credit("100", path=ledger)
    _testnet_spend(ledger, monkeypatch, "9")            # claimable = 3
    # no url, no fn
    r = tm.claim_mainnet_unlock(path=ledger, claim_fn=lambda req: {"tx_hash": "0x"})
    assert r["outcome"] == "skipped_unconfigured"
    assert "FLOP_MAINNET_CLAIM_URL" in r["reason"]
    # url but no fn
    r2 = tm.claim_mainnet_unlock(path=ledger, claim_url="https://claim.test")
    assert r2["outcome"] == "skipped_unconfigured"
    assert "claim_fn" in r2["reason"]


def test_claim_success_updates_claimed(ledger, monkeypatch):
    tm.credit("100", path=ledger)
    _testnet_spend(ledger, monkeypatch, "9")            # claimable = 3
    seen = []
    fn = lambda req: seen.append(req) or {"tx_hash": "0xCLAIM01"}
    r = tm.claim_mainnet_unlock(path=ledger, claim_fn=fn, claim_url="https://claim.test",
                                log=lambda m: None)
    assert r["outcome"] == "claimed_mainnet"
    assert r["amount"] == "3"
    assert r["tx_hash"] == "0xCLAIM01"
    assert seen and seen[0]["amount"] == "3"
    # claimable drops to 0 after claiming all
    st = tm.unlock_status(path=ledger)
    assert st["claimed_mainnet"] == "3"
    assert st["claimable"] == "0"


def test_claim_caps_at_claimable(ledger, monkeypatch):
    tm.credit("100", path=ledger)
    _testnet_spend(ledger, monkeypatch, "9")            # claimable = 3
    r = tm.claim_mainnet_unlock(amount="100", path=ledger,
                                claim_fn=lambda req: {"tx_hash": "0xC"},
                                claim_url="https://claim.test", log=lambda m: None)
    assert r["amount"] == "3"                            # kẹp ở phần claimable
