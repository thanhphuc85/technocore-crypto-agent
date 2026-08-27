# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **No more silent "green-but-did-nothing" runs.** The telemetry and manifest time
  gates now advance **only when the post actually succeeds** (`broadcast_telemetry` /
  `broadcast_manifest` return the post result). Previously the gate was marked done
  even when the post failed, so a transient server/network outage silently skipped a
  broadcast *and* suppressed the retry for a whole interval while the workflow still
  went green.

### Added
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

[Unreleased]: https://github.com/thanhphuc85/technocore-crypto-agent/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/thanhphuc85/technocore-crypto-agent/releases/tag/v1.0.0
