"""Tests cho flop_report.py — tóm tắt chi tiêu FLOP (thuần, không mạng)."""

import time

import flop_report as fr

NOW = 1_790_000_000


def _ts(off):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + off))


def _led(entries, bal="100"):
    return {"balances": {"FLOP": bal}, "entries": entries}


def test_summarize_splits_window_and_total():
    led = _led([
        {"kind": "spend", "token": "FLOP", "amount": "0.4", "ts": _ts(-100)},
        {"kind": "spend", "token": "FLOP", "amount": "0.6", "ts": _ts(-3600)},
        {"kind": "credit", "token": "FLOP", "amount": "50", "ts": _ts(-7200)},
        {"kind": "spend", "token": "FLOP", "amount": "9", "ts": _ts(-200_000)},  # ngoài 24h
    ])
    s = fr.summarize(led, NOW, mode="simulation")
    assert s["spent"] == "1" and s["spend_count"] == 2          # chỉ trong cửa sổ
    assert s["credited"] == "50" and s["credit_count"] == 1
    assert s["spent_total"] == "10" and s["spend_count_total"] == 3   # tích luỹ: cả cái cũ
    assert s["balance"] == "100" and s["mode"] == "simulation"


def test_entry_without_timestamp_counts_in_total_not_window():
    """Cắt log/entry hỏng không được làm TỔNG tụt âm thầm, nhưng cũng không được
    đoán là vừa xảy ra."""
    s = fr.summarize(_led([{"kind": "spend", "token": "FLOP", "amount": "7"}]), NOW)
    assert s["spend_count"] == 0 and s["spent"] == "0"
    assert s["spend_count_total"] == 1 and s["spent_total"] == "7"


def test_other_tokens_are_ignored():
    s = fr.summarize(_led([
        {"kind": "spend", "token": "OTHER", "amount": "5", "ts": _ts(-10)},
        {"kind": "spend", "token": "FLOP", "amount": "1", "ts": _ts(-10)},
    ]), NOW)
    assert s["spent"] == "1" and s["spend_count"] == 1


def test_summarize_survives_garbage():
    led = {"balances": {"FLOP": None}, "entries": [
        None, "rác", 42,
        {"kind": "spend", "token": "FLOP", "amount": "khong-phai-so", "ts": _ts(-10)},
        {"kind": "spend", "token": "FLOP", "amount": "2", "ts": "ts hong"},
    ]}
    s = fr.summarize(led, NOW)
    assert s["balance"] == "0" and s["spend_count"] == 1 and s["spent"] == "0"
    assert s["spend_count_total"] == 2
    assert fr.summarize(None, NOW)["spent"] == "0"              # sổ cái rỗng/None


def test_empty_ledger_reports_zeros():
    s = fr.summarize({"balances": {}, "entries": []}, NOW, mode="simulation")
    assert s["spent"] == "0" and s["spend_count"] == 0 and s["balance"] == "0"


def test_report_marks_simulation_loudly():
    """Báo cáo mô phỏng KHÔNG được đọc như chi tiêu thật."""
    txt = fr.format_report(fr.summarize(_led([]), NOW, mode="simulation"))
    assert "simulation" in txt and "KHÔNG phải chi tiêu thật" in txt


def test_report_has_no_disclaimer_on_testnet():
    txt = fr.format_report(fr.summarize(_led([]), NOW, mode="testnet"))
    assert "KHÔNG phải chi tiêu thật" not in txt


def test_report_includes_counts_and_extras():
    led = _led([{"kind": "spend", "token": "FLOP", "amount": "0.5", "ts": _ts(-10)}], bal="99.5")
    txt = fr.format_report(fr.summarize(led, NOW, mode="testnet"),
                           date_label="2026-10-17",
                           extras={"faucet": "1 claim", "session": "6 settled"})
    assert "2026-10-17" in txt
    assert "chi 24h: 0.5 FLOP (1 lần)" in txt
    assert "số dư: 99.5 FLOP" in txt
    assert "faucet: 1 claim" in txt and "session: 6 settled" in txt


# --- spend-health: "có đang KIẾM HỤT không" ------------------------------------------

def _pace(budget="100", spent="10", target="50"):
    return {"enabled": True, "daily_budget": budget,
            "spent_today": spent, "target_now": target}


def test_health_flags_behind_pace_with_idle_balance():
    s = fr.summarize(_led([], bal="87.6"), NOW)
    h = fr.spend_health(s, _pace(spent="12", target="50"))
    assert h["pace"] == "behind" and h["idle_warn"] is True
    assert h["elapsed_pct"] == 50 and h["spent_pct"] == 12


def test_health_on_track_does_not_warn():
    s = fr.summarize(_led([], bal="87.6"), NOW)
    h = fr.spend_health(s, _pace(spent="48", target="50"))
    assert h["pace"] == "on-track" and h["idle_warn"] is False


def test_health_ahead_of_pace():
    h = fr.spend_health(fr.summarize(_led([], bal="10"), NOW), _pace(spent="80", target="50"))
    assert h["pace"] == "ahead" and h["idle_warn"] is False   # chi nhanh != bỏ phí


def test_health_pacer_off_warns_only_when_nothing_spent():
    """Không kêu chỉ vì còn số dư — lúc nào cũng còn số dư. Chỉ kêu khi THỰC SỰ đứng im."""
    idle = fr.summarize(_led([], bal="50"), NOW)                       # 24h: 0 lần chi
    assert fr.spend_health(idle, None)["idle_warn"] is True
    busy = fr.summarize(_led([{"kind": "spend", "token": "FLOP", "amount": "1",
                               "ts": _ts(-60)}], bal="50"), NOW)
    assert fr.spend_health(busy, None)["idle_warn"] is False
    assert fr.spend_health(busy, None)["pace"] == "off"


def test_health_no_warning_when_balance_is_zero():
    s = fr.summarize(_led([], bal="0"), NOW)
    assert fr.spend_health(s, _pace(spent="0", target="50"))["idle_warn"] is False


def test_health_survives_missing_or_zero_budget():
    s = fr.summarize(_led([], bal="5"), NOW)
    for p in (None, {}, {"enabled": True, "daily_budget": "0"},
              {"enabled": False, "daily_budget": "100"}, "rác"):
        h = fr.spend_health(s, p)
        assert h["pace"] == "off" and h["elapsed_pct"] is None


def test_report_renders_health_lines():
    led = _led([{"kind": "spend", "token": "FLOP", "amount": "12.4", "ts": _ts(-60)}], bal="87.6")
    txt = fr.format_report(fr.summarize(led, NOW, mode="simulation"),
                           pacing=_pace(spent="12.4", target="50"))
    assert "nhàn rỗi: 87.6 FLOP" in txt and "KHÔNG sinh phân bổ" in txt
    assert "50% ngày đã trôi" in txt and "12% ngân sách đã chi" in txt
    assert "ĐANG CHẬM" in txt


def test_report_omits_pace_line_when_pacer_off():
    txt = fr.format_report(fr.summarize(_led([], bal="1"), NOW))
    assert "ngày đã trôi" not in txt


# --- rút gọn dòng kibble cho tin nhắn (log Actions giữ bản đầy đủ) -------------------

_FULL = ("live 2\u2713/0skip/200scan | kibble-score delivered=300 useful=44 not=33 "
         "useful_rate=57% pending=223 unattested=0 lifetime=58%/n=48")


def test_compact_kibble_keeps_the_three_numbers_that_matter():
    out = fr.compact_kibble(_FULL)
    assert out == "2\u2713 \u00b7 useful 57% \u00b7 lifetime 58% (n=48)"
    assert len(out) < len(_FULL) / 3          # 126 -> 37 ký tự


def test_compact_kibble_keeps_the_dry_marker():
    """Bỏ 'dry' đi thì '3✓' trông như đã giao THẬT — rút gọn không được đổi nghĩa."""
    out = fr.compact_kibble("dry 3\u2713/1skip/200scan | kibble-score delivered=5 "
                            "useful=1 not=0 useful_rate=100% pending=4 unattested=0")
    assert out.startswith("dry 3\u2713") and "useful 100%" in out


def test_compact_kibble_passes_through_unparseable_status():
    """Thà dài còn hơn mất thông tin: trạng thái không khớp mẫu phải đi qua NGUYÊN VẸN."""
    for t in ("off", "skip-outage", "error", "", None):
        assert fr.compact_kibble(t) == (t or "")


def test_compact_kibble_handles_partial_fields():
    assert fr.compact_kibble("live 0\u2713/5skip/200scan useful_rate=n/a") == \
        "0\u2713 \u00b7 useful n/a"
    assert fr.compact_kibble("lifetime 58%/n=48 nhưng thiếu phần còn lại") == \
        "lifetime 58%/n=48 nhưng thiếu phần còn lại"   # không khớp 'lifetime=' -> giữ nguyên


def test_display_rounds_long_decimals_but_keeps_ledger_math():
    """Pacer sinh số như 95.90277777777777777777777778 — tin nhắn phải đọc được."""
    led = {"balances": {"FLOP": "4.09722222222222222222222222"}, "entries": [
        {"kind": "spend", "token": "FLOP",
         "amount": "95.90277777777777777777777778", "ts": _ts(-60)}]}
    s = fr.summarize(led, NOW)
    assert s["spent"] == "95.902778" and s["balance"] == "4.097222"
    txt = fr.format_report(s)
    assert "95.902778" in txt and "7777777" not in txt


def test_display_keeps_small_amounts_exact():
    led = {"balances": {"FLOP": "99.993"}, "entries": [
        {"kind": "spend", "token": "FLOP", "amount": "0.001", "ts": _ts(-60)}]}
    s = fr.summarize(led, NOW)
    assert s["spent"] == "0.001" and s["balance"] == "99.993"
