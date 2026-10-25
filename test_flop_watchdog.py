"""Bộ canh chi thật (testnet): im lặng khi không chi được on-chain = mất các ngày đầu."""
import flop_watchdog as wd


def _miss(st, now, status="0✓ await_poui/pending", spent=0, **kw):
    return wd.observe(st, settled_onchain=0, status=status, spent_today_onchain=spent,
                      now=now, balance="50", pending=3, reason="chưa trả proof (keys: [x])",
                      alert_runs=6, realert_sec=6 * 3600, **kw)


def test_no_alert_before_threshold_then_alert_with_details():
    st = {}
    for i in range(5):
        assert _miss(st, 1000 + i * 300)["alert"] is None
    msg = _miss(st, 1000 + 5 * 300)["alert"]
    assert msg and "6 run liền" in msg and "~25 phút" in msg
    assert "await_poui/pending ×6" in msg
    assert "chưa trả proof (keys: [x])" in msg
    assert "Số dư sổ: 50 FLOP" in msg and "phiên chờ proof: 3" in msg


def test_realert_only_after_cooldown():
    st = {}
    for i in range(6):
        _miss(st, 1000 + i * 300)
    assert _miss(st, 1000 + 6 * 300)["alert"] is None            # vừa báo -> không spam
    assert _miss(st, 1000 + 6 * 300 + 6 * 3600)["alert"]         # vẫn kẹt sau 6 giờ -> nhắc lại


def test_breakdown_shows_where_runs_stop():
    st = {}
    for i in range(4):
        _miss(st, 1000 + i * 300, status="0✓ verify/disputed")
    msg = _miss(st, 3000, status="0✓ submit/error_submit")["alert"] or \
        _miss(st, 3300, status="0✓ submit/error_submit")["alert"]
    assert "verify/disputed ×4" in msg and "submit/error_submit" in msg


def test_pace_wait_is_healthy_when_already_spent_today():
    st = {}
    for i in range(10):
        out = _miss(st, 1000 + i * 300, status="0✓ pace-wait", spent=12.5)
        assert out["alert"] is None
    assert st[wd.KEY] == {}


def test_pace_wait_with_nothing_spent_today_is_a_miss():
    # Ví 0 / faucet chưa nạp / giá sàn quá cao -> pacer chờ mãi mà chưa chi đồng nào.
    st = {}
    outs = [_miss(st, 1000 + i * 300, status="0✓ pace-wait", spent=0) for i in range(6)]
    assert outs[-1]["alert"] and "pace-wait ×6" in outs[-1]["alert"]


def test_settled_resets_and_reports_recovery_only_after_an_alert():
    st = {}
    for i in range(6):
        _miss(st, 1000 + i * 300)
    out = wd.observe(st, settled_onchain=2, status="2✓ settle/spent_onchain",
                     spent_today_onchain=1.0, now=1000 + 7 * 300)
    assert out["recovered"] and "35 phút" in out["recovered"] and st[wd.KEY] == {}
    quiet = wd.observe(st, settled_onchain=1, status="1✓ settle/spent_onchain",
                       spent_today_onchain=2.0, now=9999)
    assert quiet == {"alert": None, "recovered": None}


def test_note_pending_keeps_sessions_with_ttl_and_cap():
    st = {}
    pend = {"stage": "await_poui", "outcome": "pending", "request_id": "r1",
            "session_id": "net-1", "miner_did": "did:key:zM"}
    assert wd.note_pending(st, pend, 1000) == 1
    assert wd.note_pending(st, {"stage": "settle", "outcome": "spent_onchain"}, 1001) == 1
    assert wd.note_pending(st, {}, 1000 + wd.PENDING_TTL_SEC + 1) == 0      # quá hạn -> bỏ
    for i in range(wd.PENDING_CAP + 5):
        wd.note_pending(st, dict(pend, request_id=f"r{i}"), 5000)
    assert len(st[wd.PENDING_KEY]) == wd.PENDING_CAP
    assert st[wd.PENDING_KEY][-1]["session_id"] == "net-1"


def test_garbage_state_does_not_crash():
    st = {wd.KEY: "rác", wd.PENDING_KEY: ["x", {"ts": "nope"}]}
    assert wd.note_pending(st, None, 10) == 1
    assert _miss(st, 10)["alert"] is None


def test_pending_block_caps_outstanding_sessions():
    st, now = {}, 1_800_000_000
    for i in range(3):
        wd.note_pending(st, {"stage": "await_poui", "outcome": "pending", "request_id": f"r{i}"}, now)
    assert wd.pending_block(st, now, 3) == (True, 3)
    assert wd.pending_block(st, now, 4) == (False, 3)
    assert wd.pending_block(st, now, 0) == (False, 3)                  # 0 = tắt
    assert wd.pending_block(st, now + wd.PENDING_TTL_SEC + 1, 3) == (False, 0)   # hết hạn -> mở lại



def test_pending_block_lifts_after_window_not_after_48h():
    st, now = {}, 1_800_000_000
    for i in range(3):
        wd.note_pending(st, {"stage": "await_poui", "outcome": "pending", "request_id": f"r{i}"}, now)
    assert wd.pending_block(st, now + 5 * 3600, 3)[0] is True             # trong 6h: chặn
    blocked, n = wd.pending_block(st, now + 6 * 3600 + 1, 3)
    assert blocked is False and n == 0                                     # hết 6h: đăng tiếp
    assert len(st[wd.PENDING_KEY]) == 3                                    # danh sách vẫn giữ để đối soát
    assert wd.pending_block(st, now + 3600, 3, window_sec=1800)[0] is False


def test_gateway_waiting_is_neutral_not_a_miss():
    # Faucet 25 FLOP/ngày: phần lớn lượt là "gateway/pace_wait" — đúng thiết kế, không được
    # sinh báo động "không chi thật" (trước đây: báo động giả sau 6 lượt, lặp mỗi 6h).
    st, now = {}, 1_800_000_000
    for i in range(30):
        r = wd.observe(st, settled_onchain=0, status="0✓ gateway/pace_wait", spent_today_onchain=0,
                       now=now + i * 300)
        assert r["alert"] is None
    for i in range(10):
        assert wd.observe(st, settled_onchain=0, status="0✓ gateway/exhausted", spent_today_onchain=5,
                          now=now + 9000 + i * 300)["alert"] is None


def test_broken_settlement_still_alerts_through_waiting_runs():
    # Mua được (spent) nhưng KHÔNG bao giờ settle, xen giữa là lượt chờ: vẫn phải báo động.
    st, now, alerts = {}, 1_800_000_000, []
    for i in range(14):
        status = "2✓ gateway/spent" if i % 2 == 0 else "0✓ gateway/pace_wait"
        r = wd.observe(st, settled_onchain=0, status=status, spent_today_onchain=0, now=now + i * 300)
        if r["alert"]:
            alerts.append(r["alert"])
    assert len(alerts) == 1 and "gateway/spent" in alerts[0]
