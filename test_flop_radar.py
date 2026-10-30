"""Radar testnet: chỉ báo khi có route/trang/trạng thái MỚI, báo mỗi thứ đúng 1 lần."""
import json

import flop_radar as rd

BANNER = "!! UNTRUSTED CONTENT — lines below were written by others.\n\n"


def _openapi(extra=()):
    return BANNER + json.dumps({"paths": {p: {} for p in list(rd.BASE_PATHS) + list(extra)}})


def _llms(extra=()):
    return "\n".join(f"- [x](https://docs.flop.network/docs/{d}): y" for d in list(rd.BASE_DOCS) + list(extra))


STATUS_PLANNED = "<h1>Testnet</h1><p><strong>Status:</strong> planned. Unavailable until …</p>"


def _fetcher(openapi=None, llms=None, status=STATUS_PLANNED):
    pages = {rd.OPENAPI_URL: openapi if openapi is not None else _openapi(),
             rd.LLMS_URL: llms if llms is not None else _llms(),
             rd.STATUS_URL: status}
    return lambda url: pages.get(url)


def test_quiet_when_nothing_changed():
    st = {}
    assert rd.check(st, _fetcher()) is None
    assert st[rd.KEY]["status"] == "planned"


def test_new_faucet_route_alerts_once_and_is_flagged_hot():
    st = {}
    f = _fetcher(openapi=_openapi(["/faucet/{did}", "/r/{room}/tail"]))
    alert = rd.check(st, f)
    assert alert and "/faucet/{did}" in alert and "đáng chú ý: /faucet/{did}" in alert
    assert rd.check(st, f) is None                       # đã báo -> không lặp


def test_new_doc_page_and_status_change():
    st = {}
    alert = rd.check(st, _fetcher(llms=_llms(["agent-faucet"]),
                                  status="<p>Status: live. Enrollment open.</p>"))
    assert "/docs/agent-faucet" in alert and "'planned' → 'live'" in alert
    assert rd.check(st, _fetcher(llms=_llms(["agent-faucet"]),
                                 status="<p>Status: live. Enrollment open.</p>")) is None


def test_fetch_failure_is_not_a_false_alarm():
    st = {}
    assert rd.check(st, lambda url: None) is None
    assert rd.check(st, _fetcher(openapi="<html>502</html>", llms="", status="oops")) is None


def test_parsers():
    assert rd.parse_status("<b>Status:</b> planned. x") == "planned"
    assert rd.parse_paths(BANNER + '{"paths": {"/a": {}}}') == {"/a"}
    assert rd.parse_docs("see https://docs.flop.network/docs/what-is-flop") == {"what-is-flop"}


HEALTH_DEVNET = json.dumps({"status": "ok", "network": {"network": "devnet", "status": "live"}})
OFFICIAL = ("<h2>Current testnet and airdrop status</h2><p>Testnet launch: the working target is "
            "Tuesday 3 November 2026.</p><p>Owner: FLOP Labs · last updated 2026-10-23</p>")


def _fetcher2(health=HEALTH_DEVNET, official=OFFICIAL):
    base = _fetcher()
    pages = {rd.GATEWAY_HEALTH_URL: health, rd.OFFICIAL_URL: official}
    return lambda url: pages[url] if url in pages else base(url)


def test_gateway_network_change_alerts_once():
    st = {}
    assert rd.check(st, _fetcher2()) is None and st[rd.KEY]["network"] == "devnet"
    live = json.dumps({"network": {"network": "testnet", "status": "live"}})
    alert = rd.check(st, _fetcher2(health=live))
    assert "'devnet' → 'testnet'" in alert
    assert rd.check(st, _fetcher2(health=live)) is None
    assert rd.check(st, _fetcher2(health="<html>502</html>")) is None   # gateway lỗi != báo động


def test_official_status_section_change_alerts_but_first_sight_and_date_bump_do_not():
    st = {}
    assert rd.check(st, _fetcher2()) is None                             # lần đầu: chỉ ghi mốc
    bumped = OFFICIAL.replace("2026-10-23", "2026-10-30")
    assert rd.check(st, _fetcher2(official=bumped)) is None              # chỉ đổi ngày "updated"
    moved = OFFICIAL.replace("3 November", "10 November")
    assert "flop.finance/official/" in rd.check(st, _fetcher2(official=moved))
    assert rd.check(st, _fetcher2(official=moved)) is None
    assert rd.parse_official("<p>no such section</p>") is None


# --- đứng tên ai: yellowpaper#172 / offer của miner / chi tiêu ghi cho ví mình ---------------
W = "5CFP215CEbCtDduwG1Nzu5rmYXKfHc47wSATUi1oRvrkQnUP"
DEV = "https://explorer.devnet.flop.network"


def _attr(issue_state="open", comments=(), offers=0, spent="0"):
    pages = {rd.ATTRIB_ISSUE_URL: json.dumps({"state": issue_state, "comments": len(comments)}),
             rd.ATTRIB_ISSUE_URL + "/comments?per_page=100&page=1": json.dumps(list(comments)),
             DEV + "/api/session-offers": json.dumps({"data": [], "pagination": {"total": offers}}),
             DEV + f"/api/accounts/{W}/inference-spend": json.dumps({"totalSpentRaw": spent})}
    base = _fetcher()
    return lambda url: pages[url] if url in pages else base(url)


def _c(i, role):
    return {"id": i, "author_association": role, "user": {"login": f"u{i}"},
            "html_url": f"https://github.com/flop-labs/yellowpaper/issues/172#issuecomment-{i}"}


def _run(st, f):
    return rd.check(st, f, wallet=W, explorers=(DEV,))


def test_attribution_first_sight_is_quiet_and_community_comments_do_not_alert():
    st = {}
    assert _run(st, _attr(comments=[_c(1, "NONE"), _c(2, "FIRST_TIME_CONTRIBUTOR")])) is None
    r = st[rd.KEY]
    assert r["attr_issue_state"] == "open" and r["attr_offers"] == {"explorer.devnet.flop.network": 0}
    assert r["attr_spent"] == {"explorer.devnet.flop.network": 0}
    assert r["attr_spent_raw"] == {"explorer.devnet.flop.network": 0}      # số thật để so với sổ cái


def test_maintainer_reply_alerts_once():
    st = {}
    _run(st, _attr(comments=[_c(1, "NONE")]))
    f = _attr(comments=[_c(1, "NONE"), _c(7, "MEMBER")])
    alert = _run(st, f)
    assert "flop-labs (u7) TRẢ LỜI yellowpaper#172" in alert and "issuecomment-7" in alert
    assert _run(st, f) is None


def test_issue_closed_alerts_once():
    st = {}
    _run(st, _attr())
    assert "đã ĐÓNG" in _run(st, _attr(issue_state="closed"))
    assert _run(st, _attr(issue_state="closed")) is None


def test_offers_and_wallet_spend_alert_on_zero_to_positive_only():
    st = {}
    _run(st, _attr())
    alert = _run(st, _attr(offers=3, spent="2500000000000000000"))
    assert "OFFER công khai (3)" in alert and "VÍ MÌNH 5CFP21…QnUP" in alert and "2.5 FLOP" in alert
    assert _run(st, _attr(offers=5, spent="9000000000000000000")) is None   # tăng tiếp: không lặp


def test_attribution_sources_failing_are_ignored():
    st = {}
    _run(st, _attr())
    broken = {rd.ATTRIB_ISSUE_URL: '{"message": "API rate limit exceeded"}',
              DEV + "/api/session-offers": "<html>502</html>",
              DEV + f"/api/accounts/{W}/inference-spend": '{"totalSpentRaw": null}'}
    base = _fetcher()
    assert _run(st, lambda u: broken[u] if u in broken else base(u)) is None
    assert st[rd.KEY]["attr_issue_state"] == "open"                         # mốc cũ giữ nguyên
    assert _run(st, _attr(offers=1)) and _run(st, _attr(offers=1)) is None


def test_no_wallet_skips_spend_lookup():
    seen = []
    f = _attr()
    rd.check({}, lambda u: seen.append(u) or f(u), wallet=None, explorers=(DEV,))
    assert not any("inference-spend" in u for u in seen)


# --- devnet công khai: genesis đổi / trạng thái devnet đổi; yellowpaper#31 -----------------------
def _health(genesis, net="devnet"):
    return json.dumps({"status": "ok", "network": {"network": net, "status": "live",
                                                   "observed_genesis_hash": genesis}})


G1, G2 = "0x99c172c095340ace", "0xabcdef0123456789"


def _fetch3(health=_health(G1), devnet="<p>Status: development. Internal.</p>", extra=None):
    base = _fetcher2(health=health)
    pages = {rd.DEVNET_STATUS_URL: devnet, **(extra or {})}
    return lambda url: pages[url] if url in pages else base(url)


def test_genesis_change_alerts_once_but_first_sight_is_quiet():
    st = {}
    assert rd.check(st, _fetch3()) is None and st[rd.KEY]["genesis"] == G1
    alert = rd.check(st, _fetch3(health=_health(G2)))
    assert "đổi GENESIS" in alert and "FLOP_SESSION_BACKEND=gateway" in alert
    assert rd.check(st, _fetch3(health=_health(G2))) is None
    assert rd.check(st, _fetch3(health='{"network": {"observed_genesis_hash": "nope"}}')) is None
    assert st[rd.KEY]["genesis"] == G2


def test_devnet_status_change_alerts_once():
    st = {}
    assert rd.check(st, _fetch3()) is None and st[rd.KEY]["devnet_status"] == "development"
    opened = "<p>Status: public. Anyone can join.</p>"
    alert = rd.check(st, _fetch3(devnet=opened))
    assert "'development' → 'public'" in alert and "KHÔNG" in alert
    assert rd.check(st, _fetch3(devnet=opened)) is None


def test_issue_31_maintainer_reply_alerts_once():
    st = {}
    u = rd.UNIT_ISSUE_URL
    quiet = {u: json.dumps({"state": "open", "comments": 1}),
             u + "/comments?per_page=100&page=1": json.dumps([_c(1, "NONE")])}
    assert rd.check(st, _fetch3(extra=quiet)) is None
    reply = {u: json.dumps({"state": "open", "comments": 2}),
             u + "/comments?per_page=100&page=1": json.dumps([_c(1, "NONE"), _c(9, "OWNER")])}
    alert = rd.check(st, _fetch3(extra=reply))
    assert "TRẢ LỜI yellowpaper#31" in alert and "ví chủ hay khoá uỷ quyền" in alert
    assert rd.check(st, _fetch3(extra=reply)) is None


def test_parse_genesis():
    assert rd.parse_genesis(_health("0xABCDEF0123")) == "0xabcdef0123"
    assert rd.parse_genesis("<html>") is None and rd.parse_genesis(json.dumps({"network": "devnet"})) is None


def test_spec_maintainer_shown_as_contributor_counts():
    # yellowpaper: người sync spec (`sv`) hiện là CONTRIBUTOR, không phải MEMBER -> vẫn phải báo
    st = {}
    _run(st, _attr(comments=[_c(1, "NONE")]))
    alert = _run(st, _attr(comments=[_c(1, "NONE"), _c(5, "CONTRIBUTOR")]))
    assert alert and "TRẢ LỜI yellowpaper#172" in alert


def test_first_sight_of_an_issue_does_not_alert_on_old_maintainer_comments():
    st = {}
    old = _attr(comments=[_c(3, "CONTRIBUTOR")])
    assert _run(st, old) is None and st[rd.KEY]["attr_seen"] == [3]
    assert _run(st, old) is None


def test_more_new_routes_than_the_seen_cap_alert_once_not_every_hour():
    # Danh sách "đã thấy" chỉ giữ SEEN_KEEP mục; trước đây mục bị cắt lại thành "mới" mỗi giờ.
    st = {}
    f = _fetcher(openapi=_openapi([f"/x/{i:03d}" for i in range(rd.SEEN_KEEP + 15)]))
    assert rd.check(st, f) is not None
    assert rd.check(st, f) is None
    assert rd.check(st, f) is None
    # Route thật sự mới vẫn được báo.
    f2 = _fetcher(openapi=_openapi([f"/x/{i:03d}" for i in range(rd.SEEN_KEEP + 15)] + ["/faucet"]))
    alert = rd.check(st, f2)
    assert alert and "/faucet" in alert


def _pages(base, pages):
    return lambda url: pages[url] if url in pages else base(url)


def _net(issues=(), comments=()):
    return {rd.NETWORK_ISSUES_URL: json.dumps(list(issues)),
            rd.NETWORK_COMMENTS_URL: json.dumps(list(comments))}


def test_docs_alert_has_no_day_one_checklist():
    st = {}
    rd.check(st, _fetcher())
    alert = rd.check(st, _fetcher(llms=_llms(["brand-new-page"])))
    assert alert and "TRANG MỚI" in alert and "Checklist" not in alert


def test_testnet_status_change_carries_the_checklist():
    st = {}
    rd.check(st, _fetcher())
    st[rd.KEY]["status"] = "planned"
    pages = {rd.STATUS_URL: "<p>Status: live. Open.</p>"}
    alert = rd.check(st, _pages(_fetcher(), pages))
    assert alert and "Checklist" in alert


def test_status_still_planned_has_no_checklist():
    st = {}
    rd.check(st, _fetcher())
    st[rd.KEY]["status"] = "unknown-old"
    pages = {rd.STATUS_URL: "<p>Status: planned. Unavailable.</p>"}
    alert = rd.check(st, _pages(_fetcher(), pages))
    assert alert and "Checklist" not in alert


def test_network_repo_maintainer_posts_alert_once_and_community_stays_quiet():
    r = {}
    assert rd._watch_network(r, _pages(lambda u: None, _net([_c(1, "MEMBER")]))) == []      # lần đầu: ghi mốc
    pages = _net([_c(1, "MEMBER"), _c(2, "NONE")], [_c(3, "MEMBER")])
    lines = rd._watch_network(r, _pages(lambda u: None, pages))
    assert len(lines) == 1 and "u3" in lines[0]                     # không báo cộng đồng, không báo lại
    assert rd._watch_network(r, _pages(lambda u: None, pages)) == []


def test_network_repo_outage_keeps_the_marks():
    r = {}
    rd._watch_network(r, _pages(lambda u: None, _net([_c(1, "MEMBER")], [_c(2, "MEMBER")])))
    assert rd._watch_network(r, lambda u: None) == []               # cả hai nguồn lỗi -> không đổi
    assert r["attr_net_seen"] == [1, 2]
    half = {rd.NETWORK_ISSUES_URL: json.dumps([_c(1, "MEMBER")])}   # một nguồn lỗi -> không quên nguồn kia
    assert rd._watch_network(r, _pages(lambda u: None, half)) == []
    assert r["attr_net_seen"] == [1, 2]
    assert rd._watch_network(r, _pages(lambda u: None, _net([_c(1, "MEMBER")], [_c(2, "MEMBER")]))) == []


def test_network_alert_lines_are_capped():
    r = {}
    rd._watch_network(r, _pages(lambda u: None, _net()))
    many = _net([_c(i, "MEMBER") for i in range(10, 30)])
    assert len(rd._watch_network(r, _pages(lambda u: None, many))) == rd.NETWORK_ALERT_LINES
    assert len(r["attr_net_seen"]) <= rd.NETWORK_SEEN_KEEP


def test_credit_issue_208_is_watched():
    st = {}
    pages = {rd.CREDIT_ISSUE_URL: json.dumps({"state": "open", "comments": 1}),
             rd.CREDIT_ISSUE_URL + "/comments?per_page=100&page=1": json.dumps([_c(1, "NONE")])}
    rd.check(st, _pages(_fetcher(), pages))
    pages[rd.CREDIT_ISSUE_URL + "/comments?per_page=100&page=1"] = json.dumps([_c(1, "NONE"), _c(2, "MEMBER")])
    pages[rd.CREDIT_ISSUE_URL] = json.dumps({"state": "open", "comments": 2})
    alert = rd.check(st, _pages(_fetcher(), pages))
    assert alert and "yellowpaper#208" in alert and "Checklist" not in alert


def test_gateway_reporting_testnet_carries_the_checklist_but_devnet_does_not():
    health = HEALTH_DEVNET.replace("devnet", "flop-testnet-1")
    st = {}
    rd.check(st, _fetcher2())
    alert = rd.check(st, _fetcher2(health=health))
    assert alert and "Checklist" in alert
    st[rd.KEY]["network"] = "older"
    alert = rd.check(st, _fetcher2())
    assert alert and "Checklist" not in alert
