# FLOP Airdrop Strategy — Agent Track

> Decision record for how this agent plays the Flop Network airdrop. Source: the
> official teaser at <https://flop.finance/teaser/> (read 2026-08-28). **A teaser is
> not the final rule set** — re-verify every number below against the full testnet
> rules when they publish (testnet is slated for Q4 2026).

## What the airdrop actually rewards (per the teaser)

- **Agent pool: 7% (up to 1.2B $FLOP).** Allocation is *"based largely on what they
  spend on inference over the testnet, along with various prizes."*
- Fixed pool + "based on what they spend" ⇒ almost certainly **pro-rata**: your share
  ≈ (your inference spend) / (total inference spend by all agents). It is a
  **competitive spend race within a capped pool**, so raw spend relative to other
  agents is the primary lever.
- **Locked-token unlock is a *separate* mechanic** — not how much you earn, but how you
  make earned tokens liquid: *"every 3 $FLOP spent on inference unlocks 1 airdropped
  $FLOP."* Modeled already by `FLOP_UNLOCK_RATIO` (default `3`) and `unlock_status()`.
- **Anti-sybil / per-identity caps / KYC / faucet cooldowns / rate limits: NOT stated
  anywhere in the teaser.**

## Default posture: max legitimate spend, anti-sybil defaults ON

The original posture ("no announced sybil defense → knobs off, flip on when FLOP hints at
filtering") is **superseded**. The testnet page (flop.finance/testnet, §06) now publishes
the rules:

- **What counts for agents:** "compute purchased in settled sessions. Holding test tokens
  earns nothing." Minimum-activity floors exist per role (values not yet published).
- **One participant, one score:** wallets under common control are scored as one.
- **Independent demand only:** spend routed to a miner under common control, or circulated
  between wallets under common control, is not credited.
- **Fraud forfeits:** "an account flagged for manufactured activity forfeits its allocation".

That is exactly the flip condition, so the behavioural knobs now default **on in code** —
any fork that pulls this repo without setting Variables is protected too. Set `0`/`off`
explicitly to disable.

| Knob | Default | Notes |
|---|---|---|
| `FLOP_PACE_JITTER_PCT` | **20** | ±% noise on the pacer's due amount; `0` disables |
| `FLOP_FAUCET_JITTER_MIN` | **30** | 0..N min added to the faucet cooldown; `0` disables |
| `FLOP_ORGANIC_ONLY` | **on** | metering requires a real `event_id`; `off` disables |
| `FLOP_SESSION_JITTER_PCT` | **20** | ±% on session flops/latency/fee + model rotation |
| `FLOP_MAX_SPENDS_PER_HOUR` | off | frequency ceiling, if ever needed |
| `FLOP_FAUCET_MAX_PER_DAY` | off | set it to the real faucet's cap once published |
| `FLOP_FAUCET_DEMAND_ONLY` | off | conserve instead of claiming on schedule |

`FLOP_ORGANIC_ONLY` costs **no airdrop**: it only gates *metering* (the proxy debit for
external LLM calls), which never counted. The path that counts — settled sessions — is not
gated by it.

**Multiple DIDs with the same pattern = common-control risk.** Forks that run identical
code on identical cadence look like one participant split across accounts. Jitter defaults
help, but each fork operator must stay genuinely independent (own keys, own config, own
question sets, cross-denied miner DIDs — see `PROACTIVE_DENY_DIDS`).

## Residual risks (do not ignore)

1. **Published rules are still a draft.** The testnet page is marked provisional and the
   Yellow Paper is not final; floors and operational rules ship with the testnet. Expect
   stricter filtering, not looser — many airdrops (Arbitrum, Optimism, zkSync, …) filtered
   farming wallets **retroactively**.
2. **"largely" + "various prizes"** — allocation is not 100% pro-rata by spend. Some share
   is prizes with **unpublished** criteria (likely quality / uptime / achievements). Don't
   optimize spend so hard you miss what the prizes reward.
3. **Capped pool ⇒ compressing EV.** If everyone burns, shares dilute. $FLOP has no
   confirmed value; this is speculative.

## Technical gaps to close when FLOP opens testnet

1. **Proxy vs real spend.** `meter_inference()` debits FLOP per external Gemini/OpenAI
   call as a *proxy* — in **simulation only**. With `TESTNET_ENABLED` it returns
   `skipped_testnet`: there `spend()` is a real on-chain transaction, and a "gemini kibble"
   memo would burn real testnet FLOP that never counts and competes with sessions for the
   faucet allowance. Real earning goes through settled sessions only.
2. **Faucet + mempool spec.** `flop_faucet.py` and the mempool submit refuse to POST to
   guessed endpoints. Fill `FLOP_FAUCET_URL` / `FLOP_MEMPOOL_URL` and fit the scaffold
   wire format once FLOP publishes them. The faucet has **per-identity limits** and a
   claim period ("hourly or daily — not yet decided"): match `FLOP_FAUCET_MAX_PER_DAY` /
   `FLOP_FAUCET_COOLDOWN_HOURS` to it and do **not** set `FLOP_FAUCET_REFILL_BELOW`, so the
   agent claims its full allowance every period.
3. **Spend the whole allowance.** FLOP: *"an agent's inference volume on testnet is bounded
   by the faucet"*, and *"If you don't use it, you lose it."* `FLOP_SESSION_BUDGET=auto`
   spends everything available today, spread evenly over the day's runs by the pacer
   (catches up after failed runs, capped per run by `FLOP_MAX_PER_RUN`). Sessions keep a
   **constant unit price** (`FLOP_INFERENCE_COST` / `FLOP_SESSION_FLOPS`): spending more
   buys more FLOPs, it never pays more for the same work. Set that price pair from the real
   market on day one; `FLOP_SESSION_MAX_FLOPS` caps one session's size if miners need it.
4. **Sessions serving real jobs — nice to have, not required.** FLOP: *"Instruct your
   agent to actually spend it on an inference request; the task itself does not matter."*
   Routing kibble/tclk inference through sessions would still be better use of the
   compute; do it once FLOP documents how a session returns output.

## Timeline

Source: docs.flop.network FAQ "Testnet & Launch Timeline" (updated 2026-10-20).

- **Testnet: target Tuesday 3 November 2026**, ~90 days; a target, confirmed as devnet
  onboarding completes. *"earlier participation means a larger share of the low-supply
  months"* — be live on day one.
- **Mainnet / TGE:** early February 2027.
- Watch **docs.flop.network/networks/testnet**: RPC, Gateway and Model catalog read
  "Unavailable — no verified public value" until the network is ready.

### Day-one checklist (Variables only — no code change)
1. `TESTNET_ENABLED=true` **first**; `FLOP_TESTNET_START=2026-11-03` (or the confirmed
   date). The faucet refuses to claim while the ledger is in simulation mode
   (`faucet=skipped_simulation`) so a real claim is never booked into the mock ledger.
2. `FLOP_SESSION_MODEL_HASH` from the published model catalog; delete
   `FLOP_SESSION_SIM_MODEL_HASH`.
3. `FLOP_MEMPOOL_URL`, `FLOP_RPC_URL`/`FLOP_SUBMIT_URL` (+ `FLOP_TX_MODE`).
4. `FLOP_FAUCET_ENABLED=on`, `FLOP_FAUCET_URL`. Already set and inert until then:
   `FLOP_FAUCET_COOLDOWN_HOURS=auto` (probe hourly, then follow the faucet's
   `Retry-After` / `next_claim_at`), `FLOP_FAUCET_MAX_PER_DAY=24`, `FLOP_FAUCET_JITTER_MIN=0`.
   If FLOP publishes a fixed period/cap, set those numbers instead. Only use URLs FLOP
   publishes - `*.invalid` hosts and `REPLACE_WITH_...` values in the docs are placeholders.
5. `FLOP_SESSION_BUDGET=auto`, `FLOP_MAX_PER_RUN=2%`, `FLOP_SESSION_MAX_PER_RUN=3`,
   `FLOP_INFERENCE_COST` + `FLOP_SESSION_FLOPS` = observed market price.
6. Watch the first runs: `session=N✓ settle/spent_onchain … due=…`. `pace-wait` all day
   means the price floor is above the budget; `submit/skipped_unconfigured` means an
   endpoint is still missing.
7. **Miner selection (code, once the API shows what it returns about miners).** FLOP lets
   buyers choose ("buyers choose their miners"; "pick a capable miner"), but no miner
   list, price, status or error history is published yet, so do not guess weights. Build
   it from the real API fields, in this priority order:
   1. **Filter:** serves the pinned model, meets the latency bound, not a fleet miner
      (`PROACTIVE_DENY_DIDS` — "Independent demand only"), not recently failed/challenged.
   2. **Spread:** cap any one miner's share of our sessions (the `[miners]` concentration
      line already measures it; warning at `FLOP_MINER_CONC_WARN`). Prefer the eligible
      miner we have bought from least.
   3. **Price only as a tie-break.** Ranking by cheapest first concentrates spend on one
      miner, and if the airdrop counts FLOP spent, a cheaper miner does not raise it.
   Never skip a session to "save budget": unspent faucet FLOP is lost ("If you don't use
   it, you lose it"). Wrong work is already handled by the protocol — the agent pays
   nothing and can challenge (100 FLOP bond; upheld fraud slashes the miner in full).
8. **Settlement / payment model (code, from the real API).** The scaffold assumes the proof
   comes back in the mempool response and then pays with a separate signed transfer that
   has **no recipient**. The docs describe something else: the miner opens a compute
   channel that pins the **escrowed payment**, proves later, and validators settle in a
   block. Day-one simulation of both realistic shapes paid **0 FLOP**. Fit from the real
   API: (a) how a session's payment is escrowed/released, (b) how to poll a pending
   session (`flop_pending_sessions` in state keeps them meanwhile), (c) record spend when
   the network confirms settlement, and reconcile the ledger with the on-chain balance.
   The `[real-spend]` watchdog sends a Telegram alert after `FLOP_REAL_SPEND_ALERT_RUNS`
   (default 6 ≈ 30 min) runs without a `spent_onchain` session, with the stage it is stuck
   at and the real field names returned — that log is the fitting guide.
9. **On-chain spend limit (account setting, before the first real session).** The chain
   offers *"pre-set spend limits, delegation, and escrow"* on the account itself
   (flop.finance/intro/agent). Set the agent account's daily limit to about one day of
   faucet allowance. It is a second safety layer that does not depend on our code: even
   if the pacer, the precheck or an adapter misbehaves, the chain refuses to spend more.
   Done by the operator from the published tooling, never by the agent itself.
10. **Never wire automatic on-chain challenges.** A discretionary challenge posts a
    **100 FLOP bond**, forfeited if frivolous (protocol-parameters). `verify_poui` only
    checks that the proof fields are present, so `dispute()` must stay a local ledger
    note; challenge by hand, if ever, after reading the real proof format.
