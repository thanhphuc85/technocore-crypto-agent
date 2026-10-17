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
