# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_Nothing yet. Add changes here as they land, then cut a new version._

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
