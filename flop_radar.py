# SPDX-License-Identifier: MIT
"""flop_radar — RADAR TESTNET: biết FLOP mở faucet/session trong vòng ~1 giờ, 24/7.

Vì sao cần: mỗi ngày testnet chậm là mất ~1/90 lượng faucet của danh tính này. Tác vụ canh
trong app chỉ chạy khi máy mở; agent thì chạy 5 phút/lần trên GitHub Actions. Radar gắn vào
agent: mỗi FLOP_TESTNET_RADAR_HOURS (mặc định 1) đọc 3 nguồn CÔNG KHAI, so với mốc đã biết,
có cái MỚI thì báo Telegram.

  · technocore.chat/openapi.json      — faucet nhiều khả năng mở TRÊN technocore.chat; route
                                         mới phải xuất hiện trong spec trước.
  · docs.flop.network/llms.txt        — trang tài liệu mới (faucet/API/SDK/onboarding).
  · docs.flop.network/docs/network-testnet — trạng thái mạng ("planned" -> khác).

CHỈ ĐỌC: không đăng tin, không chi, không ghi KV ngoài state của chính agent.
Thuần & test được: caller tiêm fetch_fn(url) -> str|None.
"""
from __future__ import annotations

import json
import re

KEY = "testnet_radar"
OPENAPI_URL = "https://technocore.chat/openapi.json"
LLMS_URL = "https://docs.flop.network/llms.txt"
STATUS_URL = "https://docs.flop.network/docs/network-testnet"

# Mốc đã biết (2026-10-22). Cái gì NGOÀI mốc này + ngoài những gì radar đã báo = mới.
BASE_PATHS = frozenset((
    "/", "/.well-known/agent-skills/index.json", "/.well-known/agent.json",
    "/.well-known/ai-catalog.json", "/.well-known/api-catalog",
    "/.well-known/mcp/server-card.json", "/.well-known/security.txt", "/auth.md", "/config",
    "/healthz", "/humans", "/interop.md", "/kv/{ns}", "/kv/{ns}/{key}",
    "/kv/{ns}/{key}/set-signed/{did}/{sig}/{nonce}/{value}", "/kv/{ns}/{key}/set/{value}",
    "/llms.txt", "/openapi.json", "/patterns.md", "/r/events", "/r/{room}", "/r/{room}/export",
    "/r/{room}/say-signed/{did}/{sig}/{nonce}/{text}", "/r/{room}/say/{nick}/{text}",
    "/robots.txt", "/rooms", "/sitemap.xml", "/skill.md"))
BASE_DOCS = frozenset((
    "agent-accounts-and-spend-control", "agent-integration", "airdrop-and-testnet-conversion",
    "atomic-swaps", "competitive-landscape", "core-terms", "demand-and-broker", "general-faq",
    "governance", "how-blocks-are-built", "inference-session-lifecycle",
    "miner-and-validator-economics", "miner-faq", "network-devnet", "network-mainnet",
    "network-testnet", "operator-config-reference", "proof-of-useful-inference",
    "protocol-parameters", "run-gpu-provider", "run-miner", "run-operator", "run-validator",
    "soft-and-hard-tiers", "staking-and-slashing", "team-and-company", "technology-and-chain",
    "testnet-and-timeline", "tokenomics-and-supply", "tokenomics-basics", "validator-faq",
    "verification-and-security", "verification-stack", "what-is-flop"))
BASE_STATUS = "planned"
HOT = re.compile(r"faucet|session|infer|mempool|wallet|claim|testnet|model|settle|escrow|agent", re.I)


def parse_paths(text):
    """Danh sách path từ openapi.json (bỏ phần chữ cảnh báo đứng trước '{'). None nếu hỏng."""
    if not text or "{" not in text:
        return None
    try:
        d = json.loads(text[text.index("{"):])
    except ValueError:
        return None
    paths = d.get("paths") if isinstance(d, dict) else None
    return set(paths) if isinstance(paths, dict) else None


def parse_docs(text):
    if not text:
        return None
    found = set(re.findall(r"docs\.flop\.network/docs/([a-z0-9-]+)", text))
    return found or None


def parse_status(text):
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    m = re.search(r"Status:\s*([A-Za-z][A-Za-z -]{0,40}?)[.<]", text)
    return m.group(1).strip().lower() if m else None


def check(state: dict, fetch_fn) -> str:
    """Đọc 3 nguồn, so với mốc + những gì đã báo. Trả chuỗi báo động (đã ghi nhận vào state
    để không báo lại) hoặc None. Nguồn nào lỗi thì bỏ qua nguồn đó, không báo động giả."""
    r = state.get(KEY)
    r = dict(r) if isinstance(r, dict) else {}
    seen_paths = set(r.get("paths") or [])
    seen_docs = set(r.get("docs") or [])
    status_known = r.get("status") or BASE_STATUS
    lines = []

    paths = parse_paths(fetch_fn(OPENAPI_URL))
    if paths is not None:
        new = sorted(paths - BASE_PATHS - seen_paths)
        if new:
            hot = [p for p in new if HOT.search(p)]
            lines.append("• technocore.chat có ROUTE MỚI: " + ", ".join(new[:12])
                         + (f"  ← đáng chú ý: {', '.join(hot[:6])}" if hot else ""))
            seen_paths |= set(new)

    docs = parse_docs(fetch_fn(LLMS_URL))
    if docs is not None:
        new = sorted(docs - BASE_DOCS - seen_docs)
        if new:
            lines.append("• docs.flop.network có TRANG MỚI: "
                         + ", ".join(f"/docs/{d}" for d in new[:12]))
            seen_docs |= set(new)

    status = parse_status(fetch_fn(STATUS_URL))
    if status and status != status_known:
        lines.append(f"• Trạng thái testnet đổi: '{status_known}' → '{status}'")
        status_known = status

    state[KEY] = {"paths": sorted(seen_paths)[:60], "docs": sorted(seen_docs)[:60],
                  "status": status_known}
    if not lines:
        return None
    return ("🚨 FLOP TESTNET RADAR — có thay đổi:\n" + "\n".join(lines)
            + "\n→ Mở checklist ngày 1 trong STRATEGY.md: đọc spec mới, chỉnh "
              "_faucet_claim_fn/_mempool_submit_fn, dry-run rồi mới chi thật.")
