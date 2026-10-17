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


# --- FLOP_DAILY_BUDGET=auto: ngân sách suy từ ví thật --------------------------------

def _seed_ledger(tmp_path, balance, spends=()):
    """Dựng một sổ cái thật qua token_manager (không chế JSON bằng tay)."""
    import token_manager as tm
    led = str(tmp_path / "led.json")
    tm.credit(balance, memo="seed", path=led)
    for amt, ts_off in spends:
        tm.spend(amt, memo="x", path=led)
    return led


def test_auto_budget_is_balance_plus_spent_today(tmp_path, monkeypatch):
    """Ngân sách phải ĐỨNG YÊN khi chi: nếu chỉ lấy số dư thì mục tiêu tụt theo mỗi lần
    chi và ví không bao giờ tiêu hết."""
    import token_manager as tm
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "auto")
    led = _seed_ledger(tmp_path, "100")
    assert fp.daily_budget(ledger_path=led) == Decimal("100")
    tm.spend("30", memo="x", path=led)
    assert tm.check_balance(path=led) == "70"
    assert fp.daily_budget(ledger_path=led) == Decimal("100")      # 70 còn + 30 đã chi


def test_auto_budget_grows_when_faucet_credits(tmp_path, monkeypatch):
    import token_manager as tm
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "auto")
    led = _seed_ledger(tmp_path, "100")
    tm.credit("50", memo="faucet", path=led)
    assert fp.daily_budget(ledger_path=led) == Decimal("150")


def test_auto_budget_off_when_wallet_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "auto")
    assert fp.daily_budget(ledger_path=str(tmp_path / "trong.json")) is None


def test_auto_budget_off_when_ledger_unreadable(monkeypatch):
    """Không đọc được ví -> pacer TẮT, không đoán một con số."""
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "auto")
    import token_manager as tm

    def _boom(*a, **k):
        raise RuntimeError("sổ cái hỏng")

    monkeypatch.setattr(tm, "available_today", _boom)
    assert fp.daily_budget() is None


def test_fixed_budget_still_works_and_ignores_wallet(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    led = _seed_ledger(tmp_path, "999")
    assert fp.daily_budget(ledger_path=led) == Decimal("24")


def test_auto_budget_drives_next_spend_amount(tmp_path, monkeypatch):
    """Đầu-cuối: ví 24 FLOP, giữa trưa -> nên chi đúng một nửa."""
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "auto")
    led = _seed_ledger(tmp_path, "24")
    pfile = str(tmp_path / "pace.json")
    assert fp.next_spend_amount(now=NOON, path=pfile, ledger_path=led) == "12"


def test_pacing_status_reports_auto_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "auto")
    led = _seed_ledger(tmp_path, "48")
    st = fp.pacing_status(now=NOON, path=str(tmp_path / "p.json"), ledger_path=led)
    assert st["enabled"] is True and st["daily_budget"] == "48" and st["target_now"] == "24"
