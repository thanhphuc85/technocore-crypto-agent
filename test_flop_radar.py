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
