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

1. **Proxy vs real spend.** `meter_inference()` currently debits FLOP per external
   Gemini/OpenAI call as a *proxy*. Real earning = spending **testnet FLOP on FLOP's own
   inference network** (miners earn 85% of the fee), reached through the `spend()` seam
   (`TESTNET_ENABLED` + `FLOP_RPC_URL`/`FLOP_SUBMIT_URL` + injected `submit_tx`). Point that
   seam at FLOP's real inference-payment endpoint once published — accounting logic is
   unchanged.
2. **Faucet spec.** `flop_faucet.py` refuses to POST to a guessed endpoint. Fill
   `FLOP_FAUCET_URL` + inject a real `claim_fn` when the scheme is announced. Check whether
   the real faucet has a cooldown/cap (the module assumes a 24h default that may not match).
3. **The real throughput ceiling** is *faucet acquisition rate × miner inference capacity*,
   not ambition. Measure it on day one of testnet.
4. **Make every session a real need.** Today a session is bought once per run and its
   inference output is not used — kibble/tclk answers still come from Gemini. Once FLOP
   publishes how a session returns output, route that real workload through sessions so
   each purchase serves a job. Do not "anchor" session counts to job counts before then:
   that only looks organic while the compute still goes unused.

## Timeline

- **Testnet:** Q4 2026, runs ~90 days.
- **Mainnet:** Q1 2027.
- Implication: **don't over-build now.** The highest-value next action is to watch for
  FLOP's published (a) faucet + inference endpoint spec and (b) any anti-sybil rules —
  those two facts finalize the posture above.
