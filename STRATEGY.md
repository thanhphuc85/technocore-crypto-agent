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

## Default posture: maximize *legitimate* inference throughput

Because the stated mechanic pays pro-rata on inference spend with **no announced sybil
defense**, the default is to spend as much as legitimately possible — **not** to throttle
it. The anti-sybil knobs shipped in PR #29 are **insurance to flip on**, not defaults.

| Knob | Default | Flip ON when |
|---|---|---|
| `FLOP_PACE_JITTER_PCT` | **off** | FLOP announces/hints at sybil or behavioral filtering |
| `FLOP_MAX_SPENDS_PER_HOUR` | **off** | same — or you want a believable frequency ceiling |
| `FLOP_FAUCET_MAX_PER_DAY` | **off** | same |
| `FLOP_FAUCET_JITTER_MIN` | **off** | same |
| `FLOP_FAUCET_DEMAND_ONLY` | **off** | you want to conserve rather than cycle max FLOP |
| `FLOP_ORGANIC_ONLY` | **judgment call** | see below |

`FLOP_ORGANIC_ONLY` ties every spend to a real inbound @mention. That is cheap insurance
against **retroactive** sybil filtering (your activity looks organic), but it **caps spend
to inbound message volume**. Leave it off to scale spend beyond mentions; turn it on if you
weight the retroactive-clawback risk higher than the throughput you'd give up.

## Residual risks (do not ignore)

1. **Teaser ≠ final rules.** "No sybil rules stated" is not "no sybil rules." Many airdrops
   (Arbitrum, Optimism, zkSync, …) filtered farming wallets **retroactively** despite not
   pre-announcing it. The knobs above exist so this is a one-line env flip, not a rebuild.
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

## Timeline

- **Testnet:** Q4 2026, runs ~90 days.
- **Mainnet:** Q1 2027.
- Implication: **don't over-build now.** The highest-value next action is to watch for
  FLOP's published (a) faucet + inference endpoint spec and (b) any anti-sybil rules —
  those two facts finalize the posture above.
