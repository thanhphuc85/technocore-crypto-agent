"""
Test Dynamic Spend Rate (flop_pacer) — python -m pytest test_flop_pacer.py -q
"""

import calendar
from decimal import Decimal

import pytest
import flop_pacer as fp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("FLOP_DAILY_BUDGET", "FLOP_MAX_PER_RUN", "FLOP_MIN_SPEND",
              "FLOP_PACER_FILE", "FLOP_TOKEN_SYMBOL", "FLOP_PACE_JITTER_PCT"):
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


# --- jitter chống sybil (FLOP_PACE_JITTER_PCT) ------------------------------------

def test_no_jitter_by_default(pfile, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    # rng bị bỏ qua khi pct=0 -> nhịp tuyến tính cũ, tất định.
    assert fp.next_spend_amount(now=NOON, path=pfile, rng=lambda: 0.0) == "12"


def test_jitter_scales_due_deterministically(pfile, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    monkeypatch.setenv("FLOP_PACE_JITTER_PCT", "25")
    # due=12 (NOON). rng=0.0 -> hệ số 0.75; rng=0.5 -> 1.0; rng~1 -> 1.25.
    assert fp.next_spend_amount(now=NOON, path=pfile, rng=lambda: 0.0) == "9"
    assert fp.next_spend_amount(now=NOON, path=pfile, rng=lambda: 0.5) == "12"


def test_jitter_up_reclamped_by_cap(pfile, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    monkeypatch.setenv("FLOP_MAX_PER_RUN", "12")
    monkeypatch.setenv("FLOP_PACE_JITTER_PCT", "50")
    # due=12 -> cap 12 -> jitter lên (*~1.5) -> re-clamp về trần 12.
    assert fp.next_spend_amount(now=NOON, path=pfile, rng=lambda: 0.999999) == "12"


def test_jitter_below_min_spend_waits(pfile, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    monkeypatch.setenv("FLOP_MIN_SPEND", "10")
    monkeypatch.setenv("FLOP_PACE_JITTER_PCT", "50")
    # due=12, jitter xuống (*0.5=6) < MIN_SPEND 10 -> "0" (đợi tích thêm).
    assert fp.next_spend_amount(now=NOON, path=pfile, rng=lambda: 0.0) == "0"


def test_jitter_pct_clamped_to_100(monkeypatch):
    monkeypatch.setenv("FLOP_PACE_JITTER_PCT", "250")
    assert fp.jitter_pct() == Decimal(100)


def test_pacing_status_ignores_jitter(pfile, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    monkeypatch.setenv("FLOP_PACE_JITTER_PCT", "50")
    # telemetry phải ổn định (jitter vô hiệu bằng rng=0.5 nội bộ).
    assert fp.pacing_status(now=NOON, path=pfile)["next_due"] == "12"
