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
              "FLOP_FAUCET_DEMAND_ONLY", "FLOP_FAUCET_MAX_PER_DAY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FLOP_FAUCET_JITTER_MIN", "0")    # tắt tường minh: test cần tất định


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
    monkeypatch.setenv("FLOP_FAUCET_COOLDOWN_HOURS", "10")  # jitter kẹp <= 10% chu kỳ = 60 phút
    monkeypatch.setenv("FLOP_FAUCET_JITTER_MIN", "60")      # +0..60 phút

    last = 1000
    with open(paths["faucet_state"], "w") as f:
        json.dump({"FLOP": {"last_claim_ts": last}}, f)

    base = 10 * 3600
    off = random.Random(last).random() * 60 * 60           # cùng công thức module
    # còn trong offset jitter -> chưa cho claim.
    r = ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=last + base + int(off) - 5, **paths)
    assert r["outcome"] == "skipped_cooldown"
    # qua khỏi offset -> claim.
    r2 = ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=last + base + int(off) + 5, **paths)
    assert r2["outcome"] == "claimed"


def test_jitter_is_capped_to_tenth_of_cycle(monkeypatch):
    monkeypatch.setenv("FLOP_FAUCET_COOLDOWN_HOURS", "1")
    monkeypatch.setenv("FLOP_FAUCET_JITTER_MIN", "30")
    for last in range(1, 200):
        assert ff._cooldown_seconds(last) <= 3600 + 6 * 60   # chu kỳ 1h -> jitter <= 6 phút


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


def test_faucet_jitter_on_by_default_and_explicit_zero_disables(monkeypatch):
    monkeypatch.delenv("FLOP_FAUCET_JITTER_MIN", raising=False)  # vắng biến = 30 phút
    assert ff.jitter_minutes() == 30.0
    monkeypatch.setenv("FLOP_FAUCET_JITTER_MIN", "0")
    assert ff.jitter_minutes() == 0.0


NOW = 1_000_000


def _enable(monkeypatch):
    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "true")
    monkeypatch.setenv("FLOP_FAUCET_URL", "https://faucet.test")


def test_zero_amount_is_not_credited(paths, monkeypatch):
    _enable(monkeypatch)
    r = ff.run_faucet_cycle(claim_fn=lambda req: {"amount": 0}, now=NOW, log=lambda m: None, **paths)
    assert r["outcome"] == "claimed_zero"
    assert tm.check_balance(path=paths["ledger_path"]) == "0"
    r2 = ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=NOW + 60, log=lambda m: None, **paths)
    assert r2["outcome"] == "skipped_cooldown"               # không gõ lại ngay


def test_missing_amount_still_falls_back_to_requested(paths, monkeypatch):
    _enable(monkeypatch)
    r = ff.run_faucet_cycle(claim_fn=lambda req: {}, now=NOW, log=lambda m: None, **paths)
    assert r["outcome"] == "claimed" and r["amount"] == ff.faucet_amount()


# --- chu kỳ 'auto': thử mỗi giờ, faucet báo lượt kế tiếp thì chờ đúng tới đó ---------------
def test_auto_cycle_probes_hourly_and_learns_next_claim(paths, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("FLOP_FAUCET_COOLDOWN_HOURS", "auto")
    monkeypatch.setenv("FLOP_FAUCET_JITTER_MIN", "0")
    q = lambda **k: ff.run_faucet_cycle(log=lambda m: None, **paths, **k)
    assert q(claim_fn=lambda req: {"amount": "10", "retry_after_s": 86400}, now=NOW)["outcome"] == "claimed"
    assert q(claim_fn=CLAIM_OK, now=NOW + 3600 + 1)["outcome"] == "skipped_cooldown"   # faucet hẹn 24h
    assert q(claim_fn=CLAIM_OK, now=NOW + 86400 + 1)["outcome"] == "claimed"


def test_auto_cycle_without_hint_claims_every_hour(paths, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("FLOP_FAUCET_COOLDOWN_HOURS", "auto")
    monkeypatch.setenv("FLOP_FAUCET_JITTER_MIN", "0")
    q = lambda now: ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=now, log=lambda m: None, **paths)
    assert q(NOW)["outcome"] == "claimed"
    assert q(NOW + 1800)["outcome"] == "skipped_cooldown"
    assert q(NOW + 3600)["outcome"] == "claimed"
    assert tm.check_balance(path=paths["ledger_path"]) == "200"


def test_rate_limited_reply_waits_without_crediting(paths, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("FLOP_FAUCET_COOLDOWN_HOURS", "auto")
    monkeypatch.setenv("FLOP_FAUCET_JITTER_MIN", "0")
    r = ff.run_faucet_cycle(claim_fn=lambda req: {"amount": 0, "retry_after_s": 7200},
                            now=NOW, log=lambda m: None, **paths)
    assert r["outcome"] == "claimed_zero" and tm.check_balance(path=paths["ledger_path"]) == "0"
    r2 = ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=NOW + 3600 + 1, log=lambda m: None, **paths)
    assert r2["outcome"] == "skipped_cooldown"                      # chờ đủ 2h faucet yêu cầu


def test_merge_state_never_moves_backwards():
    a = {"FLOP": {"last_claim_ts": 100, "claims": {"2026-11-03": 2}}}
    b = {"FLOP": {"last_claim_ts": 50, "next_claim_ts": 900, "claims": {"2026-11-03": 5, "2026-11-02": 9}}}
    m = ff.merge_state(a, b)
    assert m["FLOP"]["last_claim_ts"] == 100 and m["FLOP"]["next_claim_ts"] == 900
    assert m["FLOP"]["claims"] == {"2026-11-02": 9, "2026-11-03": 5}
    assert ff.merge_state(None, "junk") == {}


def test_claim_error_backs_off_instead_of_retrying_every_run(paths, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("FLOP_FAUCET_COOLDOWN_HOURS", "auto")
    monkeypatch.setenv("FLOP_FAUCET_JITTER_MIN", "0")
    calls = []

    def boom(req):
        calls.append(1)
        raise RuntimeError("400 already claimed")

    q = lambda now: ff.run_faucet_cycle(claim_fn=boom, now=now, log=lambda m: None, **paths)
    assert q(NOW)["outcome"] == "error_claim"
    for k in range(1, 12):                                   # 11 run 5 phút kế tiếp
        assert q(NOW + 300 * k)["outcome"] == "skipped_cooldown"
    assert q(NOW + 3600 + 1)["outcome"] == "error_claim"
    assert len(calls) == 2                                   # 2 lần gõ trong ~1 giờ, không phải 13


def test_seconds_until_next(paths, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("FLOP_FAUCET_COOLDOWN_HOURS", "auto")
    monkeypatch.setenv("FLOP_FAUCET_JITTER_MIN", "0")
    fs = paths["faucet_state"]
    assert ff.seconds_until_next(now=NOW, faucet_state=fs) is None          # chưa claim
    ff.run_faucet_cycle(claim_fn=CLAIM_OK, now=NOW, log=lambda m: None, **paths)
    assert ff.seconds_until_next(now=NOW + 3500, faucet_state=fs) == 100
    assert ff.seconds_until_next(now=NOW + 4000, faucet_state=fs) == 0


def test_merge_state_drops_far_future_stamps():
    import time as _t
    now = int(_t.time())
    m = ff.merge_state({"FLOP": {"last_claim_ts": now - 100}},
                       {"FLOP": {"last_claim_ts": now + 10**6, "next_claim_ts": now + 10**8}})
    assert m["FLOP"]["last_claim_ts"] == now - 100 and m["FLOP"].get("next_claim_ts", 0) == 0
