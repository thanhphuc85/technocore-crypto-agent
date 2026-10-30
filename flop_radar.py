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
  · api.flop.network/health           — gateway CHÍNH THỨC báo network ("devnet" -> khác).
  · flop.finance/official/            — mục "Current testnet and airdrop status" (FLOP Labs
                                         sửa trang này mỗi lần công bố về testnet/airdrop).

ĐỨNG TÊN AI (2026-10-28): kênh mua qua gateway do tài khoản BROKER của gateway mở -> explorer
ghi chi tiêu cho broker, ví mình = 0; yellowpaper R8.4 chấm theo người mở kênh. Chưa có câu
trả lời chính thức, nên radar canh 3 tín hiệu sẽ đổi quyết định:
  · yellowpaper#172                   — người của flop-labs (OWNER/MEMBER/COLLABORATOR/CONTRIBUTOR) trả lời,
                                         hoặc issue đóng.
  · <explorer>/api/session-offers     — miner có offer công khai -> mua TRỰC TIẾP đứng tên mình
                                         (CLI `flop session run`) bắt đầu khả thi.
  · <explorer>/api/accounts/<ví>/inference-spend — ví mình có chi tiêu ghi trên chain (0 -> >0).
  · yellowpaper#31                    — airdrop tính theo ví chủ / khoá uỷ quyền / danh tính agent
                                         (quyết định có cần lập ví agent có trần hay không).

DEVNET CÔNG KHAI (Arthur Hayes 26/10: "open up Devnet … before we graduate to Testnet"):
  · docs.flop.network/docs/network-devnet — trạng thái devnet ("development" -> khác).
  · api.flop.network/health genesis      — genesis đổi = mạng khởi động lại -> hạn mức gateway
                                         có thể được cấp mới (ví DID đã dùng hết hạn mức devnet cũ).

CHỈ ĐỌC: không đăng tin, không chi, không ghi KV ngoài state của chính agent.
Thuần & test được: caller tiêm fetch_fn(url) -> str|None.
"""
from __future__ import annotations

import hashlib
import json
import re

KEY = "testnet_radar"
OPENAPI_URL = "https://technocore.chat/openapi.json"
LLMS_URL = "https://docs.flop.network/llms.txt"
STATUS_URL = "https://docs.flop.network/docs/network-testnet"
GATEWAY_HEALTH_URL = "https://api.flop.network/health"
OFFICIAL_URL = "https://flop.finance/official/"
ATTRIB_ISSUE_URL = "https://api.github.com/repos/flop-labs/yellowpaper/issues/172"
CREDIT_ISSUE_URL = "https://api.github.com/repos/flop-labs/yellowpaper/issues/208"
# flop-labs/network = kênh báo lỗi chính thức của mạng đang chạy (mở 2026-10-30): issue/comment MỚI
# NHẤT của người flop-labs. Chỉ đọc cửa sổ gần nhất -> mốc "đã thấy" không cần giữ lâu hơn cửa sổ.
NETWORK_ISSUES_URL = ("https://api.github.com/repos/flop-labs/network/issues"
                      "?state=all&sort=created&direction=desc&per_page=20")
NETWORK_COMMENTS_URL = ("https://api.github.com/repos/flop-labs/network/issues/comments"
                        "?sort=created&direction=desc&per_page=20")
NETWORK_SEEN_KEEP = 40
NETWORK_ALERT_LINES = 5
UNIT_ISSUE_URL = "https://api.github.com/repos/flop-labs/yellowpaper/issues/31"
DEVNET_STATUS_URL = "https://docs.flop.network/docs/network-devnet"
BASE_DEVNET_STATUS = "development"
EXPLORERS = ("https://explorer.devnet.flop.network", "https://explorer.testnet.flop.network")
# CONTRIBUTOR = đã có commit trong repo. Yellowpaper chỉ do người của FLOP commit (2026-10-28: duy
# nhất `sv`, người đồng bộ bản spec — GitHub hiện là CONTRIBUTOR, không phải MEMBER).
MAINTAINER_ROLES = frozenset(("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"))

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
BASE_NETWORK = "devnet"
HOT = re.compile(r"faucet|session|infer|mempool|wallet|claim|testnet|model|settle|escrow|agent", re.I)


SEEN_KEEP = 60                       # trần mục "đã thấy" lưu trong state (state đi lên KV)


def _set_fp(items) -> str:
    """Dấu vân tay 12 hex của một tập chuỗi (không phụ thuộc thứ tự)."""
    return hashlib.sha256("\n".join(sorted(items)).encode("utf-8")).hexdigest()[:12]


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


def parse_genesis(text):
    """network.observed_genesis_hash từ /health của gateway; None nếu hỏng."""
    try:
        d = json.loads(text) if text else None
    except ValueError:
        return None
    net = d.get("network") if isinstance(d, dict) else None
    g = net.get("observed_genesis_hash") if isinstance(net, dict) else None
    return g.lower()[:80] if isinstance(g, str) and re.fullmatch(r"0x[0-9a-fA-F]{8,}", g) else None


def parse_network(text):
    """network.network từ /health của gateway ("devnet"/"testnet"/...); None nếu hỏng."""
    try:
        d = json.loads(text) if text else None
    except ValueError:
        return None
    net = d.get("network") if isinstance(d, dict) else None
    if isinstance(net, dict):
        net = net.get("network")
    return net.strip().lower()[:40] if isinstance(net, str) and net.strip() else None


def parse_official(text):
    """Dấu vân tay (sha256 12 ký tự) của mục trạng thái testnet/airdrop trên trang chính thức.
    Bỏ thẻ HTML và ngày "updated" để chỉ đổi khi NỘI DUNG đổi. None nếu không thấy mục."""
    if not text:
        return None
    t = re.sub(r"<[^>]+>", " ", text)
    m = re.search(r"(?is)Current testnet and airdrop status(.{0,4000}?)(?:Owner:|$)", t)
    if not m:
        return None
    body = re.sub(r"(?i)updated\s+\d{4}-\d{2}-\d{2}", "", m.group(1))
    body = re.sub(r"\s+", " ", body).strip().lower()
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12] if body else None


def _json(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def parse_issue(text):
    """(state, số comment) của issue GitHub; None nếu hỏng."""
    d = _json(text)
    if not isinstance(d, dict) or not isinstance(d.get("state"), str):
        return None
    n = d.get("comments")
    return d["state"], (n if isinstance(n, int) and n >= 0 else 0)


def parse_maintainer_comments(text):
    """[(id, login, url)] các comment do người của repo viết; None nếu hỏng."""
    d = _json(text)
    if not isinstance(d, list):
        return None
    out = []
    for c in d:
        if (isinstance(c, dict) and c.get("author_association") in MAINTAINER_ROLES
                and isinstance(c.get("id"), int)):
            out.append((c["id"], str((c.get("user") or {}).get("login") or "?")[:40],
                        str(c.get("html_url") or "")[:200]))
    return out


def parse_total(text):
    """pagination.total của một danh sách explorer; None nếu hỏng."""
    d = _json(text)
    p = d.get("pagination") if isinstance(d, dict) else None
    t = p.get("total") if isinstance(p, dict) else None
    return t if isinstance(t, int) and t >= 0 else None


def parse_spent(text):
    """totalSpentRaw (int, đơn vị raw 1e18) của một ví trên explorer; None nếu hỏng."""
    d = _json(text)
    v = d.get("totalSpentRaw") if isinstance(d, dict) else None
    try:
        return int(v) if isinstance(v, (str, int)) else None
    except ValueError:
        return None


def _host(url):
    return url.split("//", 1)[-1].split("/", 1)[0]


def _watch_issue(r, fetch_fn, url, num, topic, state_key, seen_key) -> list:
    """Issue yellowpaper: báo khi người của flop-labs comment hoặc issue đóng (mỗi việc 1 lần)."""
    lines = []
    issue = parse_issue(fetch_fn(url))
    if not issue:
        return lines
    st, n = issue
    first = state_key not in r                         # lần đầu: comment cũ chỉ ghi mốc, không báo
    if r.get(state_key) == "open" and st != "open":
        lines.append(f"• yellowpaper#{num} ({topic}) đã ĐÓNG — đọc kết luận: "
                     f"github.com/flop-labs/yellowpaper/issues/{num}")
    r[state_key] = st
    seen = set(r.get(seen_key) or [])
    page = max(1, -(-n // 100))                        # trang cuối = comment mới nhất
    found = parse_maintainer_comments(fetch_fn(f"{url}/comments?per_page=100&page={page}")) if n else []
    for cid, login, curl in found or []:
        if cid not in seen:
            if not first:
                lines.append(f"• flop-labs ({login}) TRẢ LỜI yellowpaper#{num} ({topic}) — đọc ngay: {curl}")
            seen.add(cid)
    r[seen_key] = sorted(x for x in seen if isinstance(x, int))[-20:]
    return lines


def _watch_network(r, fetch_fn) -> list:
    """Issue/comment mới của người flop-labs trên flop-labs/network (mỗi cái báo 1 lần). Lần đầu
    chỉ ghi mốc. Một nguồn lỗi -> giữ mốc cũ của nó, không báo nhầm cái đã thấy."""
    found = [parse_maintainer_comments(fetch_fn(u)) for u in (NETWORK_ISSUES_URL, NETWORK_COMMENTS_URL)]
    if all(f is None for f in found):
        return []
    first = "attr_net_seen" not in r
    seen = set(r.get("attr_net_seen") or [])
    window = [item for f in found for item in (f or [])]
    new = [item for item in window if item[0] not in seen]
    lines = [] if first else [f"• flop-labs ({login}) viết trên flop-labs/network: {url}"
                              for _, login, url in new[:NETWORK_ALERT_LINES]]
    ids = {item[0] for item in window}
    r["attr_net_seen"] = sorted(ids if None not in found else seen | ids)[-NETWORK_SEEN_KEEP:]
    return lines


def check_attribution(r: dict, fetch_fn, wallet=None, explorers=EXPLORERS) -> list:
    """3 tín hiệu ĐỨNG TÊN AI (xem docstring module). Ghi mốc vào r, trả các dòng báo động.
    Lần đầu thấy một nguồn chỉ ghi mốc (không báo). Nguồn lỗi -> bỏ qua."""
    lines = []
    lines += _watch_issue(r, fetch_fn, ATTRIB_ISSUE_URL, "172", "chi tiêu qua gateway đứng tên ai",
                          "attr_issue_state", "attr_seen")
    lines += _watch_issue(r, fetch_fn, UNIT_ISSUE_URL, "31",
                          "airdrop tính theo ví chủ hay khoá uỷ quyền", "attr_i31_state", "attr_i31_seen")
    lines += _watch_issue(r, fetch_fn, CREDIT_ISSUE_URL, "208", "cơ sở tính điểm chi tiêu của agent",
                          "attr_i208_state", "attr_i208_seen")
    lines += _watch_network(r, fetch_fn)
    offers = dict(r.get("attr_offers") or {})
    spent = dict(r.get("attr_spent") or {})
    spent_raw = dict(r.get("attr_spent_raw") or {})      # số raw (1e18) — so với sổ cái
    for base in explorers:
        h = _host(base)
        t = parse_total(fetch_fn(f"{base}/api/session-offers"))
        if t is not None:
            if offers.get(h) == 0 and t > 0:
                lines.append(f"• {h}: miner bắt đầu có OFFER công khai ({t}) — mua trực tiếp đứng "
                             "tên ví mình (flop session run) có thể khả thi")
            offers[h] = t
        if wallet:
            v = parse_spent(fetch_fn(f"{base}/api/accounts/{wallet}/inference-spend"))
            if v is not None:
                if spent.get(h) == 0 and v > 0:
                    lines.append(f"• {h}: VÍ MÌNH {wallet[:6]}…{wallet[-4:]} đã có chi tiêu ghi trên "
                                 f"chain ({v / 1e18:.4g} FLOP) — chi tiêu đang đứng tên mình")
                spent[h] = 0 if v == 0 else 1
                spent_raw[h] = v
    r["attr_offers"], r["attr_spent"], r["attr_spent_raw"] = offers, spent, spent_raw
    return lines


def check(state: dict, fetch_fn, wallet=None, explorers=EXPLORERS) -> str:
    """Đọc 3 nguồn, so với mốc + những gì đã báo. Trả chuỗi báo động (đã ghi nhận vào state
    để không báo lại) hoặc None. Nguồn nào lỗi thì bỏ qua nguồn đó, không báo động giả."""
    r = state.get(KEY)
    r = dict(r) if isinstance(r, dict) else {}
    seen_paths = set(r.get("paths") or [])
    seen_docs = set(r.get("docs") or [])
    status_known = r.get("status") or BASE_STATUS
    network_known = r.get("network") or BASE_NETWORK
    devnet_known = r.get("devnet_status") or BASE_DEVNET_STATUS
    genesis_known = r.get("genesis")
    official_known = r.get("official")
    lines = []
    testnet_open = False                 # CHỈ tín hiệu mạng testnet thật mới kèm checklist ngày 1

    # Danh sách "đã thấy" bị cắt còn SEEN_KEEP mục (state đi lên KV, trần 8192 ký tự), nên mục bị
    # cắt sẽ lại thành "mới" mỗi giờ. Chốt bằng DẤU VÂN TAY của cả tập: tập không đổi -> không so.
    paths_fp, docs_fp = r.get("paths_fp"), r.get("docs_fp")
    paths = parse_paths(fetch_fn(OPENAPI_URL))
    if paths is not None:
        fp = _set_fp(paths)
        if fp != paths_fp:
            new = sorted(paths - BASE_PATHS - seen_paths)
            if new:
                hot = [p for p in new if HOT.search(p)]
                lines.append("• technocore.chat có ROUTE MỚI: " + ", ".join(new[:12])
                             + (f"  ← đáng chú ý: {', '.join(hot[:6])}" if hot else ""))
            seen_paths = (seen_paths | set(new)) & paths    # route đã gỡ thì khỏi nhớ
            paths_fp = fp

    docs = parse_docs(fetch_fn(LLMS_URL))
    if docs is not None:
        fp = _set_fp(docs)
        if fp != docs_fp:
            new = sorted(docs - BASE_DOCS - seen_docs)
            if new:
                lines.append("• docs.flop.network có TRANG MỚI: "
                             + ", ".join(f"/docs/{d}" for d in new[:12]))
            seen_docs = (seen_docs | set(new)) & docs
            docs_fp = fp

    status = parse_status(fetch_fn(STATUS_URL))
    if status and status != status_known:
        lines.append(f"• Trạng thái testnet đổi: '{status_known}' → '{status}'")
        testnet_open = "planned" not in status.lower()
        status_known = status

    health = fetch_fn(GATEWAY_HEALTH_URL)
    network = parse_network(health)
    if network and network != network_known:
        lines.append(f"• api.flop.network báo mạng đổi: '{network_known}' → '{network}'")
        testnet_open = testnet_open or "test" in network.lower()
        network_known = network
    genesis = parse_genesis(health)
    if genesis and genesis_known and genesis != genesis_known:
        lines.append(f"• api.flop.network đổi GENESIS ({genesis_known[:10]}… → {genesis[:10]}…): mạng "
                     f"'{network or network_known}' khởi động lại — hạn mức gateway có thể được cấp mới. "
                     "Muốn chạy tập (xem bản tin) thì đặt FLOP_SESSION_BACKEND=gateway, xong xoá.")
    if genesis:
        genesis_known = genesis            # lần đầu chỉ ghi mốc

    devnet = parse_status(fetch_fn(DEVNET_STATUS_URL))
    if devnet and devnet != devnet_known:
        lines.append(f"• Trạng thái DEVNET đổi: '{devnet_known}' → '{devnet}' (devnet công khai KHÔNG "
                     "tính airdrop — chỉ để chạy thử; xem docs.flop.network/docs/network-devnet)")
        devnet_known = devnet

    official = parse_official(fetch_fn(OFFICIAL_URL))
    if official and official_known and official != official_known:
        lines.append("• flop.finance/official/ SỬA mục trạng thái testnet/airdrop — đọc ngay")
    if official:
        official_known = official          # lần đầu chỉ ghi mốc, không báo

    attr = {k: v for k, v in r.items() if k.startswith("attr_")}
    lines += check_attribution(attr, fetch_fn, wallet, explorers)

    state[KEY] = {"paths": sorted(seen_paths)[:SEEN_KEEP], "docs": sorted(seen_docs)[:SEEN_KEEP],
                  "paths_fp": paths_fp, "docs_fp": docs_fp,
                  "status": status_known, "network": network_known, "official": official_known,
                  "devnet_status": devnet_known, "genesis": genesis_known, **attr}
    if not lines:
        return None
    alert = "🚨 FLOP TESTNET RADAR — có thay đổi:\n" + "\n".join(lines)
    if not testnet_open:
        return alert
    return (alert
            + "\n→ Checklist ngày 1 (STRATEGY.md): đặt TESTNET_ENABLED=true (agent tự mua compute qua "
              "gateway bằng ví DID); FLOP_FAUCET_ENABLED=on + FLOP_FAUCET_URL + FLOP_FAUCET_TIER "
              "(manual/linked/quick); testnet có gateway riêng thì đặt FLOP_GATEWAY_URL. Xem dòng "
              "[session]/[gateway] ở run đầu tiên.")
