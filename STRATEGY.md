# FLOP Airdrop Strategy — Agent Track

> Decision record for how this agent plays the Flop Network airdrop. Source: the
> official teaser at <https://flop.finance/teaser/> (read 2026-08-28). **A teaser is
> not the final rule set** — re-verify every number below against the full testnet
> rules when they publish (testnet is slated for Q4 2026).

## What the airdrop actually rewards (per the teaser)

- **Agent pool: 7% (up to 1.2B $FLOP).** Allocation is *"based largely on what they
  spend on inference over the testnet, along with various prizes."* The yellowpaper
  decision log (D-0438) splits it as **600M "agent spend cap" + 600M "testnet prize
  pool"**: steady compute spend targets one half; the other half is prizes whose rules are
  not published yet - enter FLOP's testnet contests when they are announced.
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
   guessed endpoints. **Seen on devnet (2026-10-23):** FLOP is a Substrate chain - accounts
   are **SS58** (prefix 42), not EVM, so `EVM_PRIVATE_KEY` plays no part. The faucet is
   `POST https://faucet.flop.network/fund {"address": "<SS58>"}` -> `{success, amount,
   txHash, tier}`; quick tier 25 FLOP, verified-human tier 500-2000 FLOP, one grant per
   address and per identity every 24 h (`/health` -> `limits`). Compute is bought through
   the OpenAI-compatible gateway `api.flop.network` (enroll the wallet with a signed
   challenge -> bearer key -> `/v1/chat/completions`, receipts at `/v1/keys/me/receipts`),
   which does not match the scaffold's mempool/PoUI/settle shape. **`flop_gateway.py` now
   implements that path** (`FLOP_SESSION_BACKEND=gateway`, see "Gateway backend" below).
   Fill `FLOP_FAUCET_URL` / `FLOP_MEMPOOL_URL` and fit the scaffold
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
- The radar (`flop_radar.py`, hourly) also alerts when `api.flop.network/health` reports a
  network other than `devnet`, and when the "Current testnet and airdrop status" section
  of **flop.finance/official/** changes (FLOP Labs edits it with every testnet/airdrop
  announcement; airdrop eligibility itself is announced on x.com/flop_labs).

### Gateway backend (`flop_gateway.py`)

Measured against the live devnet gateway on 2026-10-24 (read-only: health, catalog,
estimate, challenge): one model `qwen/qwen2.5-0.5b-instruct`, 0.006 FLOP per prompt and
per completion token (512 tokens ~ 3.15 FLOP, our local sizing matches `/models/estimate`
exactly), 5 miners, an 8-line `flop-devnet-enroll/v1` challenge naming our SS58 address.

- **Enroll:** sign the challenge with the wallet key (only if it names *this* gateway and
  *our* address), get a model-scoped bearer. The bearer is kept in `state.json` **encrypted
  (AES-GCM, key derived from the agent seed)** - a workflow that can read the Actions cache
  but not the secrets sees ciphertext only. Re-enrolled when it is about to expire or on 401.
- **Pace:** spend the remaining quota linearly until it expires (or the UTC day ends),
  accumulating small amounts until one call is worth it; at most
  `FLOP_GATEWAY_MAX_CALLS_PER_RUN` (2, the allowance allows 2 requests/min) per run, each
  sized so it costs about its share and sent with `max_cost_flop` so the **gateway** caps
  the spend. Simulated: 25 FLOP over 20 h is spread evenly and gone before expiry.
- **Errors:** 401 -> re-enroll next run; 403 unfunded / 429 / 503 -> back off
  (`Retry-After`); 402 -> log the payment shape, never pay; missing `cost_flop` -> nothing is
  booked from a guess.
- **Rehearsal before testnet:** with `TESTNET_ENABLED` off the same code runs on devnet and
  books nothing. It needs a funded devnet wallet (>= 1 FLOP from faucet.flop.network) and
  each account gets one 24 h allowance only, so it is a one-day dry run.

### Day-one checklist (Variables only — no code change)
1. `TESTNET_ENABLED=true` **first**; `FLOP_TESTNET_START=2026-11-03` (or the confirmed
   date). The faucet refuses to claim while the ledger is in simulation mode
   (`faucet=skipped_simulation`) so a real claim is never booked into the mock ledger.
2. `FLOP_SESSION_MODEL_HASH` from the published model catalog; delete
   `FLOP_SESSION_SIM_MODEL_HASH`.
3. **Gateway backend (the path FLOP published):** `FLOP_SESSION_BACKEND=gateway` (the default
   once `TESTNET_ENABLED` is on; set `mempool` only to force the old scaffold) and
   **`FLOP_WALLET_USE_DID=on` - the DID key is the FLOP wallet** (SS58
   `5CFP215CEbCtDduwG1Nzu5rmYXKfHc47wSATUi1oRvrkQnUP` for the current DID). Decided
   2026-10-24: tclk's FLOP HTLC rail (`@flop-labs/tclk-flop`, tclk #210) binds each party's
   FLOP account to *the exact Ed25519 key of its `did:key`* and rejects any other wallet,
   and the airdrop is credited to the account that bought the compute - so buying and
   any future earning must happen on one account, the DID. Keep it for the whole testnet
   (switching splits the record across two accounts). Mitigate the single-key risk with
   the offline seed backup and the on-chain spend limit (item 9). `FLOP_WALLET_SEED_HEX`
   (separate wallet) stays supported but is not the plan. Prompts: `FLOP_GATEWAY_PROMPTS`
   (`a|b|c`, your own - no shared default, receipts carry `prompt_hash`) or the bodies of
   `FLOP_KIBBLE_REQUEST_QUESTIONS`. If testnet gets its own host, `FLOP_GATEWAY_URL`.
   The agent spends only when `/health` reports `FLOP_GATEWAY_NETWORK` (default `testnet`
   with `TESTNET_ENABLED`, `devnet` without it - devnet runs are a rehearsal and are never
   booked). The scaffold mempool path stays for reference: `FLOP_MEMPOOL_URL`,
   `FLOP_RPC_URL`/`FLOP_SUBMIT_URL` (+ `FLOP_TX_MODE`).
4. `FLOP_FAUCET_ENABLED=on`, `FLOP_FAUCET_URL` (devnet shape:
   `https://faucet.flop.network/fund`; a bare domain gets `/fund` appended). `FLOP_FAUCET_ADDRESS` can stay empty: the faucet then funds the gateway wallet (the DID, item 3) - the account that buys the compute; a different address triggers a one-time warning.
   With an address set the adapter sends the real body and reads the 24 h cooldown from
   the faucet's `/health`, so `auto` waits a full day instead of probing hourly.
   **Verified-human tier (operator only):** prove personhood once on the faucet page with
   BrightID or Self *on the agent's FLOP address*, then set `FLOP_FAUCET_TIER=linked` and
   `FLOP_FAUCET_PROVIDER=brightid|self`; the agent then requests the larger grant daily.
   The MetaMask/SIWE path (Human Passport / Guild score) needs a fresh human signature
   every claim, so the agent refuses it - claim that way by hand **and set
   `FLOP_FAUCET_TIER=manual`**. ⚠️ **Trap (measured on devnet 2026-10-24):** the 24 h cooldown
   is shared by every tier on one address - after a quick 25 FLOP grant the verified request
   is refused ("this address was just funded"). An agent left on `quick` would take the 25
   first every day and block the 500-2000 grant. Pick one per address: `manual` (you claim
   verified by hand), `linked` (agent claims verified via BrightID/Self) or `quick` (no
   human proof). On `quick` the agent sends a one-time Telegram reminder of this. One human
   proof, one address: never link the same identity to several wallets. Already set and
   inert until then:
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
   3. **Price depends on what "compute purchased" measures — check the spec first.** The
      airdrop page shares the agent pool *"pro rata to compute purchased in settled
      sessions"*, which reads two ways:
      - **FLOP spent:** price does not change the score (100 FLOP counts as 100 whether the
        miner is cheap or dear), so price is only a tie-break — ranking cheapest-first just
        concentrates spend on one miner.
      - **FLOPs bought** (the unit miners are scored in: *"verified compute served, measured
        in floating-point operations"*): a cheaper miner buys more compute for the same
        faucet FLOP, so a lower FLOP-per-FLOPs price directly raises the score. Then rank by
        price per FLOPs among the miners that pass step 1, still under the step-2 spread cap
        and never a fleet miner.
      Until the spec settles it, log price per FLOPs for every settled session so the choice
      can be made from real data on day one.
   Never skip a session to "save budget": unspent faucet FLOP is lost ("If you don't use
   it, you lose it"). Wrong work is already handled by the protocol — the agent pays
   nothing and can challenge (100 FLOP bond; upheld fraud slashes the miner in full).
8. **Settlement / payment model (code, from the real API).** *Gateway backend:* the gateway
   charges the enrolled allowance per request. Measured on devnet (2026-10-24): `usage` has
   only token counts (no `cost_flop`), so pacing reads the spend from the `/keys/me` quota;
   `/v1/keys/me/receipts` does **not** follow the OpenAPI `InferenceReceipt` schema - each
   receipt has `channel_id`, `request_id`, `reserved_spend`, `settled_spend(_raw)`, `state`,
   `response_status`, `terminal_event`. The testnet ledger is booked **from settled
   receipts** (`settled_spend`, deduped by `request_id`), i.e. exactly "compute purchased
   in settled sessions"; receipts are read at the start of every run, so late settlements
   are booked even when that run's calls fail. A `402` with x402 fields (`price`, `asset`, `hash_lock`,
   `destination`, `timeout_blocks`) is **logged and never paid automatically** - fit that
   by hand if testnet uses pay-per-request. *Scaffold mempool backend:* the scaffold assumes the proof
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
