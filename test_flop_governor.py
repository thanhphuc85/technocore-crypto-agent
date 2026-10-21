"""Phanh cứng số tin chat/phòng/ngày — lưới an toàn cho bug kiểu #154."""
import calendar

import flop_governor as g

DAY1 = calendar.timegm((2026, 10, 21, 0, 5, 0))
CAPS = g.parse_caps("")                     # mặc định


def _post(st, room, now, caps=CAPS):
    r = g.check(st, room, caps, now)
    if r["allowed"]:
        g.record(st, room, now)
    return r


def test_bug_154_scenario_flop_room_stops_at_cap():
    # Telemetry mỗi 5 phút như bug #154 (287 lần, gọn trong một ngày UTC): dừng ở tin thứ cap+1.
    st, allowed, alerts = {}, 0, []
    for i in range(287):
        r = _post(st, "flop", DAY1 + i * 300)
        allowed += r["allowed"]
        if r["alert"]:
            alerts.append(r["alert"])
    assert allowed == CAPS["flop"] == 40
    assert len(alerts) == 1 and "r/flop" in alerts[0] and "40" in alerts[0]   # báo ĐÚNG 1 lần


def test_counts_only_successful_posts():
    st = {}
    for _ in range(5):
        assert g.check(st, "flop", CAPS, DAY1)["allowed"]    # kiểm mà không ghi -> không tốn trần
    assert g.check(st, "flop", CAPS, DAY1)["count"] == 0
    g.record(st, "flop", DAY1)
    assert g.check(st, "flop", CAPS, DAY1)["count"] == 1


def test_new_utc_day_resets_counts_and_alerts():
    st = {}
    for i in range(41):
        _post(st, "flop", DAY1 + i)
    assert not g.check(st, "flop", CAPS, DAY1 + 100)["allowed"]
    nxt = DAY1 + 86400
    r = g.check(st, "flop", CAPS, nxt)
    assert r["allowed"] and r["count"] == 0
    assert st[g.KEY]["alerted"] == []


def test_rooms_are_independent_and_unknown_rooms_use_default():
    st = {}
    for i in range(40):
        _post(st, "flop", DAY1 + i)
    assert g.check(st, "kibble", CAPS, DAY1)["allowed"]
    assert g.cap_for(CAPS, "some-new-room") == CAPS["*"]


def test_parse_caps_overrides_and_ignores_garbage():
    caps = g.parse_caps("flop=10, kibble = 5 ,tclk-offers=0,bad,x=abc,y=-3,*=7")
    assert caps["flop"] == 10 and caps["kibble"] == 5 and caps["*"] == 7
    assert caps["tclk-offers"] == 0 and "y" not in caps and "x" not in caps
    st = {}
    assert not g.check(st, "tclk-offers", caps, DAY1)["allowed"]       # 0 = chặn hẳn


def test_merge_takes_max_same_day_and_ignores_other_day():
    st = {}
    g.record(st, "flop", DAY1)
    g.merge(st, {"day": "2026-10-21", "rooms": {"flop": 30, "kibble": 2}, "alerted": ["meta"]}, DAY1)
    assert st[g.KEY]["rooms"] == {"flop": 30, "kibble": 2} and st[g.KEY]["alerted"] == ["meta"]
    g.merge(st, {"day": "2026-10-20", "rooms": {"flop": 999}}, DAY1)    # hôm qua -> bỏ
    assert st[g.KEY]["rooms"]["flop"] == 30


def test_garbage_state_does_not_crash():
    st = {g.KEY: {"day": "2026-10-21", "rooms": {"flop": "x", "kibble": 3}, "alerted": [1, "a"]}}
    r = g.check(st, "kibble", CAPS, DAY1)
    assert r["allowed"] and r["count"] == 3 and st[g.KEY]["alerted"] == ["a"]
    assert "kibble 3/1300" in g.summary_line(st, CAPS, DAY1)
