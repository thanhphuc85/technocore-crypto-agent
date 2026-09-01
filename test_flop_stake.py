"""
Test cho flop_stake.py — ủy quyền đặt cọc (đường earn airdrop thứ 2).
Sổ cái dùng file tạm (path=) -> KHÔNG đụng token_ledger.json thật. KHÔNG chạm mạng.
"""
import flop_stake as fs
import token_manager as tm


def _ledger(tmp_path):
    return str(tmp_path / "ledger.json")


def test_delegate_simulation_moves_liquid_to_delegated(tmp_path):
    p = _ledger(tmp_path)
    tm.credit("100", path=p)
    r = fs.delegate("40", "val-a", path=p)
    assert r["outcome"] == "delegated_simulated"
    assert r["liquid_after"] == "60" and r["delegated_after"] == "40"
    st = fs.stake_status(path=p)
    assert st["total_delegated"] == "40" and st["liquid_balance"] == "60"
    assert st["by_validator"] == {"val-a": "40"}


def test_delegate_rejects_over_balance(tmp_path):
    p = _ledger(tmp_path)
    tm.credit("10", path=p)
    r = fs.delegate("40", "val-a", path=p)
    assert r["outcome"] == "skipped_insufficient"
    assert fs.stake_status(path=p)["total_delegated"] == "0"   # không đổi sổ cái


def test_delegate_rejects_bad_amount_and_missing_validator(tmp_path):
    p = _ledger(tmp_path)
    tm.credit("10", path=p)
    assert fs.delegate("0", "val-a", path=p)["outcome"] == "skipped_insufficient"
    assert fs.delegate("-5", "val-a", path=p)["outcome"] == "skipped_insufficient"
    assert fs.delegate("5", "", path=p)["outcome"] == "skipped_insufficient"


def test_undelegate_reverses_and_clears_when_zero(tmp_path):
    p = _ledger(tmp_path)
    tm.credit("100", path=p)
    fs.delegate("40", "val-a", path=p)
    r = fs.undelegate("40", "val-a", path=p)
    assert r["outcome"] == "undelegated_simulated" and r["liquid_after"] == "100"
    assert fs.stake_status(path=p)["by_validator"] == {}       # xoá key khi về 0


def test_undelegate_more_than_staked_rejected(tmp_path):
    p = _ledger(tmp_path)
    tm.credit("100", path=p)
    fs.delegate("10", "val-a", path=p)
    assert fs.undelegate("20", "val-a", path=p)["outcome"] == "skipped_insufficient"


def test_record_reward_bumps_rewards_and_liquid(tmp_path):
    p = _ledger(tmp_path)
    tm.credit("10", path=p)
    r = fs.record_reward("2.5", validator="val-a", path=p)
    assert r["outcome"] == "reward_recorded" and r["rewards_total"] == "2.5"
    assert r["liquid_after"] == "12.5"
    assert fs.record_reward("0", path=p)["outcome"] == "skipped_insufficient"


def test_testnet_skipped_unconfigured_leaves_ledger_untouched(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    tm.credit("100", path=p)
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.delenv("FLOP_STAKE_URL", raising=False)
    monkeypatch.delenv("FLOP_SUBMIT_URL", raising=False)
    monkeypatch.delenv("FLOP_RPC_URL", raising=False)
    r = fs.delegate("40", "val-a", path=p)
    assert r["outcome"] == "skipped_unconfigured"
    assert fs.stake_status(path=p)["total_delegated"] == "0"   # KHÔNG bịa giao dịch


def test_testnet_delegate_onchain_with_injected_submit(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    tm.credit("100", path=p)
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_STAKE_URL", "https://stake.invalid")
    seen = {}

    def fake(tx):
        seen.update(tx)
        return {"tx_hash": "0xSTAKE01"}

    r = fs.delegate("40", "val-a", path=p, submit_fn=fake)
    assert r["outcome"] == "delegated_onchain" and r["tx_hash"] == "0xSTAKE01"
    assert seen["action"] == "delegate" and seen["validator"] == "val-a"
    assert fs.stake_status(path=p)["total_delegated"] == "40"


def test_maybe_delegate_gated(tmp_path, monkeypatch):
    p = _ledger(tmp_path)
    tm.credit("100", path=p)
    monkeypatch.delenv("FLOP_STAKE_ENABLED", raising=False)
    assert fs.maybe_delegate("10", "val-a", path=p)["outcome"] == "skipped_off"
    monkeypatch.setenv("FLOP_STAKE_ENABLED", "true")
    assert fs.maybe_delegate("10", "val-a", path=p)["outcome"] == "delegated_simulated"
