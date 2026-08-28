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
              "FLOP_FAUCET_STATE", "TESTNET_ENABLED", "FLOP_TOKEN_SYMBOL",
              "FLOP_FAUCET_DEMAND_ONLY", "FLOP_FAUCET_JITTER_MIN",
              "FLOP_FAUCET_MAX_PER_DAY"):
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


# --- Faucet theo NHU CẦU (FLOP_FAUCET_DEMAND_ONLY) --------------------------------

def test_demand_only_requires_threshold(paths, monkeypatch):
    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "true")
    monkeypatch.setenv("FLOP_FAUCET_URL", "https://faucet.test")
    monkeypatch.setenv("FLOP_FAUCET_DEMAND_ONLY", "true")
    # thiếu REFILL_BELOW -> từ chối claim theo lịch.
    r = ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=3_000_000, **paths)
    assert r["outcome"] == "skipped_demand"


def test_demand_only_claims_when_balance_low(paths, monkeypatch):
    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "true")
    monkeypatch.setenv("FLOP_FAUCET_URL", "https://faucet.test")
    monkeypatch.setenv("FLOP_FAUCET_DEMAND_ONLY", "true")
    monkeypatch.setenv("FLOP_FAUCET_REFILL_BELOW", "50")
    # số dư 0 < 50 -> claim; số dư sau claim (100) >= 50 -> lần sau skipped_full.
    r = ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=3_000_000, **paths)
    assert r["outcome"] == "claimed"
    r2 = ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=3_000_000 + 10 ** 7, **paths)
    assert r2["outcome"] == "skipped_full"


# --- Cooldown jitter (FLOP_FAUCET_JITTER_MIN) -------------------------------------

def test_no_jitter_on_first_claim(paths, monkeypatch):
    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "true")
    monkeypatch.setenv("FLOP_FAUCET_URL", "https://faucet.test")
    monkeypatch.setenv("FLOP_FAUCET_COOLDOWN_HOURS", "0")
    monkeypatch.setenv("FLOP_FAUCET_JITTER_MIN", "600")
    # last=0 (chưa từng claim) -> KHÔNG jitter -> claim ngay dù now nhỏ.
    r = ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=10, **paths)
    assert r["outcome"] == "claimed"


def test_cooldown_jitter_extends_window(paths, monkeypatch):
    import random

    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "true")
    monkeypatch.setenv("FLOP_FAUCET_URL", "https://faucet.test")
    monkeypatch.setenv("FLOP_FAUCET_COOLDOWN_HOURS", "0")   # base cooldown 0 -> chỉ còn jitter
    monkeypatch.setenv("FLOP_FAUCET_JITTER_MIN", "60")      # +0..60 phút

    last = 1000
    with open(paths["faucet_state"], "w") as f:
        json.dump({"FLOP": {"last_claim_ts": last}}, f)

    off = random.Random(last).random() * 60 * 60           # cùng công thức module
    # còn trong offset jitter -> chưa cho claim.
    r = ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=last + int(off) - 5, **paths)
    assert r["outcome"] == "skipped_cooldown"
    # qua khỏi offset -> claim.
    r2 = ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=last + int(off) + 5, **paths)
    assert r2["outcome"] == "claimed"


# --- Envelope: trần claim/ngày (FLOP_FAUCET_MAX_PER_DAY) --------------------------

def test_daily_cap_blocks_after_limit(paths, monkeypatch):
    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "true")
    monkeypatch.setenv("FLOP_FAUCET_URL", "https://faucet.test")
    monkeypatch.setenv("FLOP_FAUCET_COOLDOWN_HOURS", "0")   # tách khỏi cooldown để test cap
    monkeypatch.setenv("FLOP_FAUCET_MAX_PER_DAY", "2")
    now = 3_000_000
    assert ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=now, **paths)["outcome"] == "claimed"
    assert ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=now, **paths)["outcome"] == "claimed"
    assert ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=now, **paths)["outcome"] == "skipped_daily_cap"


def test_daily_cap_resets_next_day(paths, monkeypatch):
    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "true")
    monkeypatch.setenv("FLOP_FAUCET_URL", "https://faucet.test")
    monkeypatch.setenv("FLOP_FAUCET_COOLDOWN_HOURS", "0")
    monkeypatch.setenv("FLOP_FAUCET_MAX_PER_DAY", "1")
    day1 = 3_000_000
    assert ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=day1, **paths)["outcome"] == "claimed"
    assert ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=day1, **paths)["outcome"] == "skipped_daily_cap"
    day2 = day1 + 86_400                                    # +24h -> ngày UTC khác -> reset
    assert ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=day2, **paths)["outcome"] == "claimed"
