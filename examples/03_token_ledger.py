"""
03 — FLOP token ledger: credit, spend (simulation), check balance.

Exercises ``token_manager`` end to end in the default **simulation** mode
(``TESTNET_ENABLED`` unset): ``credit`` records a faucet top-up, ``spend`` debits
a MOCK balance and logs the ``[SIMULATION] Spent ...`` line (nothing touches a
chain), and ``check_balance`` reports the running balance. Balance math uses
``Decimal`` so ``0.001`` is exact.

No private key and no network are required. This example writes its ledger to a
throwaway temp file so it never touches ``token_ledger.json`` in your checkout.

Requirements:
  - ``pip install -e .`` (or run from the repo root)

Run:
    python examples/03_token_ledger.py
"""

import os
import sys
import tempfile

# Allow running straight from a checkout without `pip install -e .`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import token_manager as tm


def main() -> int:
    ledger = os.path.join(tempfile.mkdtemp(prefix="flop-example-03-"), "ledger.json")
    print(f"mode   : {tm.ledger_mode()}  (set TESTNET_ENABLED=true for real testnet transfers)")
    print(f"ledger : {ledger}")

    c = tm.credit("100", memo="faucet top-up", path=ledger)
    print(f"credit : +{c['amount']} {c['token']}  -> balance {c['balance_after']}")

    for memo in ("Gemini inference", "Gemini inference", "market snapshot"):
        s = tm.spend("0.001", memo, path=ledger)
        print(f"spend  : {s['outcome']}  -{s['amount']} {s['token']}  -> balance {s['balance_after']}")

    print(f"balance: {tm.check_balance('FLOP', path=ledger)} FLOP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
