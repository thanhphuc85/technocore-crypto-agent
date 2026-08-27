"""
technocore_agent — public API facade for the Technocore agent SDK.

This package is a thin, stable re-export layer over the flat modules that make up
the SDK (``agent_cron``, ``token_manager``, ``flop_pacer``, ``flop_faucet``,
``flop_tx``). It exists so downstream code can write:

    import technocore_agent
    pk  = technocore_agent.load_private_key()
    did = technocore_agent.did_of(pk)
    technocore_agent.post_message(pk, did, "signed hello")

...instead of importing the underlying modules directly. Nothing here changes
behavior — the flat modules remain fully importable and unchanged
(``import agent_cron``, ``import token_manager`` still work exactly as before).
``agent_cron`` stays the reference 24/7 agent; ``technocore_agent`` is just the
surface.

Importing this package never requires a private key or network access — the
Ed25519 seed is read only when you call ``load_private_key()``.
"""

__version__ = "1.2.1"

# --- Core protocol client (agent_cron) --------------------------------------
from agent_cron import (
    build_reply,
    did_of,
    fetch_messages,
    kv_get,
    kv_set,
    llm_reply,
    load_private_key,
    post_message,
    sign_message,
)

# --- Auto-cycle faucet scaffold (flop_faucet) ----------------------------
from flop_faucet import (
    faucet_enabled,
    run_faucet_cycle,
)

# --- Dynamic Spend Rate pacer (flop_pacer) ---------------------------------
from flop_pacer import (
    daily_budget,
    next_spend_amount,
    pacing_status,
    record_spend,
)

# --- submit_tx adapters (flop_tx) ----------------------------------------
from flop_tx import (
    build_submit_tx,
    evm_submit_tx,
    relay_submit_tx,
)

# --- FLOP token ledger + 3:1 mainnet-unlock accounting (token_manager) ------
from token_manager import (
    check_balance,
    claim_mainnet_unlock,
    credit,
    default_token,
    ledger_mode,
    load_ledger,
    meter_inference,
    sign_transaction,
    spend,
    testnet_enabled,
    unlock_ratio,
    unlock_status,
)

__all__ = [
    "__version__",
    # agent_cron
    "load_private_key",
    "did_of",
    "sign_message",
    "post_message",
    "fetch_messages",
    "kv_set",
    "kv_get",
    "llm_reply",
    "build_reply",
    # token_manager
    "credit",
    "spend",
    "check_balance",
    "sign_transaction",
    "unlock_status",
    "claim_mainnet_unlock",
    "meter_inference",
    "load_ledger",
    "ledger_mode",
    "testnet_enabled",
    "unlock_ratio",
    "default_token",
    # flop_pacer
    "next_spend_amount",
    "record_spend",
    "pacing_status",
    "daily_budget",
    # flop_faucet
    "run_faucet_cycle",
    "faucet_enabled",
    # flop_tx
    "build_submit_tx",
    "relay_submit_tx",
    "evm_submit_tx",
]
