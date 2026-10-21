"""Tính năng đăng tin phải có kết quả + dấu chân công khai cho báo cáo ngày."""
import flop_outcomes as o

NOW = 1_800_000_000


def test_real_tclk_pattern_pauses_after_enough_accepts():
    # Đo 2026-10-21: 6 ACCEPT/run, 0 lần payer khoá tiền, 0 việc hoàn tất.
    st, alert, runs = {}, None, 0
    while alert is None and runs < 500:
        o.record(st, "tclk", actions=6, wins=0)
        alert = o.evaluate(st, "tclk", NOW + runs * 300)
        runs += 1
    assert runs == 34                                     # 204 lượt ≈ 2,8 giờ thay vì hàng tuần
    assert alert and "tclk" in alert and "0 kết quả" in alert
    assert o.paused_until(st, "tclk", NOW + runs * 300) > 0


def test_real_engage_pattern_pauses():
    # 823 câu hỏi / 3 trả lời (0,4%): 100 câu đầu thường 0–1 trả lời -> dưới ngưỡng 2%.
    st = {}
    o.record(st, "engage", actions=100, wins=1)
    assert o.evaluate(st, "engage", NOW) is not None
    assert o.paused_until(st, "engage", NOW + 3600)


def test_healthy_feature_is_not_paused_and_window_restarts():
    st = {}
    o.record(st, "tclk", actions=200, wins=30)            # 15% — mức bình thường của board
    assert o.evaluate(st, "tclk", NOW) is None
    assert o.paused_until(st, "tclk", NOW) == 0
    assert st[o.KEY]["tclk"]["actions"] == 0               # cửa sổ đo mới


def test_not_judged_before_enough_actions():
    st = {}
    o.record(st, "tclk", actions=199, wins=0)
    assert o.evaluate(st, "tclk", NOW) is None
    assert st[o.KEY]["tclk"]["actions"] == 199


def test_pause_expires():
    st = {}
    o.record(st, "engage", actions=100, wins=0)
    o.evaluate(st, "engage", NOW, pause_sec=3600)
    assert o.paused_until(st, "engage", NOW + 3599)
    assert o.paused_until(st, "engage", NOW + 3601) == 0


def test_engage_replies_counted_once_per_peer_within_window():
    engaged = {"did:a": NOW - 600, "did:b": NOW - 600, "did:old": NOW - 10 * 3600}
    msgs = [{"from": "did:a", "text": "@NguyenVuLV good question, it is per sender"},
            {"from": "did:a", "text": "also NguyenVuLV note the lane"},       # cùng người -> 1 lần
            {"from": "did:b", "text": "unrelated chatter"},                   # không nhắc ta
            {"from": "did:old", "text": "NguyenVuLV late reply"},             # quá cửa sổ
            {"from": "did:x", "text": "NguyenVuLV hi"}]                       # không phải người được hỏi
    counted = {}
    got = o.new_engage_replies(msgs, engaged, ["nguyenvulv", "NguyenVuLV"], NOW, counted)
    assert got == ["did:a"]
    assert o.new_engage_replies(msgs, engaged, ["nguyenvulv"], NOW + 60, counted) == []


def test_footprint_line_and_spam_warning():
    shares = {"flop": (15, 200), "kibble": (3, 200)}
    line, warn = o.footprint_line({"flop": 28, "kibble": 900}, {"flop": 40, "kibble": 1300, "*": 30}, shares)
    assert "flop 28/40 (7.5% phòng)" in line and "kibble 900/1300 (1.5% phòng)" in line
    assert warn and "r/flop" in warn and "kibble" not in warn
    _, ok = o.footprint_line({"flop": 5}, {"*": 30}, {"flop": (2, 200)})
    assert ok is None


def test_room_share_and_garbage_state():
    assert o.room_share([{"from": "me"}, {"from": "x"}, "junk"], "me") == (1, 2)
    st = {o.KEY: {"tclk": {"actions": "x", "wins": None, "paused_until": "bad"}}}
    assert o.paused_until(st, "tclk", NOW) == 0
    o.record(st, "tclk", actions=3)
    assert st[o.KEY]["tclk"]["actions"] == 3
