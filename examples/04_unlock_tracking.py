"""
04 — 3:1 mainnet-unlock accounting with a fake ``submit_tx``.

Shows how real testnet spend accrues toward the mainnet unlock. Only
``spent_onchain`` spends count — simulated spends never do, so unlock credit
can't be farmed with mock spend. Here we flip ``TESTNET_ENABLED=true`` and inject
a **fake** ``submit_tx`` lambda that returns a canned tx hash, so ``spend``
takes the on-chain path without any network. ``unlock_status`` then reports
``unlocked_mainnet = spent_testnet / FLOP_UNLOCK_RATIO`` (default ratio 3).

No private key and no network are required. Writes to a throwaway temp ledger.

Requirements:
  - ``pip install -e .`` (or run from the repo root)

Run:
    python examples/04_unlock_tracking.py
"""

import os
import sys
import tempfile

# Allow running straight from a checkout without `pip install -e .`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import token_manager as tm

FAKE_RPC = "https://rpc.example.invalid"


def fake_submit_tx(tx: dict) -> dict:
    """Stand-in for a real relayer/RPC call — returns a canned tx hash, no network."""
    return {"tx_hash": "0xFAKEtxhash" + tx["amount"].replace(".", "")}


def main() -> int:
    ledger = os.path.join(tempfile.mkdtemp(prefix="flop-example-04-"), "ledger.json")
    tm.credit("100", memo="faucet top-up", path=ledger)

    # A simulated spend first — proves it does NOT accrue toward the unlock.
    tm.spend("5", "simulated spend", path=ledger)
    sim = tm.unlock_status(path=ledger)
    print(f"after simulated spend : spent_testnet={sim['spent_testnet']} (0 — correct)")

    # Now real testnet spend via the injected fake submit_tx.
    os.environ["TESTNET_ENABLED"] = "true"
    try:
        for _ in range(3):
            r = tm.spend(
                "3", "testnet spend", path=ledger,
                submit_tx=fake_submit_tx, rpc=FAKE_RPC, log=lambda _m: None,
            )
            print(f"spend : {r['outcome']}  tx={r['tx_hash']}  balance={r['balance_after']}")
    finally:
        del os.environ["TESTNET_ENABLED"]

    st = tm.unlock_status(path=ledger)
    print(
        f"unlock: spent_testnet={st['spent_testnet']}  ratio=1/{st['ratio']}  "
        f"unlocked_mainnet={st['unlocked_mainnet']}  claimable={st['claimable']}"
    )
    # 9 testnet FLOP spent / 3 == 3 FLOP unlocked on mainnet
    return 0 if st["unlocked_mainnet"] == "3" else 1


if __name__ == "__main__":
    raise SystemExit(main())
