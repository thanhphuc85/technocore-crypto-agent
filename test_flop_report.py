"""Tests cho flop_report.py — tóm tắt chi tiêu FLOP (thuần, không mạng)."""

import re
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
    # Dòng dẫn đầu nay là THƯỚC ĐO THẬT (mua compute), không phải tổng chi gộp.
    assert "MUA COMPUTE (được tính)" in txt
    assert "ghi sổ LLM (KHÔNG tính): 0.5 FLOP" in txt
    assert "số dư 99.5" in txt
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


# --- tổng CHẠY thắng log bị cắt + dự phóng kỳ testnet ---------------------------------

def _led_cum(cum, count=None, by_day=None, bal="71.5", entries=20):
    d = {"balances": {"FLOP": bal}, "spent_cumulative": {"FLOP": cum},
         "entries": [{"kind": "spend", "token": "FLOP", "amount": "1.8", "ts": _ts(-60)}
                     for _ in range(entries)]}
    if count is not None:
        d["spend_count_cumulative"] = {"FLOP": count}
    if by_day is not None:
        d["spent_by_day"] = {"FLOP": by_day}
    return d


def test_total_comes_from_running_sum_not_the_trimmed_log():
    """PR #134 cắt entries còn 20 khi publish -> cộng từ log hụt hàng chục lần."""
    s = fr.summarize(_led_cum("19.628", count=204), NOW)
    assert s["spent_total"] == "19.628" and s["spend_count_total"] == 204


def test_total_falls_back_to_entries_for_legacy_ledgers():
    s = fr.summarize({"balances": {"FLOP": "9"}, "entries": [
        {"kind": "spend", "token": "FLOP", "amount": "2", "ts": _ts(-60)}]}, NOW)
    assert s["spent_total"] == "2"


def test_projection_uses_completed_days_only():
    """Hôm nay là ngày DỞ DANG — gộp vào sẽ kéo tụt trung bình."""
    today = time.strftime("%Y-%m-%d", time.gmtime(NOW))
    by = {"2026-10-14": "100000", "2026-10-15": "99000", "2026-10-16": "101000",
          today: "20000"}
    p = fr.projection(_led_cum("320000", by_day=by), NOW, horizon_days=90)
    assert p["days"] == 3 and p["basis"] == "days"
    assert p["avg_per_day"] == "100000" and p["projected"] == "9000000"


def test_projection_falls_back_to_today_run_rate():
    """Ngày đầu chạy: chưa có ngày trọn -> ngoại suy từ phần ngày đã trôi."""
    noon = NOW - (NOW % 86400) + 43200                    # đúng 12:00 UTC
    today = time.strftime("%Y-%m-%d", time.gmtime(noon))
    p = fr.projection(_led_cum("50", by_day={today: "50"}), noon, horizon_days=90)
    assert p["basis"] == "runrate" and p["avg_per_day"] == "100"      # 50 / 0.5 ngày
    assert p["projected"] == "9000"


def test_projection_empty_when_nothing_spent():
    p = fr.projection(_led_cum("0", by_day={}), NOW)
    assert p["projected"] is None and p["basis"] == ""
    assert fr.projection(None, NOW)["projected"] is None


def test_projection_horizon_is_configurable():
    today = time.strftime("%Y-%m-%d", time.gmtime(NOW))
    by = {"2026-10-16": "100", today: "1"}
    assert fr.projection(_led_cum("101", by_day=by), NOW, horizon_days=30)["projected"] == "3000"
    assert fr.projection(_led_cum("101", by_day=by), NOW, horizon_days=0)["projected"] == "0"


def test_report_says_spent_not_earned():
    """9 triệu là ĐÃ CHI. Phần thưởng chia pro-rata, không ai biết trước tổng chi mạng."""
    today = time.strftime("%Y-%m-%d", time.gmtime(NOW))
    by = {"2026-10-14": "100000", "2026-10-15": "100000", today: "5"}
    led = _led_cum("200005", count=9, by_day=by)
    txt = fr.format_report(fr.summarize(led, NOW, mode="testnet"),
                           proj=fr.projection(led, NOW, horizon_days=90))
    assert "~9,000,000 FLOP ĐÃ CHI" in txt and "KHÔNG phải nhận" in txt
    assert "pro-rata" in txt


def test_big_numbers_get_thousand_separators():
    assert fr._grp("9000000") == "9,000,000"
    assert fr._grp("1234.5678") == "1,234.5678"
    assert fr._grp("-2500") == "-2,500"
    assert fr._grp("abc") == "abc" and fr._grp("") == ""


# --- cộng dồn theo ngày: "ngày N/90 · hôm nay +X -> tích luỹ Y" -----------------------

def _days_ago(n):
    return time.strftime("%Y-%m-%d", time.gmtime(NOW - n * 86400))


def test_day_index_counts_from_first_spend_day():
    led = _led_cum("300000", by_day={_days_ago(2): "100000", _days_ago(1): "100000",
                                     _days_ago(0): "100000"})
    led["first_spend_day"] = _days_ago(2)
    p = fr.projection(led, NOW, horizon_days=90)
    assert p["day_index"] == 3 and p["days_left"] == 87
    assert p["today"] == "100000" and p["total_so_far"] == "300000"


def test_report_shows_the_daily_accumulation():
    led = _led_cum("300000", count=9, by_day={_days_ago(2): "100000", _days_ago(1): "100000",
                                              _days_ago(0): "100000"})
    led["first_spend_day"] = _days_ago(2)
    txt = fr.format_report(fr.summarize(led, NOW, mode="testnet"),
                           proj=fr.projection(led, NOW, horizon_days=90))
    assert "ngày 3/90" in txt
    assert "hôm nay +100,000" in txt and "tích luỹ 300,000 FLOP" in txt
    assert "còn 87 ngày" in txt


def test_announced_testnet_start_overrides_first_spend():
    """Khi FLOP công bố mốc bắt đầu, nó thắng ngày chi đầu tiên của ta."""
    led = _led_cum("10", by_day={_days_ago(0): "10"})
    led["first_spend_day"] = _days_ago(1)
    led["testnet_start"] = _days_ago(10)
    assert fr.projection(led, NOW, horizon_days=90)["day_index"] == 11


def test_day_index_absent_without_a_start_date():
    led = _led_cum("10", by_day={_days_ago(0): "10"})
    p = fr.projection(led, NOW, horizon_days=90)
    assert p["day_index"] is None and p["days_left"] is None
    txt = fr.format_report(fr.summarize(led, NOW), proj=p)
    assert "tích luỹ" not in txt and "còn " not in txt
    assert not re.search(r"ngày \d+/\d+", txt)        # không có dòng "ngày N/90"


def test_day_index_survives_a_broken_start_date():
    led = _led_cum("10", by_day={_days_ago(0): "10"})
    led["first_spend_day"] = "khong-phai-ngay"
    assert fr.projection(led, NOW, horizon_days=90)["day_index"] is None


def test_days_left_never_goes_negative():
    led = _led_cum("10", by_day={_days_ago(0): "10"})
    led["first_spend_day"] = _days_ago(200)
    p = fr.projection(led, NOW, horizon_days=90)
    assert p["day_index"] == 201 and p["days_left"] == 0


# --- tach CHI PHIEN (duoc tinh) khoi metering (khong tinh) ---------------------------

def _led_sess(sess, sess_n, total, by_day=None, sess_by_day=None, bal="50000"):
    d = {"balances": {"FLOP": bal},
         "spent_cumulative": {"FLOP": total}, "spend_count_cumulative": {"FLOP": 3000},
         "session_cumulative": {"FLOP": sess}, "session_count_cumulative": {"FLOP": sess_n},
         "entries": []}
    if by_day is not None:
        d["spent_by_day"] = {"FLOP": by_day}
    if sess_by_day is not None:
        d["session_by_day"] = {"FLOP": sess_by_day}
    return d


def test_summary_separates_session_spend_from_metering():
    """Thước đo airdrop CHỈ là 'compute purchased in settled sessions'."""
    today = time.strftime("%Y-%m-%d", time.gmtime(NOW))
    s = fr.summarize(_led_sess("287000", 2870, "320000",
                               sess_by_day={today: "99000"}), NOW)
    assert s["session_total"] == "287000" and s["session_count"] == 2870
    assert s["session_today"] == "99000"
    assert s["spent_total"] == "320000"            # tổng gộp vẫn có, nhưng là phụ


def test_report_leads_with_the_metric_that_counts():
    today = time.strftime("%Y-%m-%d", time.gmtime(NOW))
    txt = fr.format_report(fr.summarize(_led_sess("287000", 2870, "320000",
                                                  sess_by_day={today: "99000"}),
                                        NOW, mode="testnet"))
    first = txt.split("\n")[1]
    assert "MUA COMPUTE (được tính)" in first and "287,000" in first
    assert "ghi sổ LLM (KHÔNG tính): 33,000" in txt     # metering = tổng 320k trừ 287k compute


def test_projection_uses_session_spend_not_total():
    """Dự phóng 90 ngày phải theo con số ĐƯỢC TÍNH, không theo tổng gộp."""
    d0, d1 = _days_ago(2), _days_ago(1)
    led = _led_sess("200000", 2000, "320000",
                    by_day={d0: "100000", d1: "100000"},
                    sess_by_day={d0: "90000", d1: "90000"})
    p = fr.projection(led, NOW, horizon_days=90)
    assert p["avg_per_day"] == "90000" and p["projected"] == "8100000"
    assert p["total_so_far"] == "200000"


def test_projection_falls_back_to_total_for_legacy_ledgers():
    d0 = _days_ago(1)
    led = {"balances": {"FLOP": "1"}, "spent_cumulative": {"FLOP": "50"},
           "spent_by_day": {"FLOP": {d0: "25"}}, "entries": []}
    p = fr.projection(led, NOW, horizon_days=10)
    assert p["avg_per_day"] == "25" and p["total_so_far"] == "50"


def test_session_fields_default_to_zero_when_absent():
    s = fr.summarize({"balances": {}, "entries": []}, NOW)
    assert s["session_total"] == "0" and s["session_count"] == 0
    assert s["session_today"] == "0"



def test_compact_kibble_shows_clean_score_when_present():
    full = _FULL + " clean=82%/n=40"
    assert "sạch 82% (n=40)" in fr.compact_kibble(full)
    assert "sạch" not in fr.compact_kibble(_FULL)          # chưa có bộ sạch -> không bịa



def test_meter_line_excludes_compute_and_credits_survive_trim():
    led = _led([], bal="66")
    led.update({"spent_cumulative": {"FLOP": "225.5"}, "spend_count_cumulative": {"FLOP": 4704},
                "session_cumulative": {"FLOP": "127.5"}, "session_count_cumulative": {"FLOP": 1985},
                "credit_by_hour": {"FLOP": {time.strftime("%Y-%m-%dT%H", time.gmtime(NOW - 7 * 3600)): ["100", 1],
                                             time.strftime("%Y-%m-%dT%H", time.gmtime(NOW - 30 * 3600)): ["5", 1]}}})
    s = fr.summarize(led, NOW)
    assert s["meter_total"] == "98" and s["meter_count"] == 2719
    assert s["credited"] == "100" and s["credit_count"] == 1            # nạp 7h trước, entries đã bị cắt
    txt = fr.format_report(s)
    assert "ghi sổ LLM (KHÔNG tính): 98 FLOP (2719 lần)" in txt and "nạp 24h: 100 FLOP (1 lần)" in txt
