"""
Test cho token_manager.py — chạy: python -m pytest test_token_manager.py -q

Bao phủ: công tắc mode, số học Decimal, credit, spend (simulation / insufficient /
testnet-unconfigured / testnet-onchain / error_submit), đọc file hỏng, và
meter_inference gating.
"""

import json
import pytest

from decimal import Decimal

import token_manager as tm


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Mỗi test khởi đầu ở simulation, không dính env FLOP_/TESTNET_ của máy chạy."""
    for k in ("TESTNET_ENABLED", "FLOP_RPC_URL", "FLOP_SUBMIT_URL", "FLOP_TX_MODE",
              "FLOP_TOKEN_SYMBOL", "TOKEN_LEDGER_FILE", "FLOP_METER_ENABLED",
              "FLOP_INFERENCE_COST", "FLOP_MAX_SPENDS_PER_HOUR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FLOP_ORGANIC_ONLY", "off")       # tắt tường minh; test riêng bật lại


@pytest.fixture
def ledger(tmp_path):
    return str(tmp_path / "ledger.json")


# --- mode + config ---------------------------------------------------------------

def test_mode_defaults_to_simulation(monkeypatch):
    assert tm.ledger_mode() == "simulation"
    assert tm.testnet_enabled() is False
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    assert tm.ledger_mode() == "testnet"
    monkeypatch.setenv("TESTNET_ENABLED", "yes")
    assert tm.ledger_mode() == "testnet"
    monkeypatch.setenv("TESTNET_ENABLED", "off")
    assert tm.ledger_mode() == "simulation"


# --- số học Decimal --------------------------------------------------------------

def test_decimal_helpers():
    assert tm._fmt(tm._parse_amount("0.0010")) == "0.001"      # bỏ số 0 thừa
    assert tm._parse_amount("nope") is None
    assert tm._parse_amount("") is None
    # cộng/trừ không trôi float
    assert tm._fmt(tm._parse_amount("0.1") + tm._parse_amount("0.2")) == "0.3"


# --- credit ----------------------------------------------------------------------

def test_credit_accumulates(ledger):
    assert tm.credit("100", path=ledger)["balance_after"] == "100"
    assert tm.credit("0.5", path=ledger)["balance_after"] == "100.5"
    assert tm.check_balance(path=ledger) == "100.5"


def test_credit_rejects_bad_amount(ledger):
    assert tm.credit("0", path=ledger)["ok"] is False
    assert tm.credit("junk", path=ledger)["ok"] is False


# --- spend: simulation -----------------------------------------------------------

def test_spend_simulation_logs_and_debits(ledger):
    tm.credit("100", path=ledger)
    lines = []
    r = tm.spend("0.001", "Gemini Inference", path=ledger, log=lines.append)
    assert r["outcome"] == "spent_simulated"
    assert r["balance_after"] == "99.999"
    assert "[SIMULATION] Spent 0.001 MOCK_FLOP for Gemini Inference" in lines
    assert tm.check_balance(path=ledger) == "99.999"


def test_spend_insufficient(ledger):
    tm.credit("0.0005", path=ledger)
    lines = []
    r = tm.spend("0.001", "x", path=ledger, log=lines.append)
    assert r["outcome"] == "skipped_insufficient"
    assert lines == []                              # không log, không chi
    assert tm.check_balance(path=ledger) == "0.0005"


def test_spend_rejects_bad_amount(ledger):
    tm.credit("100", path=ledger)
    assert tm.spend("0", "x", path=ledger)["outcome"] == "skipped_insufficient"
    assert tm.spend("-1", "x", path=ledger)["outcome"] == "skipped_insufficient"
    assert tm.spend("junk", "x", path=ledger)["outcome"] == "skipped_insufficient"


# --- spend: testnet --------------------------------------------------------------

def test_testnet_refuses_without_rpc(ledger, monkeypatch):
    tm.credit("100", path=ledger)                   # nạp khi còn simulation
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    r = tm.spend("1", "x", path=ledger, submit_tx=lambda tx: {"tx_hash": "0xNO"})
    assert r["outcome"] == "skipped_unconfigured"
    assert "FLOP_RPC_URL" in r["reason"]
    assert tm.check_balance(path=ledger) == "100"   # số dư nguyên vẹn


def test_testnet_refuses_when_tx_mode_off(ledger, monkeypatch):
    # Có endpoint nhưng FLOP_TX_MODE=off -> KHÔNG tự nối adapter -> vẫn từ chối gửi.
    tm.credit("100", path=ledger)
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_RPC_URL", "https://rpc.test")
    monkeypatch.setenv("FLOP_TX_MODE", "off")
    r = tm.spend("1", "x", path=ledger)
    assert r["outcome"] == "skipped_unconfigured"
    assert "submit_tx" in r["reason"]
    assert tm.check_balance(path=ledger) == "100"


def test_testnet_submits_and_debits(ledger, monkeypatch):
    tm.credit("100", path=ledger)
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_RPC_URL", "https://rpc.test")
    seen = []
    r = tm.spend("0.001", "Gemini Inference", path=ledger,
                 submit_tx=lambda tx: seen.append(tx) or {"tx_hash": "0xdeadbeefcafe"})
    assert r["outcome"] == "spent_onchain"
    assert r["tx_hash"] == "0xdeadbeefcafe"
    assert r["balance_after"] == "99.999"
    assert seen and seen[0]["amount"] == "0.001" and seen[0]["rpc_url"] == "https://rpc.test"


def test_testnet_submit_error_does_not_debit(ledger, monkeypatch):
    tm.credit("100", path=ledger)
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_RPC_URL", "https://rpc.test")

    def boom(tx):
        raise RuntimeError("rpc down")

    r = tm.spend("1", "x", path=ledger, submit_tx=boom)
    assert r["outcome"] == "error_submit"
    assert "rpc down" in r["reason"]
    assert tm.check_balance(path=ledger) == "100"   # không trừ khi gửi lỗi


# --- store loading ---------------------------------------------------------------

def test_load_missing_and_corrupt(ledger, tmp_path):
    assert tm.load_ledger(ledger) == {"balances": {}, "entries": []}
    assert tm.check_balance(path=ledger) == "0"
    bad = str(tmp_path / "bad.json")
    with open(bad, "w") as f:
        json.dump([1, 2, 3], f)                      # cấu trúc sai -> coi như rỗng
    assert tm.load_ledger(bad) == {"balances": {}, "entries": []}


# --- meter_inference (gated) -----------------------------------------------------

def test_meter_inference_off_by_default(ledger):
    assert tm.meter_inference(path=ledger)["outcome"] == "skipped_off"


def test_meter_inference_on(ledger, monkeypatch):
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    r = tm.meter_inference(path=ledger)
    assert r["outcome"] == "spent_simulated"
    assert r["amount"] == "0.001"                    # mặc định FLOP_INFERENCE_COST


# --- Bất biến organic-only (FLOP_ORGANIC_ONLY) chống burn-loop tổng hợp -----------

def test_meter_organic_only_blocks_without_event(ledger, monkeypatch):
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    monkeypatch.setenv("FLOP_ORGANIC_ONLY", "true")
    r = tm.meter_inference(path=ledger)              # không event_id -> từ chối
    assert r["outcome"] == "skipped_synthetic"
    assert tm.check_balance(path=ledger) == "1"      # không hề chi


def test_meter_organic_only_blank_event_blocked(ledger, monkeypatch):
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    monkeypatch.setenv("FLOP_ORGANIC_ONLY", "true")
    assert tm.meter_inference(path=ledger, event_id="   ")["outcome"] == "skipped_synthetic"


def test_meter_organic_only_allows_with_event(ledger, monkeypatch):
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    monkeypatch.setenv("FLOP_ORGANIC_ONLY", "true")
    r = tm.meter_inference(path=ledger, event_id="alice")
    assert r["outcome"] == "spent_simulated"


def test_meter_event_id_not_required_when_flag_off(ledger, monkeypatch):
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    # ORGANIC_ONLY tắt -> event_id không bắt buộc (hành vi cũ giữ nguyên).
    assert tm.meter_inference(path=ledger)["outcome"] == "spent_simulated"


# --- Envelope tần suất (FLOP_MAX_SPENDS_PER_HOUR) ---------------------------------

def test_rate_cap_blocks_after_limit(ledger, monkeypatch):
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    monkeypatch.setenv("FLOP_MAX_SPENDS_PER_HOUR", "2")
    # 2 lần chi đầu OK; lần 3 trong cùng giờ -> chặn trước khi chi.
    assert tm.meter_inference(path=ledger)["outcome"] == "spent_simulated"
    assert tm.meter_inference(path=ledger)["outcome"] == "spent_simulated"
    r = tm.meter_inference(path=ledger)
    assert r["outcome"] == "skipped_rate_cap"
    assert tm.check_balance(path=ledger) == "0.998"  # đúng 2 lần *0.001, lần 3 không trừ


# --- spend_stats (audit chống sybil) ---------------------------------------------

def test_spend_stats_counts_spends_only(ledger, monkeypatch):
    tm.credit("1", path=ledger)                      # credit KHÔNG tính là spend
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    tm.meter_inference(path=ledger)
    tm.meter_inference(path=ledger)
    st = tm.spend_stats(path=ledger)
    assert st["spend_count_total"] == 2
    assert st["spend_count_24h"] == 2


def test_spend_stats_24h_window(ledger):
    import time as _t
    now = 1_700_000_000
    old_ts = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(now - 200_000))  # ~55h trước
    new_ts = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(now - 100))      # trong 24h
    state = {"balances": {"FLOP": "0"}, "entries": [
        {"kind": "spend", "token": "FLOP", "amount": "0.001", "ts": old_ts},
        {"kind": "spend", "token": "FLOP", "amount": "0.001", "ts": new_ts},
        {"kind": "credit", "token": "FLOP", "amount": "1", "ts": new_ts},
    ]}
    with open(ledger, "w") as f:
        json.dump(state, f)
    st = tm.spend_stats(now=now, path=ledger)
    assert st["spend_count_total"] == 2              # cả 2 spend, bỏ credit
    assert st["spend_count_24h"] == 1                # chỉ entry trong 24h


# --- hạt giống MÔ PHỎNG (chỉ simulation) ---------------------------------------------

def test_sim_credit_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("FLOP_SIM_CREDIT", raising=False)
    r = tm.maybe_sim_credit(path=str(tmp_path / "l.json"))
    assert r["outcome"] == "skipped_disabled"


def test_sim_credit_refuses_on_testnet(tmp_path, monkeypatch):
    """Chốt quan trọng nhất: hạt giống mô phỏng KHÔNG BAO GIỜ chạm sổ cái testnet thật."""
    monkeypatch.setenv("FLOP_SIM_CREDIT", "100")
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    p = str(tmp_path / "l.json")
    assert tm.maybe_sim_credit(path=p)["outcome"] == "skipped_testnet"
    assert tm.check_balance(path=p) == "0"          # không nạp gì cả


def test_sim_credit_seeds_once_then_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOP_SIM_CREDIT", "100")
    monkeypatch.delenv("TESTNET_ENABLED", raising=False)
    p = str(tmp_path / "l.json")
    assert tm.maybe_sim_credit(path=p, log=lambda *a: None)["outcome"] == "credited"
    assert tm.check_balance(path=p) == "100"
    r2 = tm.maybe_sim_credit(path=p, log=lambda *a: None)       # đã có tiền -> không chồng
    assert r2["outcome"] == "skipped_has_balance"
    assert tm.check_balance(path=p) == "100"


def test_sim_credit_rejects_bad_amount(tmp_path, monkeypatch):
    monkeypatch.delenv("TESTNET_ENABLED", raising=False)
    p = str(tmp_path / "l.json")
    for bad in ("khong-phai-so", "-5", "0", "NaN"):
        monkeypatch.setenv("FLOP_SIM_CREDIT", bad)
        assert tm.maybe_sim_credit(path=p)["outcome"] == "skipped_bad_amount"
    assert tm.check_balance(path=p) == "0"


def test_sim_credit_memo_says_it_is_simulated(tmp_path, monkeypatch):
    """Sổ cái được publish CÔNG KHAI -> dòng credit phải tự nói nó là mô phỏng."""
    monkeypatch.setenv("FLOP_SIM_CREDIT", "50")
    monkeypatch.delenv("TESTNET_ENABLED", raising=False)
    p = str(tmp_path / "l.json")
    tm.maybe_sim_credit(path=p, log=lambda *a: None)
    memo = tm.load_ledger(p)["entries"][0]["memo"].lower()
    assert "simulation" in memo and "faucet that" in memo


def test_metering_works_after_seed(tmp_path, monkeypatch):
    """Chuỗi đầy đủ: hạt giống -> metering hết skipped_insufficient."""
    monkeypatch.setenv("FLOP_SIM_CREDIT", "10")
    monkeypatch.setenv("FLOP_METER_ENABLED", "on")
    monkeypatch.delenv("TESTNET_ENABLED", raising=False)
    p = str(tmp_path / "l.json")
    assert tm.meter_inference(memo="x", event_id="k1", path=p)["outcome"] == "skipped_insufficient"
    tm.maybe_sim_credit(path=p, log=lambda *a: None)
    assert tm.meter_inference(memo="x", event_id="k1", path=p)["outcome"] == "spent_simulated"


# --- available_today: nền của FLOP_DAILY_BUDGET=auto ----------------------------------

def test_available_today_counts_balance_plus_todays_spend(tmp_path):
    p = str(tmp_path / "l.json")
    tm.credit("100", memo="seed", path=p)
    assert tm.available_today(path=p) == "100"
    tm.spend("40", memo="x", path=p)
    assert tm.check_balance(path=p) == "60"
    assert tm.available_today(path=p) == "100"          # 60 còn + 40 đã chi hôm nay


def test_available_today_ignores_yesterdays_spend(tmp_path):
    """Chi của hôm qua KHÔNG được cộng vào ngân sách hôm nay."""
    import time as _t
    p = str(tmp_path / "l.json")
    tm.credit("100", memo="seed", path=p)
    tm.spend("40", memo="x", path=p)
    now = int(_t.time())
    tomorrow = now - (now % 86400) + 86400 + 3600       # 01:00 UTC ngày mai
    assert tm.available_today(now=tomorrow, path=p) == "60"    # chỉ còn số dư


def test_available_today_empty_wallet_is_zero(tmp_path):
    assert tm.available_today(path=str(tmp_path / "trong.json")) == "0"


def test_available_today_skips_entries_without_timestamp(tmp_path):
    p = str(tmp_path / "l.json")
    tm.credit("10", memo="seed", path=p)
    led = tm.load_ledger(p)
    led["entries"].append({"kind": "spend", "token": "FLOP", "amount": "5"})   # thiếu ts
    tm.save_ledger(led, p)
    assert tm.available_today(path=p) == "10"           # không đoán thời điểm


# --- spent_cumulative: mốc CHỈ TĂNG để hydrate không tua ngược sổ cái -----------------

def test_cumulative_accrues_on_simulation_spend(tmp_path):
    p = str(tmp_path / "l.json")
    tm.credit("100", memo="seed", path=p)
    assert tm.cumulative_spent(tm.load_ledger(p)) == Decimal(0)
    tm.spend("3", memo="x", path=p)
    tm.spend("2", memo="x", path=p)
    assert tm.cumulative_spent(tm.load_ledger(p)) == Decimal("5")
    assert tm.check_balance(path=p) == "95"


def test_cumulative_only_grows_while_balance_falls(tmp_path):
    """Đây CHÍNH LÀ lý do cần nó: số dư đi xuống nên không dùng làm thước tiến độ được."""
    p = str(tmp_path / "l.json")
    tm.credit("100", memo="seed", path=p)
    prev_bal, prev_cum = Decimal("100"), Decimal(0)
    for _ in range(5):
        tm.spend("1", memo="x", path=p)
        bal = Decimal(tm.check_balance(path=p))
        cum = tm.cumulative_spent(tm.load_ledger(p))
        assert bal < prev_bal and cum > prev_cum
        prev_bal, prev_cum = bal, cum


def test_cumulative_survives_entry_log_trimming(tmp_path):
    """Cắt log KHÔNG được làm mốc tụt — nó là tổng chạy, không suy từ entries."""
    p = str(tmp_path / "l.json")
    tm.credit("100", memo="seed", path=p)
    for _ in range(4):
        tm.spend("1", memo="x", path=p)
    led = tm.load_ledger(p)
    led["entries"] = []                              # giả lập publish đã cắt hết log
    tm.save_ledger(led, p)
    assert tm.cumulative_spent(tm.load_ledger(p)) == Decimal("4")


def test_cumulative_tolerates_garbage():
    assert tm.cumulative_spent({"spent_cumulative": "rác"}) == Decimal(0)
    assert tm.cumulative_spent({"spent_cumulative": {"FLOP": None}}) == Decimal(0)
    assert tm.cumulative_spent({}) == Decimal(0) and tm.cumulative_spent(None) == Decimal(0)


def test_spent_today_amount_only_counts_today(tmp_path):
    import time as _t
    p = str(tmp_path / "l.json")
    tm.credit("100", memo="seed", path=p)
    tm.spend("7", memo="x", path=p)
    assert tm.spent_today_amount(path=p) == Decimal("7")
    now = int(_t.time())
    tomorrow = now - (now % 86400) + 86400 + 60
    assert tm.spent_today_amount(now=tomorrow, path=p) == Decimal("0")


def test_spent_today_amount_skips_entries_without_timestamp(tmp_path):
    p = str(tmp_path / "l.json")
    tm.credit("100", memo="seed", path=p)
    led = tm.load_ledger(p)
    led["entries"].append({"kind": "spend", "token": "FLOP", "amount": "5"})
    tm.save_ledger(led, p)
    assert tm.spent_today_amount(path=p) == Decimal("0")


def test_spent_by_day_is_a_running_total_not_derived_from_entries(tmp_path):
    """Nguồn đúng khi bản KV bị cắt log: tổng chạy theo ngày."""
    p = str(tmp_path / "l.json")
    tm.credit("100", memo="seed", path=p)
    tm.spend("3", memo="x", path=p)
    tm.spend("4", memo="x", path=p)
    assert tm.spent_today_amount(path=p) == Decimal("7")
    led = tm.load_ledger(p)
    led["entries"] = []                                  # giả lập bản đã cắt sạch log
    tm.save_ledger(led, p)
    assert tm.spent_today_amount(path=p) == Decimal("7")   # vẫn đúng


def test_spent_by_day_keeps_only_recent_days(tmp_path):
    p = str(tmp_path / "l.json")
    tm.credit("100", memo="seed", path=p)
    led = tm.load_ledger(p)
    led["spent_by_day"] = {"FLOP": {f"2026-01-{d:02d}": "1" for d in range(1, 21)}}
    tm.save_ledger(led, p)
    tm.spend("1", memo="x", path=p)
    days = tm.load_ledger(p)["spent_by_day"]["FLOP"]
    assert len(days) <= tm.DAY_KEEP + 1                  # cắt bớt, không phình vô hạn


def test_legacy_ledger_without_spent_by_day_still_works(tmp_path):
    """Sổ cái CŨ chưa có spent_by_day -> rơi về cộng từ entries."""
    import time as _t
    p = str(tmp_path / "l.json")
    ts = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())
    tm.save_ledger({"balances": {"FLOP": "9"}, "entries": [
        {"kind": "spend", "token": "FLOP", "amount": "2", "ts": ts}]}, p)
    assert tm.spent_today_amount(path=p) == Decimal("2")


def test_first_spend_day_is_set_once_and_never_moves(tmp_path):
    """Cần nó để biết 'đang ở ngày thứ mấy' — spent_by_day chỉ giữ 7 ngày."""
    import time as _t
    p = str(tmp_path / "l.json")
    tm.credit("100", memo="seed", path=p)
    tm.spend("1", memo="x", path=p)
    first = tm.load_ledger(p)["first_spend_day"]
    assert first == _t.strftime("%Y-%m-%d", _t.gmtime())
    tm.spend("1", memo="x", path=p)
    assert tm.load_ledger(p)["first_spend_day"] == first      # không đổi


def test_first_spend_day_repairs_a_null_from_an_old_publish(tmp_path):
    """Bản publish trước #137 ghi `first_spend_day: null`. setdefault KHÔNG sửa được khoá
    đã tồn tại, nên None nằm lại vĩnh viễn — production đúng như vậy."""
    p = str(tmp_path / "l.json")
    tm.save_ledger({"balances": {"FLOP": "100"}, "entries": [], "first_spend_day": None,
                    "spent_by_day": {"FLOP": {"2026-10-17": "90.9"}}}, p)
    tm.spend("1", memo="x", path=p)
    assert tm.load_ledger(p)["first_spend_day"] == "2026-10-17"   # lấy ngày SỚM NHẤT đã biết


def test_first_spend_day_defaults_to_today_when_nothing_known(tmp_path):
    import time as _t
    p = str(tmp_path / "l.json")
    tm.credit("100", memo="seed", path=p)
    tm.spend("1", memo="x", path=p)
    assert tm.load_ledger(p)["first_spend_day"] == _t.strftime("%Y-%m-%d", _t.gmtime())


def test_sim_credit_refills_when_only_dust_is_left(tmp_path, monkeypatch):
    """Qua nửa đêm ví còn 0.205 bụi: '> 0' nên KHÔNG bao giờ nạp lại, ngân sách ngày mới teo."""
    monkeypatch.setenv("FLOP_SIM_CREDIT", "100")
    monkeypatch.delenv("TESTNET_ENABLED", raising=False)
    p = str(tmp_path / "l.json")
    tm.save_ledger({"balances": {"FLOP": "0.205"}, "entries": []}, p)
    assert tm.maybe_sim_credit(path=p, log=lambda *a: None)["outcome"] == "credited"
    assert Decimal(tm.check_balance(path=p)) > Decimal("100")


def test_sim_credit_still_refuses_a_funded_wallet(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOP_SIM_CREDIT", "100")
    monkeypatch.delenv("TESTNET_ENABLED", raising=False)
    p = str(tmp_path / "l.json")
    tm.save_ledger({"balances": {"FLOP": "50"}, "entries": []}, p)
    assert tm.maybe_sim_credit(path=p)["outcome"] == "skipped_has_balance"


def test_sim_credit_threshold_is_tunable(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOP_SIM_CREDIT", "100")
    monkeypatch.setenv("FLOP_SIM_CREDIT_BELOW", "40")
    monkeypatch.delenv("TESTNET_ENABLED", raising=False)
    p = str(tmp_path / "l.json")
    tm.save_ledger({"balances": {"FLOP": "30"}, "entries": []}, p)
    assert tm.maybe_sim_credit(path=p, log=lambda *a: None)["outcome"] == "credited"


def test_sim_credit_is_capped_to_once_per_day(tmp_path, monkeypatch):
    """Faucet THẬT có MAX_PER_DAY; hạt giống mô phỏng thì không, nên pacer rút cạn ví rồi
    nạp lại NGAY trong ngày — đo được 2 lần nạp/ngày, tổng chi 115 thay vì 100."""
    monkeypatch.setenv("FLOP_SIM_CREDIT", "100")
    monkeypatch.delenv("TESTNET_ENABLED", raising=False)
    p = str(tmp_path / "l.json")
    tm.save_ledger({"balances": {"FLOP": "0"}, "entries": []}, p)
    assert tm.maybe_sim_credit(path=p, log=lambda *a: None)["outcome"] == "credited"
    tm.spend("100", memo="x", path=p)                      # rút cạn lại trong cùng ngày
    r = tm.maybe_sim_credit(path=p, log=lambda *a: None)
    assert r["outcome"] == "skipped_daily_cap" and "trần 1" in r["reason"]


def test_sim_credit_resets_on_the_next_utc_day(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOP_SIM_CREDIT", "100")
    monkeypatch.delenv("TESTNET_ENABLED", raising=False)
    p = str(tmp_path / "l.json")
    tm.save_ledger({"balances": {"FLOP": "0"}, "entries": []}, p)
    t0 = 1_792_000_000
    assert tm.maybe_sim_credit(path=p, log=lambda *a: None, now=t0)["outcome"] == "credited"
    tm.spend("100", memo="x", path=p)
    assert tm.maybe_sim_credit(path=p, log=lambda *a: None,
                               now=t0)["outcome"] == "skipped_daily_cap"
    assert tm.maybe_sim_credit(path=p, log=lambda *a: None,
                               now=t0 + 86400)["outcome"] == "credited"   # ngày mới


def test_sim_credit_daily_cap_is_tunable(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOP_SIM_CREDIT", "10")
    monkeypatch.setenv("FLOP_SIM_CREDIT_MAX_PER_DAY", "3")
    monkeypatch.delenv("TESTNET_ENABLED", raising=False)
    p = str(tmp_path / "l.json")
    tm.save_ledger({"balances": {"FLOP": "0"}, "entries": []}, p)
    for _ in range(3):
        assert tm.maybe_sim_credit(path=p, log=lambda *a: None)["outcome"] == "credited"
        tm.spend("10", memo="x", path=p)
    assert tm.maybe_sim_credit(path=p, log=lambda *a: None)["outcome"] == "skipped_daily_cap"


def test_session_spend_tracked_separately_from_metering(tmp_path, monkeypatch):
    """Chỉ chi cho phiên đã settle mới vào bộ đếm thước-đo."""
    monkeypatch.setenv("FLOP_METER_ENABLED", "on")
    p = str(tmp_path / "l.json")
    tm.credit("100", memo="seed", path=p)
    tm.spend("0.001", memo="gemini kibble:explain", path=p)
    tm.spend("0.05", memo="inference-session:abc", path=p)
    tm.spend("0.001", memo="gemini tclk-work", path=p)
    tm.spend("0.05", memo="inference-session:def", path=p)
    d = tm.load_ledger(p)
    assert d["spent_cumulative"]["FLOP"] == "0.102"        # gộp cả hai
    assert d["session_cumulative"]["FLOP"] == "0.1"        # CHỈ phiên
    assert d["session_count_cumulative"]["FLOP"] == 2
    assert list(d["session_by_day"]["FLOP"].values()) == ["0.1"]


def test_is_session_spend_matches_only_the_session_memo():
    assert tm.is_session_spend("inference-session:abc") is True
    for other in ("gemini kibble:explain", "gemini tclk-work", "faucet", "", None):
        assert tm.is_session_spend(other) is False


# --- Mặc định BẬT: fork không đặt Variable vẫn được bảo vệ -------------------------
def test_organic_only_on_by_default(ledger, monkeypatch):
    monkeypatch.delenv("FLOP_ORGANIC_ONLY", raising=False)   # vắng biến = BẬT
    assert tm.organic_only() is True
    tm.credit("1", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    assert tm.meter_inference(path=ledger)["outcome"] == "skipped_synthetic"


def test_organic_only_explicit_off(monkeypatch):
    for v in ("0", "false", "off", "no", "OFF"):
        monkeypatch.setenv("FLOP_ORGANIC_ONLY", v)
        assert tm.organic_only() is False



def test_meter_never_spends_onchain_on_testnet(ledger, monkeypatch):
    # Metering là proxy mô phỏng. Ở testnet spend() là giao dịch THẬT: memo "gemini kibble"
    # đốt FLOP thật không được tính, và giành ngân sách của phiên.
    tm.credit("50", path=ledger)
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_RPC_URL", "http://rpc.invalid")
    sent = []
    r = tm.meter_inference(event_id="job1", path=ledger,
                           submit_tx=lambda tx: sent.append(tx) or {"tx_hash": "0x1"})
    assert r["outcome"] == "skipped_testnet"
    assert sent == [] and tm.check_balance(path=ledger) == "50"


def test_spend_counts_survive_kv_entry_trim(ledger, monkeypatch):
    # Bản đẩy KV chỉ giữ 20 entries: đếm 24h/tổng phải lấy từ tổng chạy, không hụt về 20.
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    tm.credit("1", path=ledger)
    for _ in range(30):
        tm.meter_inference(path=ledger, event_id="e")
    led = tm.load_ledger(ledger)
    led["entries"] = led["entries"][-20:]
    with open(ledger, "w") as f:
        json.dump(led, f)
    st = tm.spend_stats(path=ledger)
    assert st["spend_count_total"] == 30
    assert st["spend_count_24h"] == 30


def test_rate_cap_holds_above_20_after_trim(ledger, monkeypatch):
    monkeypatch.setenv("FLOP_METER_ENABLED", "true")
    monkeypatch.setenv("FLOP_MAX_SPENDS_PER_HOUR", "25")
    tm.credit("1", path=ledger)
    for _ in range(25):
        assert tm.meter_inference(path=ledger, event_id="e")["outcome"] == "spent_simulated"
    led = tm.load_ledger(ledger)
    led["entries"] = led["entries"][-20:]               # mô phỏng hydrate từ KV
    with open(ledger, "w") as f:
        json.dump(led, f)
    assert tm.meter_inference(path=ledger, event_id="e")["outcome"] == "skipped_rate_cap"


def test_hour_buckets_are_bounded(ledger, monkeypatch):
    st = {}
    t0 = 1_800_000_000
    for i in range(40):
        monkeypatch.setattr(tm.time, "time", lambda i=i: t0 + i * 3600)
        tm._accrue_cumulative(st, "FLOP", tm.Decimal("0.001"))
    assert len(st["spend_count_by_hour"]["FLOP"]) == tm.HOUR_KEEP


def test_testnet_spend_without_tx_hash_is_not_booked(ledger, monkeypatch):
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_RPC_URL", "https://rpc.test")
    tm.credit("5", path=ledger)
    r = tm.spend("1", "inference-session:x", path=ledger, submit_tx=lambda tx: {"status": "ok"})
    assert r["outcome"] == "error_submit" and "tx hash" in r["reason"]
    assert tm.check_balance(path=ledger) == "5"
    assert tm.load_ledger(ledger).get("session_cumulative", {}) == {}



def test_next_nonce_strictly_increases_within_one_millisecond(monkeypatch):
    monkeypatch.setattr(tm.time, "time", lambda: 1_800_000_000.0)
    seq = [int(tm.next_nonce()) for _ in range(5)]
    assert seq == sorted(seq) and len(set(seq)) == 5



def test_credit_hour_buckets_are_recorded(ledger):
    tm.credit("100", path=ledger)
    tm.credit("5", path=ledger)
    buckets = tm.load_ledger(ledger)["credit_by_hour"]["FLOP"]
    assert len(buckets) == 1 and list(buckets.values())[0] == ["105", 2]
