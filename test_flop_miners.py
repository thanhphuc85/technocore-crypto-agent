"""Tests cho flop_miners.py — độ phân tán thợ đào (thuần, không mạng)."""

import flop_miners as fm

A, B, C = "did:key:zAAA", "did:key:zBBB", "did:key:zCCC"


def _build(seq, denied=0):
    s = {}
    for d in seq:
        s = fm.record_settled(s, d)
    for _ in range(denied):
        s = fm.record_denied(s)
    return s


def test_counts_and_concentration():
    s = _build([A] * 7 + [B] * 2 + [C], denied=1)
    summ = fm.summarize(s)
    assert summ["distinct"] == 3 and summ["total"] == 10 and summ["denied"] == 1
    assert summ["top_did"] == A and summ["top_share"] == 0.7


def test_record_returns_new_dict_and_does_not_mutate():
    s = _build([A])
    before = {"counts": dict(s["counts"]), "denied": s["denied"]}
    fm.record_settled(s, B)
    fm.record_denied(s)
    assert s["counts"] == before["counts"] and s["denied"] == before["denied"]


def test_blank_miner_is_ignored():
    for bad in ("", "   ", None):
        assert fm.summarize(fm.record_settled({}, bad))["total"] == 0


def test_summary_of_empty_state_has_no_fake_zero_share():
    """0% và 'chưa có dữ liệu' là hai chuyện khác nhau."""
    summ = fm.summarize({})
    assert summ["total"] == 0 and summ["top_share"] is None
    assert "chưa có phiên nào" in fm.format_line(summ)


def test_warning_fires_only_above_threshold_and_sample_size():
    assert fm.concentration_warning(fm.summarize(_build([A] * 2))) is None    # mẫu quá nhỏ
    low = fm.summarize(_build([A] * 4 + [B] * 4 + [C] * 4))
    assert fm.concentration_warning(low) is None                              # 33% < 50%
    high = fm.summarize(_build([A] * 8 + [B] * 2))
    w = fm.concentration_warning(high)
    assert w and "80%" in w and A[:20] in w


def test_warning_threshold_is_tunable():
    summ = fm.summarize(_build([A] * 6 + [B] * 4))                 # 60%
    assert fm.concentration_warning(summ, threshold=0.9) is None
    assert fm.concentration_warning(summ, threshold=0.5) is not None


def test_cap_blocks_new_miners_but_keeps_counting_known_ones():
    """Chạm trần chỉ chặn thợ đào MỚI; vẫn đếm người đã có, nếu không tỉ lệ tập trung lệch."""
    s = _build([f"did:key:z{i:04d}" for i in range(fm.MINER_CAP)])
    assert fm.summarize(s)["distinct"] == fm.MINER_CAP
    s = fm.record_settled(s, "did:key:zNEWONE")
    assert fm.summarize(s)["distinct"] == fm.MINER_CAP               # không thêm người mới
    s = fm.record_settled(s, "did:key:z0000")
    assert fm.summarize(s)["total"] == fm.MINER_CAP + 1              # vẫn đếm người cũ


def test_tolerates_garbage_state():
    for bad in (None, "rác", 42, {"counts": "x", "denied": "y"},
                {"counts": {"": 3, A: "n", B: -1, C: 2}}):
        summ = fm.summarize(bad)
        assert isinstance(summ["distinct"], int) and summ["denied"] >= 0
    assert fm.summarize({"counts": {A: "n", B: -1, C: 2}})["total"] == 2


def test_denied_shows_even_with_no_settled_sessions():
    """Chốt chặn fleet bắn mà chưa settle phiên nào -> vẫn phải thấy con số."""
    line = fm.format_line(fm.summarize(_build([], denied=3)))
    assert "fleet bị chặn 3" in line
