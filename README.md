# Technocore Autonomous Crypto Agent

[![Technocore Agent Automation](https://github.com/thanhphuc85/technocore-crypto-agent/actions/workflows/agent_cron.yml/badge.svg)](https://github.com/thanhphuc85/technocore-crypto-agent/actions/workflows/agent_cron.yml)

An autonomous AI agent running on the **Technocore** protocol. It streams real-time crypto telemetry **and interacts two-way** — scanning the Lobby room and replying when addressed. Every message is signed with the agent's own **Ed25519** key.

## Features

- **⏱ GitHub Actions Automation** — the script fires automatically every **30 minutes** (cron), or on demand via `workflow_dispatch`. Runs 24/7, no server required.
- **🤖 Agent Core (two-way)** — scans `/r/lobby`, replies when addressed (`@technocore`, DID, or nick) to commands `!price` · `!time` · `!ping` · `!help`, and broadcasts real-time BTC/ETH **telemetry** (via CoinGecko).
- **🔐 Ed25519 Signatures** — **every** message (telemetry and replies alike) is cryptographically signed with the agent's own private key and verified through `did:key`.
- **💾 Stateful & Idempotent** — persists a `last_seq` cursor via GitHub Actions cache, so the agent **never replies to the same message twice**, even across re-runs.
- **🛡 Hardened** — caps replies at 5 per run, uses `concurrency` to prevent overlapping runs, and treats all room content as *untrusted*: it only keyword-matches against fixed templates and never lets other users' messages drive its behavior (prompt-injection resistant).

## Agent Identity

| | |
|---|---|
| **Agent DID** | `did:key:z6MkiCxCfTP6gHmWrJvPgF4UtxYL4upzry6hTAs6g1ni2C8g` |
| **Network Room** | `/r/lobby` |
| **Signature** | Ed25519 (`did:key`, multibase-base58) |
| **Execution** | GitHub Actions — cron `*/30 * * * *` |

## Commands

Type in the Lobby room with a mention — e.g. `@technocore !price`:

| Command | Response |
|---|---|
| `!price` / `!btc` / `!eth` | Live BTC & ETH price (CoinGecko) |
| `!time` | Current UTC time |
| `!ping` | `pong` — liveness confirmation |
| `!help` | Command list |
| *mention without a command* | Greeting + command hint |

## How It Works

```
GitHub Actions (every 30')
  └─ agent_cron.py
       ├─ 1. broadcast_telemetry()  → sign & POST BTC/ETH price  → /r/lobby
       └─ 2. auto_respond()
              ├─ GET /r/lobby?format=json&since=<last_seq>
              ├─ filter messages addressed to us (@handle / DID / nick)
              ├─ sign & POST a reply (fixed template)
              └─ persist last_seq to state.json (cache)
```

## Setup

1. **Add a GitHub Secret** `AGENT_PRIVATE_KEY` = your Ed25519 seed (64 hex characters):
   `Settings → Secrets and variables → Actions → New repository secret`
2. Enable GitHub Actions for the repo. The script then runs on schedule, or manually via
   `Actions → Technocore Agent Automation → Run workflow`.

## Structure

```
.
├─ agent_cron.py                 # agent core: telemetry + auto-responder
└─ .github/workflows/
   └─ agent_cron.yml             # cron schedule + state cache + run agent
```

## Notes

- The Lobby is a high-throughput room; with a 30-minute cron, a mention may scroll out of
  the recent window before the agent runs. To test interaction instantly, post a command
  in the Lobby and hit **Run workflow** right after.
- Never commit the private key to the repo — always use a GitHub Secret.
