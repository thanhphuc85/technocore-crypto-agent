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
              "FLOP_PACER_FILE", "FLOP_TOKEN_SYMBOL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FLOP_PACE_JITTER_PCT", "0")      # tắt tường minh: test cần tất định


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


# --- pacer MẤT TRÍ NHỚ: flop_pacer.json không được cache qua run --------------------

def _ledger_with_today(tmp_path, spends):
    """Sổ cái thật có `spends` lần chi trong NGÀY HÔM NAY."""
    import token_manager as tm
    led = str(tmp_path / "led.json")
    tm.credit("1000", memo="seed", path=led)
    for amt in spends:
        tm.spend(amt, memo="x", path=led)
    return led


def test_pacer_throttles_even_when_its_own_file_is_lost(tmp_path, monkeypatch):
    """LỖI THẬT: workflow chỉ cache state.json, nên flop_pacer.json rỗng MỖI run ->
    pacer tưởng hôm nay chưa chi gì -> chi kịch trần mãi mãi. Sổ cái phải cứu."""
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    led = _ledger_with_today(tmp_path, ["12"])          # đã chi đúng mục tiêu giữa trưa
    fresh = str(tmp_path / "chua-ton-tai.json")          # file pacer rỗng, như trên runner
    assert fp.next_spend_amount(now=NOON, path=fresh, ledger_path=led) == "0"


def test_pacer_still_spends_when_ledger_shows_a_deficit(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    monkeypatch.setenv("FLOP_MAX_PER_RUN", "0.5")
    led = _ledger_with_today(tmp_path, ["2"])            # mới chi 2, mục tiêu 12
    out = fp.next_spend_amount(now=NOON, path=str(tmp_path / "x.json"), ledger_path=led)
    assert out == "0.5"                                   # còn thiếu -> chi kịch trần


def test_catches_up_after_a_gap(tmp_path, monkeypatch):
    """Đúng kịch bản được hỏi: nhiều run lỗi -> run sau phải chi bù, không bỏ sót."""
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    led = _ledger_with_today(tmp_path, ["2"])            # 18h mà mới chi 2 (mục tiêu 18)
    six_pm = MIDNIGHT + 18 * 3600
    due = fp.next_spend_amount(now=six_pm, path=str(tmp_path / "x.json"), ledger_path=led)
    assert Decimal(due) == Decimal("16")                 # 18 - 2, không kẹp trần


def test_falls_back_to_pacer_record_when_ledger_unreadable(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    import token_manager as tm

    def _boom(*a, **k):
        raise RuntimeError("sổ cái hỏng")

    monkeypatch.setattr(tm, "spent_today_amount", _boom)
    pf = str(tmp_path / "p.json")
    fp.record_spend("12", now=NOON, path=pf)
    assert fp.next_spend_amount(now=NOON, path=pf) == "0"   # vẫn dùng bản ghi của pacer


def test_takes_the_larger_of_the_two_sources(tmp_path, monkeypatch):
    """Lấy MAX: bản ghi pacer (vd trên VPS có đĩa bền) không được bị sổ cái nuốt."""
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "24")
    led = _ledger_with_today(tmp_path, ["1"])
    pf = str(tmp_path / "p.json")
    fp.record_spend("12", now=NOON, path=pf)             # pacer nhớ nhiều hơn sổ cái
    assert fp.next_spend_amount(now=NOON, path=pf, ledger_path=led) == "0"


# --- trần MỖI LẦN CHI theo PHẦN TRĂM: co giãn theo ngân sách --------------------------

def test_absolute_cap_unchanged(monkeypatch):
    monkeypatch.setenv("FLOP_MAX_PER_RUN", "0.5")
    assert fp.max_per_run(Decimal("100")) == Decimal("0.5")
    assert fp.max_per_run(Decimal("100000")) == Decimal("0.5")     # KHÔNG co giãn (cố ý)


def test_percent_cap_scales_with_budget():
    """Lỗi THẬT: trần tuyệt đối 0.5 với ngân sách 100k chỉ cho tiêu 0,576%/ngày."""
    import os
    os.environ["FLOP_MAX_PER_RUN"] = "2%"
    try:
        assert fp.max_per_run(Decimal("100")) == Decimal("2")
        assert fp.max_per_run(Decimal("100000")) == Decimal("2000")
    finally:
        os.environ.pop("FLOP_MAX_PER_RUN", None)


def test_percent_cap_needs_a_budget(monkeypatch):
    monkeypatch.setenv("FLOP_MAX_PER_RUN", "2%")
    assert fp.max_per_run(None) is None
    assert fp.max_per_run(Decimal("0")) is None


def test_unset_means_no_cap_contract_unchanged(monkeypatch):
    """Giữ NGUYÊN hợp đồng cũ: rỗng = không trần. (Vận hành thì nên luôn đặt một trần —
    không trần nghĩa là sau sự cố dài phần thiếu có thể bị chi hết trong MỘT lần.)"""
    monkeypatch.delenv("FLOP_MAX_PER_RUN", raising=False)
    assert fp.max_per_run(Decimal("100000")) is None
    assert fp.max_per_run(None) is None


def test_bad_or_zero_values_mean_no_cap(monkeypatch):
    for bad in ("rác", "-5", "0", "0%", "-2%", "%"):
        monkeypatch.setenv("FLOP_MAX_PER_RUN", bad)
        assert fp.max_per_run(Decimal("100")) is None


def test_percent_cap_bounds_a_real_spend(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOP_DAILY_BUDGET", "100000")
    monkeypatch.setenv("FLOP_MAX_PER_RUN", "2%")
    due = fp.next_spend_amount(now=NOON, path=str(tmp_path / "p.json"))
    assert Decimal(due) == Decimal("2000")        # đến hạn 50.000 nhưng kẹp 2% = 2.000


def test_jitter_on_by_default_and_explicit_zero_disables(monkeypatch):
    monkeypatch.delenv("FLOP_PACE_JITTER_PCT", raising=False)  # vắng biến = 20%
    assert fp.jitter_pct() == Decimal(20)
    monkeypatch.setenv("FLOP_PACE_JITTER_PCT", "0")
    assert fp.jitter_pct() == Decimal(0)



def test_explicit_budget_works_without_daily_budget_env(pfile, monkeypatch):
    # Phiên dùng pacer qua tham số `budget`, KHÔNG bật FLOP_DAILY_BUDGET (biến đó còn
    # điều khiển giá metering — bật nó từng làm 1 lần suy luận bị tính 2 FLOP).
    monkeypatch.delenv("FLOP_DAILY_BUDGET", raising=False)
    noon = calendar.timegm((2026, 11, 3, 12, 0, 0))
    assert fp.next_spend_amount(now=noon, path=pfile) is None     # không budget -> tắt
    assert fp.next_spend_amount(now=noon, path=pfile, budget="100") == "50"
    for bad in ("0", "-5", "abc"):
        assert fp.next_spend_amount(now=noon, path=pfile, budget=bad) is None
