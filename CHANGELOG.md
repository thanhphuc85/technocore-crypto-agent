# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **tclk/1 payee (deal-making), DRY-RUN by default.** New `flop_tclk.py` speaks the [tclk/1]
  (github.com/flop-labs/tclk) HTLC/PTLC convention as the **payee**: it discovers valid offers on
  `/r/tclk-offers` (sender is payer, hash-lock, a rail we accept, within deadline), mints a
  preimage, and builds a spec-correct `accept` frame (statement = `sha256(preimage)`, contract id
  over canonical `{offer, accept}`). The canonical-JSON + ASCII-escape + domain-hash are a
  byte-exact port of the reference `src/frames.ts` — verified against 50 live JS-generated offer
  ids, and pinned by a real-offer test vector. Gated `FLOP_TCLK_ENABLED` (default OFF); when on it
  starts **DRY-RUN** (`FLOP_TCLK_DRY_RUN=off` to go live) which only logs `would ACCEPT`, posts
  nothing, and stores no secret. **Safety by construction: the module only discovers + accepts — it
  ships no `lock`/`reveal` builder at all**, so it can never auto-claim funds; revealing (the claim)
  stays a human step. Shares the kibble write-outage health-guard. Alpha/testnet/unaudited per the
  spec — no real value. Adds `flop_tclk` + 11 tests.
- **Kibble health-guard: skip the worker when the write-path is down.** technocore.chat can keep
  serving reads (GET 200) while rejecting writes (POST 503 / read-timeout). In that state the
  kibble worker used to spend real DeepSeek inference answering a job, then fail to DELIVER (503)
  and log a hanging FLOP spend for work that never landed. A per-run POST-health counter
  (`posts_degraded()` — true only when every POST attempted this run failed) now gates the worker:
  during a write outage it is skipped entirely (`kibble=skip-outage`), so no inference is wasted
  and the kibble cursor doesn't advance, leaving the jobs to be picked up once the server recovers.
  A single successful POST clears the guard, so a transient blip won't trip it.
- **Wider market coverage: 41 coins + 9 A2A verbs.** The coin table grew from 15 to 41 tickers
  (added LTC, BCH, UNI, SHIB, PEPE, WBTC, SUI, APT, ARB, OP, INJ, LDO, AAVE, FIL, ETC, FTM, ALGO,
  HBAR, VET, ICP, STX, SEI, TIA, RUNE, GRT, MKR — each with a Binance fallback pair; all 41
  CoinGecko ids verified to resolve live), so `!price <coin>` and the A2A `price` verb answer far
  more assets and live-grounding picks them up in free-form replies. The agent-to-agent protocol
  gained `market`, `top`, `trending`, `dominance`, `gas` verbs (was just `price|fear|help|about`),
  each returning a parseable line — so another agent can pull the same data the human `!commands`
  expose. The verb list is centralised so `help`, `about`, and `!help` stay in sync.
- **Agent-to-agent (A2A) message protocol.** Other agents can now "call the agent like an API"
  with a terse, machine-readable command — `@handle price eth` / `fear` / `help` / `about` (no
  `!`, verb + at most one arg) — and get back a single parseable line
  (`[NguyenVuLV] @caller ok price ETH 2522.0 (+2.4% 24h) | src=coingecko/binance | t=<iso>`, or
  `err <reason>`). Anything chattier falls through to the normal live-grounded LLM reply, and human
  `!price` is untouched. The protocol is **read-only by design** — no verb writes state from
  untrusted input (no remote `remember`/`kv-set`), so a hostile peer can't inject into the agent's
  memory through it. Advertised in `!help` and via the `about` verb. ([`a2a_reply`](agent_cron.py))
- **Structured peer profile + standing goal (memory upgrade).** Alongside the raw q/a turns, the
  agent now keeps a compact per-peer profile keyed by DID (preferred language + most-recent coins)
  and injects it as one context line, so replies recall *who this peer is* without replaying whole
  turns. A standing **goal** (`AGENT_GOAL`) is prepended to every inference's system prompt so the
  agent stays on-mission instead of drifting into a chatbot, and is mirrored to a public
  `/kv/<ns>/goal` note for humans/agents to audit what it's doing.
- **Duplicate-post guard.** Replies and proactive messages are de-duplicated per *(recipient,
  content)* within a 6h window, so an echo-loop with one peer can't make the agent repeat the exact
  same line; identical generic lines to *different* peers are still allowed. Time-gated broadcasts
  (telemetry/manifest/alert) are unaffected.
- **Compute-buying scaffold: inference sessions (3:1) + stake delegation.** Aligns the agent
  with the two — and only two — airdrop-earning paths in the FLOP agent spec
  (`intro.flop.network/agent.html`): paying miners for inference, and delegating stake.
  [`flop_session.py`](flop_session.py) models the 5-field session request (model-weight hash ·
  max latency · FLOPs · security flags · fee) → mempool submit → PoUI → `verify_poui` →
  `settle()` (via `token_manager.spend()`, so real settlement accrues the 3:1 unlock) or
  `dispute()`; `run_inference_session()` runs the full loop, with a mock miner/PoUI in
  simulation. [`flop_stake.py`](flop_stake.py) adds `delegate`/`undelegate`/`record_reward`/
  `stake_status` on the shared `token_ledger.json` (a small public `token_manager.save_ledger`
  helper was added for it). Both are gated default-OFF (`FLOP_SESSION_ENABLED` /
  `FLOP_STAKE_ENABLED`), never fabricate a tx (missing endpoint ⇒ `skipped_unconfigured`), and
  are honest about scope (`verify_poui` checks linkage/presence, not cryptographic soundness; no
  invented stake-reward rate). Preparation only — earns nothing until FLOP testnet is live. Adds
  17 tests (`test_flop_session.py`, `test_flop_stake.py`).
- **Durable state mirrored to KV + optional multi-runner coordination.** The broadcast cooldown
  timers (`last_telemetry`/`last_manifest`/`last_digest`/`last_recap`) plus cursor and weekly/alert
  state are now mirrored to the KV store (`hydrate_durable_from_kv` / `persist_durable_to_kv`) and
  re-hydrated at startup, so the agent won't re-post broadcasts even if the GitHub Actions
  `state.json` cache is evicted. Also adds a `RUNNER_ROLE` (`primary`/`backup`) + KV **heartbeat**
  handoff so a second runner can be added later without double-posting: the primary stamps a
  heartbeat each run; a `backup` **stands down** while that heartbeat is fresh
  (`BACKUP_STANDBY_MINUTES`, default 45) and only takes over when it goes stale. Manual
  `workflow_dispatch` bypasses standby. The Actions workflow defaults to `RUNNER_ROLE=primary`
  (single runner). Adds tests for heartbeat freshness, durable hydrate/persist, and the
  standby/force gate. The scheme is clean for one primary + one backup; a third concurrent runner
  would need a KV lease/lock.
- **DeepSeek is now the primary LLM provider, with Gemini as the fallback.** `LLM_PROVIDER=auto`
  (default) builds a provider chain **DeepSeek → Gemini → OpenAI**, keeping only providers that
  have a key; if the primary errors at call time, the agent automatically retries the next one
  in the chain (`_provider_chain()` / `_provider_reply()`). Pinning `LLM_PROVIDER=deepseek|gemini|openai`
  selects a single provider with no fallback. New env vars: `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`
  (default `deepseek-chat`), `DEEPSEEK_BASE_URL` (default `https://api.deepseek.com`,
  OpenAI-compatible). DeepSeek and OpenAI now share one `_openai_compatible_reply()` caller, and
  the output guard's `sk-…` secret pattern already blocks leaked DeepSeek keys. README + workflow
  updated; adds tests for chain selection, failover, and the DeepSeek endpoint.

### Fixed
- **Kibble answers that legitimately begin with "Skip" are no longer dropped as declines.**
  `answer_kibble_job()` detected a model decline with `text.upper().startswith("SKIP")`, which
  also killed valid deliverables like *"Skip lists are a data structure…"* / *"Skip connections
  in ResNets…"*. A decline is now recognized only when the response is short (≤40 chars) and
  starts with `SKIP`, matching the prompt's "reply with exactly 'SKIP'". Skip logging now
  distinguishes *empty/guarded* output from an explicit *SKIP* so the cause is visible in run
  logs. Adds tests.
- **`FLOP_KIBBLE_TYPES` no longer silently accepts every job type under GitHub Actions.**
  A workflow maps an unset Repo Variable to an **empty string** (the env var exists, value
  `""`), so `os.environ.get("FLOP_KIBBLE_TYPES", <default>)` returned `""` — not the default —
  which parsed to an empty list, and `select_jobs()` treated an empty allow-list as *"accept
  all"*, so the worker answered `research`/`analyze` jobs it was meant to skip (seen live: a
  `research` job answered with a `Sources:` line). Two-layer fix: config now falls back to the
  default when the env value is blank, and `select_jobs()` now treats an empty whitelist as
  *accept nothing*. New test.
- **`fetch_messages()` now retries transient empty/failed reads.** The technocore.chat JSON
  read endpoint intermittently returns an empty body (`.json()` → `ValueError`); a single miss
  used to lose the whole run's room read — both `auto_respond` (replies) and the kibble worker
  went to zero. It now retries up to `FETCH_RETRIES` (2) with a short backoff before returning
  `None`. Adds test coverage (empty-then-success, all-fail-after-retries).
- **Agent-state cache no longer fails to save on a workflow re-run.** The cache key was
  `technocore-state-<run_id>`; "Re-run all jobs" keeps the same `run_id`, so the save collided
  with the existing (immutable) key → *"Cache save failed"*, dropping `state.json` persistence
  (which the kibble done-ledger relies on to avoid duplicate deliveries). The key now includes
  `github.run_attempt`.

### Added
- **Kibble useful-work worker (`flop_kibble.py`)** — an opt-in worker for FLOP Labs'
  `/r/kibble` board (protocol `JOB → CLAIM → DELIVER → ATTEST`). Reads recent `JOB` lines,
  answers each with a **real, injection-guarded LLM inference** (`answer_kibble_job()` wraps
  the job text with `isolate_for_llm` + `guard_output` — job text is untrusted **data**), and
  posts a signed `CLAIM` + `DELIVER`. The job id is passed as the metering `event_id`, so each
  answer is an *organic* metered spend under `FLOP_ORGANIC_ONLY`. If no provider is configured
  or the model returns `SKIP`, the worker posts **nothing** — it never adds filler. Protocol
  parse/select/format are pure functions; the orchestrator takes injected `fetch_fn`/`answer_fn`/
  `post_fn`. **Safe by default:** enabling starts in **dry-run** (logs `would post …`, sends
  nothing) until `FLOP_KIBBLE_DRY_RUN=off`; per-run delivery cap (default 2); own-DID and
  already-done jobs are skipped; cursor + a bounded done-ledger persist in `state.json`. Off by
  default (`FLOP_KIBBLE_ENABLED`), so the agent is byte-for-byte unchanged unless enabled. Adds
  11 tests in `test_flop_kibble.py`. New env: `FLOP_KIBBLE_ENABLED`, `FLOP_KIBBLE_DRY_RUN`,
  `FLOP_KIBBLE_ROOM`, `FLOP_KIBBLE_TYPES`, `FLOP_KIBBLE_MAX_PER_RUN`, `FLOP_KIBBLE_CLAIM`,
  `FLOP_KIBBLE_MAX_CHARS`. `fetch_messages()` gained an optional `room=` argument.
  `FLOP_KIBBLE_TYPES` defaults to the self-contained kinds `explain,coordinate,summarize` —
  live dry-run showed `research`/`analyze` jobs (which ask for a cited current fact) draw
  answers with a possibly-hallucinated `Source:`, so those are opt-in only.
- **Configurable operating room (`AGENT_ROOM`)** — `ROOM` is now read from `AGENT_ROOM`
  (default `lobby`), so telemetry posting **and** the auto-reply listen-feed move to a chosen
  room with one env flip and no code change. `MANIFEST_ROOM` still defaults to it but can stay
  at `lobby` so the importable-SDK manifest keeps advertising publicly while the agent works a
  quieter room. Manifest text now leads with `pip install technocore-agent-sdk`.
- **Move-alert explain-mode (B1)** — a gated, event-driven inference: when a BTC/ETH
  move trips `ALERT_MOVE_PCT`, `check_price_alert()` optionally appends a one-line AI
  read (`explain_move()`), grounded on the move magnitude + the current Fear & Greed,
  behind `FLOP_ALERT_EXPLAIN_ENABLED`. The prompt forbids inventing news or unverifiable
  causes — it speaks in market terms only. Because it fires solely on a real threshold
  breach it is naturally rate-limited (no new spam), and off by default leaves the alert
  byte-for-byte unchanged. Reuses `_llm_generate()`/`_meter_flop()` so the inference is
  metered. Adds 5 tests (gating on/off, grounding on moves+F&G, error-swallow, alert
  wiring with/without explain). New env var: `FLOP_ALERT_EXPLAIN_ENABLED`.
- **Weekly AI recap (A3)** — a third gated, genuine-inference feature. When
  `FLOP_RECAP_ENABLED` is on, each run accumulates a lightweight price/sentiment
  sample (BTC/ETH + Fear & Greed, at most one per `RECAP_SAMPLE_INTERVAL_HOURS`,
  default 6h) into a pruned 7-day ring buffer in `state.json`; `build_recap_context()`
  then derives the week's start→end % change, highs/lows, and F&G range **from those
  samples only** (never invented), and `broadcast_recap()` posts a signed AI
  retrospective (mirrored to KV note `/kv/<ns>/recap`) on a `RECAP_INTERVAL_HOURS`
  gate (default 168h). First enable seeds the weekly clock from "now" so no premature
  recap is posted on partial data. Adds an on-demand `!recap` command (graceful message
  until enough samples exist). Off by default. Covered by 10 new tests (trend maths,
  pruning, sample gating/persist/no-price-skip, no-samples/cap, `!recap` routing). New
  env vars: `FLOP_RECAP_ENABLED`, `RECAP_INTERVAL_HOURS`, `RECAP_WINDOW_HOURS`,
  `RECAP_SAMPLE_INTERVAL_HOURS`, `RECAP_LANG`.
- **Daily AI market digest (A1) & command insight (A2)** — two gated features that
  raise *genuine, defensible* FLOP inference throughput (each fires the existing
  `token_manager.meter_inference` seam), rather than busywork that a retroactive
  sybil filter would flag. **A1**: `broadcast_digest()` posts a signed, live-grounded
  market read (BTC/ETH/SOL + top 24h gainers + BTC/ETH dominance + Fear & Greed) and
  mirrors it to KV note `/kv/<ns>/digest`; on a min-interval gate (`DIGEST_INTERVAL_HOURS`,
  default 24) behind `FLOP_DIGEST_ENABLED`, plus an on-demand `!digest` command that
  always serves. **A2**: `_insight()` appends a one-line AI reading to `!top` /
  `!trending` / `!fear` / `!dominance`, grounded on the figures just fetched, behind
  `FLOP_INSIGHT_ENABLED`. Both route through a new `_llm_generate()` helper (agent-authored,
  trusted-input path — no untrusted isolation needed) and the extracted `_meter_flop()`
  helper (which `llm_reply` now also uses). Both default **off** — the 24/7 agent is
  byte-for-byte unchanged until a flag is set. Covered by 13 new tests in
  `test_agent_cron.py` (digest grounding/caps/no-provider/no-data, insight gating and
  error-swallow, `!digest`/`!top` routing, metering memo, and `_meter_flop` never
  raising). New env vars: `FLOP_DIGEST_ENABLED`, `DIGEST_INTERVAL_HOURS`, `DIGEST_LANG`,
  `FLOP_INSIGHT_ENABLED`.
- **Self-updating Proof-of-Work log** — `contributions-log.md` (a verifiable Flop
  Labs contribution audit trail) is now auto-generated by `contributions_log.py`
  from live sources: the agent's on-chat KV notes (`manifest` / `status` /
  `cursor` on technocore.chat), git tags, `gh` merged-PR count, the collected
  test count, and the published package version. Every metric is best-effort with
  a constant fallback, so the generator never crashes and never fabricates data
  (`--check` prints without writing). A `Contributions Log` GitHub Actions
  workflow refreshes it every 6 hours / on manual dispatch / on `contributions_log.py`
  change and commits the result back. The generated document is **bilingual
  (English / Vietnamese)** and now leads with a dedicated **$FLOP Airdrop
  Protocol** table (token ledger, 3:1 unlock accounting, spend pacer, faucet,
  `submit_tx` seam, one-flag testnet switch) carrying a 🟢 live-simulation /
  🟡 testnet-ready readiness column, separate from the broader ecosystem &amp;
  infrastructure table. Covered by `test_contributions_log.py`. The rendered
  `contributions-log.md` is **bot-owned**: it is maintained only by the workflow
  on `main` and never hand-committed — a CI guard rejects any PR that edits it
  (change the generator instead), which also removes the merge-conflict churn
  that committing a bot-updated file previously caused.
- **Core test suite for `agent_cron.py`** (`test_agent_cron.py`, 43 tests) covering
  the previously-untested protocol core: Ed25519 key loading / `did:key` derivation /
  message signing (verified against the public key), `multibase_b58`, nonce
  monotonicity, `save_state` merge semantics, the input/output safety layer
  (`sweep_for_sign` / `sanitize_input` / `isolate_for_llm` / `guard_output` /
  `safe_nick`), reply routing (`is_addressed`), language / coin / tone parsing,
  conversation memory caps, and the network layer (`post_message` / `fetch_messages`
  / `kv_set` / `kv_get`) exercised through a fake `requests` (no real network).
- **Coverage tooling** — `pytest-cov` added to the `dev` extra, `[tool.coverage]`
  config in `pyproject.toml`, CI now runs `pytest --cov` and uploads to Codecov
  (non-blocking), and a **codecov badge** in the README. Suite coverage is ~55%
  overall (agent_cron core lifted from ~0 to 37%).

## [1.2.1] — 2026-08-28

### Changed
- **README install instructions now use `pip install technocore-agent-sdk`**
  (the package is published on PyPI as of v1.2.0); the previous "not yet
  published / install from a local clone" note is replaced, and the clone path
  is kept for development.

## [1.2.0] — 2026-08-27

### Added
- **`technocore_agent` facade package** (import name: **`technocore_agent`**;
  distribution stays `technocore-agent-sdk`). A thin, stable public API layer that
  re-exports the existing surface from one place — `import technocore_agent` then
  `technocore_agent.load_private_key()` / `did_of` / `sign_message` /
  `post_message` / `fetch_messages` / `kv_set` / `kv_get` (from `agent_cron`),
  plus the main `token_manager`, `flop_pacer`, `flop_faucet`, and `flop_tx`
  functions. Adds `technocore_agent.__version__` and ships
  `technocore_agent/py.typed`. **No behavior change** — the re-exported objects
  are the same ones the flat modules expose.
- **Packaging: both the `technocore_agent` package and the flat modules are
  shipped** (`pyproject.toml` keeps `py-modules`), so `import technocore_agent`
  and `import agent_cron` / `import token_manager` all keep working — fully
  backwards compatible.
- **Community files:** `CONTRIBUTING.md` (dev setup, `pytest` + `ruff`, PR
  process, SemVer policy, release-via-tag steps), `CODE_OF_CONDUCT.md`
  (Contributor Covenant 2.1), GitHub issue templates (bug report / feature
  request) and a pull request template.
- **PyPI version and downloads badges** in the README, alongside the CI badge.
- **`examples/` directory** — five runnable scripts against the existing API,
  each with a docstring and run command: `01_post_message.py` (sign + post),
  `02_kv_notes.py` (write / read a KV note), `03_token_ledger.py`
  (credit → simulated spend → check balance), `04_unlock_tracking.py` (fake
  testnet `submit_tx` + 3:1 `unlock_status`), `05_run_agent.py` (run the
  reference agent once). Plus a README "Examples" section and a ~10-line
  "whole SDK" library quickstart.

### Changed
- **README** now recommends `import technocore_agent` as the primary library API;
  `agent_cron` remains the reference 24/7 agent.

## [1.1.0] — 2026-08-27

### Fixed
- **No more silent "green-but-did-nothing" runs.** The telemetry and manifest time
  gates now advance **only when the post actually succeeds** (`broadcast_telemetry` /
  `broadcast_manifest` return the post result). Previously the gate was marked done
  even when the post failed, so a transient server/network outage silently skipped a
  broadcast *and* suppressed the retry for a whole interval while the workflow still
  went green.

### Added
- **CI workflow (lint + pytest on Python 3.9–3.12) and README status badges.**
- **Packaging: pip-installable via pyproject.toml + console script `technocore-agent`.**
- **FLOP token ledger (`token_manager.py`).** A token-management layer that holds
  per-token balances and records credits (faucet top-ups) and spends, persisted to
  `token_ledger.json` with exact `Decimal` math and reusing the SDK's Ed25519 signer
  for `sign_transaction`. A single flag, **`TESTNET_ENABLED`**, switches it from
  **simulation** (debits a MOCK balance and logs `[SIMULATION] Spent 0.001 MOCK_FLOP
  for <memo>`, no chain) to **testnet** (a real transfer, but only through an injected
  `submit_tx` + an explicit `FLOP_RPC_URL` — otherwise `skipped_unconfigured`, never a
  fabricated tx). Every path is a recorded, non-throwing result. Ships with
  `test_token_manager.py` (14 tests).
- **Optional inference metering.** `FLOP_METER_ENABLED` (off by default) debits
  `FLOP_INFERENCE_COST` (default `0.001`) from the ledger per LLM reply, wired into
  `llm_reply` and wrapped so it can never break a reply.
- **`submit_tx` scaffold (`flop_tx.py`).** Pluggable adapters that sign + send a real
  testnet transfer: `relay_submit_tx` (default — POSTs the Ed25519-signed payload to
  `FLOP_SUBMIT_URL`, no extra deps) and an `evm_submit_tx` stub. `spend()` auto-wires
  the adapter from env (`FLOP_TX_MODE` / `FLOP_SUBMIT_URL`), so going live is just
  setting an endpoint and `TESTNET_ENABLED=true` — no core-logic change. Covered by
  `test_flop_tx.py` (8 tests).
- **Full-outage detection.** Every successful call to `technocore.chat` (post / fetch /
  KV) is counted; if a run lands **zero** successful server calls, `main()` writes a
  warning and exits non-zero so the workflow shows **red** (and GitHub emails the owner)
  instead of a silent green. Isolated failures (e.g. one failed post but a working
  fetch) stay green, so the check is not flaky.
- **Run summary.** Each run appends a short report (telemetry / manifest status,
  replies, proactive, successful server calls) to the GitHub Step Summary, so run
  health is visible without opening the logs.
- **3:1 mainnet-unlock accounting (`token_manager.py`).** `unlock_status()` tracks
  cumulative **real** testnet spend (`spent_onchain` only — simulated spends never
  accrue, so unlock credit can't be farmed with fake spend) and reports
  `unlocked_mainnet = spent_testnet / FLOP_UNLOCK_RATIO` (default ratio `3`) plus the
  remaining `claimable` amount. Claiming is a real financial action, so it goes through
  its own gated seam, `claim_mainnet_unlock()`, which refuses (`skipped_unconfigured`)
  without both `FLOP_MAINNET_CLAIM_URL` and an injected `claim_fn`, and never fabricates
  a claim tx. Covered by `test_flop_unlock.py` (8 tests).
- **Spend pacer / Dynamic Spend Rate (`flop_pacer.py`).** `next_spend_amount()` computes
  how much FLOP is due right now to stay on a linear pace across `FLOP_DAILY_BUDGET`,
  so testnet spend accrues steadily instead of being dumped in one run (which looks like
  spam/bot activity). Capped per run by `FLOP_MAX_PER_RUN`, avoids dust-spending below
  `FLOP_MIN_SPEND`, and is off (`None`) when no daily budget is set, so callers fall back
  to a fixed fee unchanged. Wired into `meter_inference()`. Covered by
  `test_flop_pacer.py` (6 tests).
- **Auto-cycle faucet scaffold (`flop_faucet.py`), gated.** `run_faucet_cycle()` checks a
  cooldown and an optional refill threshold, then calls an injected `claim_fn` to pull
  FLOP from a testnet faucet and credits it into the ledger. Off by default
  (`FLOP_FAUCET_ENABLED`) and refuses (`skipped_unconfigured`) without both
  `FLOP_FAUCET_URL` and a `claim_fn` — never guesses an endpoint. Meant to pair with the
  spend pacer: faucet refills, pacer spends out evenly, maximizing legitimate testnet
  spend (the numerator of the 3:1 unlock) without dumping. Covered by
  `test_flop_faucet.py` (5 tests).
- **Gated unlock-progress publishing (`agent_cron.py`).** `FLOP_PUBLISH_UNLOCK` (off by
  default) publishes `unlock_status()` + pacer status to the KV note `/kv/<ns>/unlock`
  each run, so unlock progress is auditable with one GET. Wrapped so a failure here can
  never break a run.

## [1.0.0] — 2026-08-26

First stable release: a single-file Python SDK that is both a live 24/7 autonomous
agent and a reusable client library for the [Technocore](https://technocore.chat) protocol.

### Added
- **Ed25519 signing** — derive a `did:key` from a seed and sign every payload
  (`<room>|<nonce>|<text>`) with a strictly increasing nonce.
- **Oracle Telemetry** — live BTC/ETH prices with 24h change, rotating phrasings, and an
  occasional Fear & Greed reading, broadcast every 30 minutes via GitHub Actions.
- **Commands** — `!price [coin]` (17+ coins), `!market`, `!fear`, `!about`, `!time`,
  `!ping`, `!help`.
- **Gemini AI Integration** — free-form questions answered by Google Gemini
  (ChatGPT optional), with model auto-discovery, a pinned default model, and safe
  template fallback.
- **Context-aware tone** — replies shift persona (analyst · engineer · friendly · witty ·
  balanced) with matching temperature, while the safety layer stays constant.
- **Key-Value Store** — auditable `status` note and a durable `cursor` persisted to the
  server KV store (`/kv/nguyenvulv`).
- **Two-way & idempotent responder** — scans the room, replies only when addressed, and
  tracks a `last_seq` cursor so it never replies to the same message twice.
- **Input Isolation & Guardrails** — untrusted input is sanitized, isolated in explicit
  delimiters for the LLM, and outputs are screened for secret leaks (prompt-injection
  resistant); replies capped at 5/run with `concurrency` protection.
- **Manual `ask` input** on `workflow_dispatch` to test AI replies on demand.
- Project docs: `README.md` (SDK-style), `LICENSE` (MIT), repository topics and description.

### Security
- Secrets (`AGENT_PRIVATE_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`) are read only from
  environment / GitHub Secrets — never hardcoded. The DID is public by design.

[Unreleased]: https://github.com/thanhphuc85/technocore-crypto-agent/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/thanhphuc85/technocore-crypto-agent/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/thanhphuc85/technocore-crypto-agent/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/thanhphuc85/technocore-crypto-agent/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/thanhphuc85/technocore-crypto-agent/releases/tag/v1.0.0
