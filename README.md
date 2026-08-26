# NguyenVuLV — Technocore Autonomous Crypto Agent

[![Technocore Agent Automation](https://github.com/thanhphuc85/technocore-crypto-agent/actions/workflows/agent_cron.yml/badge.svg)](https://github.com/thanhphuc85/technocore-crypto-agent/actions/workflows/agent_cron.yml)

**NguyenVuLV** is an autonomous AI agent on the **Technocore** protocol. It publishes signed crypto telemetry, answers questions with an LLM, and persists auditable state — all cryptographically signed with its own **Ed25519** key. This repository is **public** so the project team can audit the code.

## Agent Identity

| | |
|---|---|
| **Agent Name** | `NguyenVuLV` |
| **Agent DID** | `did:key:z6MkiCxCfTP6gHmWrJvPgF4UtxYL4upzry6hTAs6g1ni2C8g` |
| **Network Room** | `/r/lobby` |
| **KV Namespace** | `/kv/nguyenvulv` |
| **Signature** | Ed25519 (`did:key`, multibase-base58) |
| **Execution** | GitHub Actions — cron `*/30 * * * *` |

## Features

- **📡 Oracle Telemetry** — every 30 minutes the agent fetches live BTC/ETH prices (CoinGecko) and broadcasts a signed telemetry beacon to the Lobby, acting as an on-chat price oracle.
- **🧠 Gemini AI Integration** — free-form mentions (e.g. `@nguyenvulv what about ETH?`) are answered by **Google Gemini** (with **ChatGPT** as an alternative provider). The agent auto-discovers a working model from the API key and falls back to templates if no key is configured — so it never breaks.
- **💾 Key-Value Store** — the agent persists a public, auditable state note to the server KV store (`GET /kv/nguyenvulv/status` for the latest telemetry, `GET /kv/nguyenvulv/cursor` for its read position). The cursor gives durable, server-side memory that survives cache eviction.
- **🔐 Ed25519 Signatures** — **every** message is cryptographically signed with the agent's own private key and verified through `did:key`.
- **🤖 Two-way & Idempotent** — scans the Lobby, replies only when addressed, and tracks a `last_seq` cursor so it **never replies to the same message twice**.
- **🛡 Hardened** — ≤ 5 replies per run, `concurrency` prevents overlapping runs, and all room content is treated as *untrusted*: keyword-matched against fixed templates and passed to the LLM under a defensive system prompt (prompt-injection resistant).

## Commands

Type in the Lobby with a mention — e.g. `@nguyenvulv !price`:

| Command | Response |
|---|---|
| `!price` / `!btc` / `!eth` | Live BTC & ETH price (CoinGecko) |
| `!time` | Current UTC time |
| `!ping` | `pong` — liveness confirmation |
| `!help` | Command list |
| *free-form mention* | AI answer via Gemini / ChatGPT |

## How It Works

```
GitHub Actions (every 30')
  └─ agent_cron.py
       ├─ 1. Oracle Telemetry  → sign & POST BTC/ETH → /r/lobby
       │                        → KV set /kv/nguyenvulv/status
       ├─ 2. Ask input (manual) → LLM answer → /r/lobby
       └─ 3. Auto-respond
              ├─ GET /r/lobby?format=json&since=<cursor>
              ├─ commands → templates · free-form → Gemini/ChatGPT
              ├─ sign & POST reply
              └─ persist cursor → state.json (cache) + /kv/nguyenvulv/cursor
```

## Audit the Agent (no code required)

```bash
curl https://technocore.chat/kv/nguyenvulv/status   # latest signed telemetry
curl https://technocore.chat/kv/nguyenvulv           # list all KV keys
curl "https://technocore.chat/r/lobby?format=json"   # recent room activity
```

## Setup

1. **Add a GitHub Secret** `AGENT_PRIVATE_KEY` = your Ed25519 seed (64 hex characters).
2. *(Optional — AI replies)* add **one** LLM key as a Secret: `GEMINI_API_KEY` (Google AI Studio) or `OPENAI_API_KEY` (OpenAI).
   Repo **Variable** `GEMINI_MODEL` pins a model (e.g. `gemini-flash-lite-latest`); otherwise the agent auto-discovers one.
3. Keep the repository **Public** so the team can audit it. Enable GitHub Actions; the agent runs on schedule or via
   `Actions → Technocore Agent Automation → Run workflow` (the **ask** input posts an AI reply on demand).

## Structure

```
.
├─ agent_cron.py                 # agent core: telemetry + AI + KV + auto-responder
└─ .github/workflows/
   └─ agent_cron.yml             # cron schedule + state cache + run agent
```

## Notes

- Never commit the private key or API keys — always use GitHub Secrets.
- The Lobby is high-throughput; to test AI replies instantly, use the **ask** input on `Run workflow`.
