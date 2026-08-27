"""
Test Auto-Cycle Faucet scaffold (flop_faucet) — python -m pytest test_flop_faucet.py -q
"""

import json
import pytest
import flop_faucet as ff
import token_manager as tm


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("FLOP_FAUCET_ENABLED", "FLOP_FAUCET_URL", "FLOP_FAUCET_AMOUNT",
              "FLOP_FAUCET_COOLDOWN_HOURS", "FLOP_FAUCET_REFILL_BELOW",
              "FLOP_FAUCET_STATE", "TESTNET_ENABLED", "FLOP_TOKEN_SYMBOL"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def paths(tmp_path):
    return {"ledger_path": str(tmp_path / "ledger.json"),
            "faucet_state": str(tmp_path / "faucet.json")}


CLAIM_OK = lambda req: {"amount": "100"}


def test_disabled_by_default(paths):
    r = ff.run_faucet_cycle(claim_fn=CLAIM_OK, **paths)
    assert r["outcome"] == "skipped_disabled"


def test_unconfigured_without_url_or_fn(paths, monkeypatch):
    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "true")
    assert ff.run_faucet_cycle(claim_fn=CLAIM_OK, **paths)["outcome"] == "skipped_unconfigured"
    monkeypatch.setenv("FLOP_FAUCET_URL", "https://faucet.test")
    assert ff.run_faucet_cycle(claim_fn=None, **paths)["outcome"] == "skipped_unconfigured"


def test_claims_and_credits_ledger(paths, monkeypatch):
    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "true")
    monkeypatch.setenv("FLOP_FAUCET_URL", "https://faucet.test")
    seen = []
    fn = lambda req: seen.append(req) or {"amount": "100"}
    r = ff.run_faucet_cycle(claim_fn=fn, now=1_000_000, log=lambda m: None, **paths)
    assert r["outcome"] == "claimed"
    assert r["amount"] == "100"
    assert seen and seen[0]["faucet_url"] == "https://faucet.test"
    assert tm.check_balance("FLOP", path=paths["ledger_path"]) == "100"


def test_cooldown(paths, monkeypatch):
    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "true")
    monkeypatch.setenv("FLOP_FAUCET_URL", "https://faucet.test")
    monkeypatch.setenv("FLOP_FAUCET_COOLDOWN_HOURS", "24")
    # claim gần đây (1h trước) -> còn trong cooldown
    with open(paths["faucet_state"], "w") as f:
        json.dump({"FLOP": {"last_claim_ts": 1_000_000 - 3600}}, f)
    r = ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=1_000_000, **paths)
    assert r["outcome"] == "skipped_cooldown"


def test_refill_below_threshold(paths, monkeypatch):
    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "true")
    monkeypatch.setenv("FLOP_FAUCET_URL", "https://faucet.test")
    monkeypatch.setenv("FLOP_FAUCET_REFILL_BELOW", "50")
    tm.credit("80", path=paths["ledger_path"])          # số dư 80 >= 50 -> chưa cần
    r = ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=2_000_000, log=lambda m: None, **paths)
    assert r["outcome"] == "skipped_full"


def test_claim_error_is_recorded(paths, monkeypatch):
    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "true")
    monkeypatch.setenv("FLOP_FAUCET_URL", "https://faucet.test")

    def boom(req):
        raise RuntimeError("faucet down")

    r = ff.run_faucet_cycle(claim_fn=boom, now=3_000_000, **paths)
    assert r["outcome"] == "error_claim"
    assert "faucet down" in r["reason"]
