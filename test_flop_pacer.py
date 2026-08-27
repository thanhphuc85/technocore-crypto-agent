"""
Test Dynamic Spend Rate (flop_pacer) — python -m pytest test_flop_pacer.py -q
"""

import calendar
import pytest
import flop_pacer as fp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("FLOP_DAILY_BUDGET", "FLOP_MAX_PER_RUN", "FLOP_MIN_SPEND",
              "FLOP_PACER_FILE", "FLOP_TOKEN_SYMBOL"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def pfile(tmp_path):
    return str(tmp_path / "pacer.json")


MIDNIGHT = calendar.timegm((2026, 8, 27, 0, 0, 0, 0, 0, 0))
NOON = MIDNIGHT + 12 * 3600


def test_disabled_without_budget(pfile):
    assert fp.next_spend_amount(now=NOON, path=pfile) is None


def test_even_pace_target(pfile, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    # nửa ngày -> mục tiêu 12, chưa chi gì -> đến hạn 12
    assert fp.next_spend_amount(now=NOON, path=pfile) == "12"
    # ghi đã chi 12 -> tới hạn = 0
    fp.record_spend("12", now=NOON, path=pfile)
    assert fp.next_spend_amount(now=NOON, path=pfile) == "0"


def test_max_per_run_cap(pfile, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    monkeypatch.setenv("FLOP_MAX_PER_RUN", "1")
    assert fp.next_spend_amount(now=NOON, path=pfile) == "1"   # due 12 nhưng kẹp 1


def test_min_spend_waits(pfile, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "1")
    # 1 giây sau nửa đêm -> due ~ 1/86400 < min 0.0001 -> "0" (đợi tích thêm)
    assert fp.next_spend_amount(now=MIDNIGHT + 1, path=pfile) == "0"


def test_day_reset(pfile, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    fp.record_spend("12", now=NOON, path=pfile)
    next_day_noon = NOON + 24 * 3600
    # sang ngày mới -> spent_today reset -> lại đến hạn 12 lúc trưa
    assert fp.next_spend_amount(now=next_day_noon, path=pfile) == "12"


def test_pacing_status(pfile, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    fp.record_spend("6", now=NOON, path=pfile)
    st = fp.pacing_status(now=NOON, path=pfile)
    assert st["enabled"] is True
    assert st["daily_budget"] == "24"
    assert st["spent_today"] == "6"
    assert st["target_now"] == "12"
    assert st["next_due"] == "6"        # 12 - 6
