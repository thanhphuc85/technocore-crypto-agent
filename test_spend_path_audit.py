"""Hồi quy từ audit đường chi tiêu 2026-10-31: mỗi test ứng với một lỗi đã tái hiện bằng gateway giả."""
import json
import os

import pytest

import agent_cron as ac
import flop_gateway as fg
import flop_radar
import flop_report
import flop_watchdog
import token_manager as tm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

NOW = 1_800_000_000
KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


class R:
    def __init__(self, code=200, body=None, headers=None):
        self.status_code, self._b, self.headers = code, body if body is not None else {}, headers or {}
        self.content = json.dumps(self._b).encode()

    def json(self):
        return self._b

    def raise_for_status(self):
        return None


def ok_chat(usage=None):
    return R(200, {"id": "x", "choices": [{"message": {"content": "a"}}],
                   "usage": {"completion_tokens": 10} if usage is None else usage})


class Srv:
    """Gateway giả tối giản; `chat_fn` quyết định từng lần gọi chat."""

    def __init__(self, net="testnet", quota=1000.0):
        self.net, self.quota, self.receipts, self.chats = net, quota, [], []
        self.models = {"data": [{"id": "m", "pricing": {"prompt": "0.006", "completion": "0.006"}}]}
        self.chat_fn = lambda body, headers: ok_chat()
        self.enroll_ok = True

    def __call__(self, method, url, headers=None, json=None, timeout=20):
        if url.endswith("/health"):
            return R(200, {"network": {"network": self.net}})
        if url.endswith("/keys/me"):
            return R(200, {"remaining_quota": self.quota, "expires_at": (NOW + 80000) * 1000})
        if url.endswith("/keys/me/receipts"):
            return R(200, {"receipts": list(self.receipts)})
        if url.endswith("/models"):
            return R(200, self.models)
        if url.endswith("/models/estimate"):
            return R(200, {"estimated_input_tokens": 20})
        if "/enroll/wallet/challenge" in url and self.enroll_ok:
            return R(200, {"message": f"api.flop.network enroll\n{fg.wallet_address(KEY)}\nnonce"})
        if url.endswith("/enroll/wallet") and self.enroll_ok:
            return R(200, {"key": "newtok", "model": "m", "remaining_quota": self.quota,
                           "expires_at": (NOW + 80000) * 1000})
        if url.endswith("/chat/completions"):
            self.chats.append({"key": (headers or {}).get("Idempotency-Key"),
                               "prompt": json["messages"][0]["content"], "body": json})
            return self.chat_fn(json, headers)
        return R(404, {"error": "not_found"})


def rc(rid, state="settled", spend=6.5, **extra):
    return {"request_id": rid, "state": state, "response_status": 200, "settled_spend": spend,
            "channel_id": "c" + rid, "model": "m", **extra}


def fresh(net="testnet"):
    return {fg.KEY: {"profile": fg.profile_of(net), "vault": fg.seal_bearer(KEY, "tok"), "model": "m",
                     "expires_at": (NOW + 80000) * 1000}}


def book(cost, ref, model):
    return tm.record_gateway_spend(cost, f"{fg.SESSION_MEMO} {model}", ref)


def step(state, srv, now=NOW, net="testnet", **kw):
    return fg.run_step(state, agent_key=KEY, http=srv, now=now, prompts=["p1", "p2", "p3"],
                       expect_network=net, book_fn=book, log=lambda m: None, sleep=lambda s: None,
                       rng=lambda: 0.5, **kw)


def timeout(body, headers):
    raise ac.requests.ReadTimeout("t")


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("FLOP_GATEWAY") or k.startswith("FLOP_ATTRIB"):
            monkeypatch.delenv(k)
    for k, v in {"TESTNET_ENABLED": "true", "FLOP_WALLET_USE_DID": "on", "FLOP_GATEWAY_PROMPTS": "q1|q2|q3",
                 "FLOP_PACE_JITTER_PCT": "0", "FLOP_GATEWAY_SKIP_PCT": "0", "FLOP_GATEWAY_CALL_GAP_SEC": "0",
                 "TOKEN_LEDGER_FILE": str(tmp_path / "ledger.json")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ac, "kv_set", lambda *a, **k: True)
    monkeypatch.setattr(ac, "build_market_context", lambda *a, **k: "")
    monkeypatch.setattr(ac, "RADAR_EXPLORER", "")


@pytest.fixture
def telegram(monkeypatch):
    sent = []
    monkeypatch.setattr(ac, "_telegram_send", lambda m: sent.append(m) or True)
    return sent


def pin_now(monkeypatch, now):
    """gateway_session_step truyền `now` của nó; ghim lại để biên nhận/hạn ổn định."""
    real = fg.run_step
    monkeypatch.setattr(fg, "run_step", lambda state, **k: real(state, **{**k, "now": now}))


# --- lưu trữ kết quả qua nhánh lỗi -----------------------------------------------------

def test_failed_run_keeps_receipt_results_for_alerts_miners_and_persistence(monkeypatch, telegram):
    st = fresh()
    srv = Srv()
    srv.receipts = [rc("r1")]
    srv.chat_fn = lambda b, h: R(429, {"error": "busy"}, {"Retry-After": "60"})
    monkeypatch.setattr(ac, "_gateway_http", srv)
    monkeypatch.setattr(ac, "channel_info", lambda n, c: {"miner": "5Miner", "agent": "5BROKER"})
    saved = []
    monkeypatch.setattr(ac, "persist_ledger_to_kv", lambda *a: saved.append(1))
    monkeypatch.setattr(ac, "_miner_stats", {})
    pin_now(monkeypatch, NOW)
    _, settled, last = ac.gateway_session_step(st, KEY, NOW)
    assert settled == 1 and last["outcome"] == "gateway_settled_unverified"
    assert saved and any("5BROKER"[:8] in m for m in telegram) and ac._miner_stats
    assert st[ac.FOREIGN_SEEN_KEY] == NOW


def test_outage_exit_still_saves_the_ledger_copy(monkeypatch):
    src = open("agent_cron.py", encoding="utf-8").read()
    i = src.index("OUTAGE toàn phần -> exit 1")
    assert "persist_ledger_to_kv" in src[i:src.index("sys.exit(1)", i)]


# --- request treo ----------------------------------------------------------------------

def test_inflight_restored_from_kv_is_reconciled_not_dropped(monkeypatch, telegram):
    st = {"gateway_inflight": {"inflight": {"key": "HUNG", "ts": NOW - 60, "max_cost": 7}, "at": NOW}}
    srv = Srv()
    monkeypatch.setattr(ac, "_gateway_http", srv)
    pin_now(monkeypatch, NOW)
    ac.gateway_session_step(st, KEY, NOW)
    assert st[fg.KEY]["last_outcome"] == "reconciling" and srv.chats == []


def test_inflight_from_another_network_is_still_dropped(monkeypatch, telegram):
    st = {"gateway_inflight": {"inflight": {"key": "OLD", "ts": NOW - 60, "max_cost": 7},
                               "profile": "https://api.flop.network/v1|devnet", "at": NOW}}
    srv = Srv()
    monkeypatch.setattr(ac, "_gateway_http", srv)
    pin_now(monkeypatch, NOW)
    ac.gateway_session_step(st, KEY, NOW)
    assert st[fg.KEY]["last_outcome"] != "reconciling"


def test_hung_job_is_not_sent_again(monkeypatch, telegram):
    st = fresh()
    st["gateway_jobs"] = [{"id": "j1", "task": "JOB-ONE"}, {"id": "j2", "task": "JOB-TWO"}]
    srv = Srv()
    srv.chat_fn = timeout
    monkeypatch.setattr(ac, "_gateway_http", srv)
    real = fg.run_step
    monkeypatch.setattr(fg, "run_step", lambda state, **k: real(state, **{**k, "now": NOW}))
    ac.gateway_session_step(st, KEY, NOW)
    assert [j["id"] for j in st["gateway_jobs"]] == ["j2"]


def test_replay_is_off_by_default(monkeypatch):
    st = fresh()
    srv = Srv()
    srv.chat_fn = timeout
    step(st, srv)
    st[fg.KEY]["wait_until"] = 0
    assert step(st, srv, now=NOW + 900)["outcome"] == "reconciling" and len(srv.chats) == 1


def test_quota_drop_after_replay_stops_further_replays(monkeypatch):
    monkeypatch.setenv("FLOP_GATEWAY_REPLAY_MAX", "5")
    st = fresh()
    srv = Srv()
    srv.chat_fn = timeout
    step(st, srv)

    def charged(body, headers):
        srv.quota -= 6.5
        return R(504, {"error": "gw"})
    srv.chat_fn = charged
    st[fg.KEY]["wait_until"] = 0
    r = step(st, srv, now=NOW + 900)
    assert r["replay_charged"] == pytest.approx(6.5) and st[fg.KEY]["replay_unsafe"]
    st[fg.KEY]["wait_until"] = 0
    step(st, srv, now=NOW + 1000)
    assert len(srv.chats) == 2                        # 1 gốc + 1 phát lại, không thêm


# --- phản hồi lạ -----------------------------------------------------------------------

@pytest.mark.parametrize("usage", [{"cost_flop": "n/a"}, "rác", {"cost_flop": "inf"}])
def test_malformed_usage_does_not_lose_a_paid_call(usage):
    st = fresh()
    srv = Srv()

    def paid(body, headers):
        srv.quota -= 6.5
        return ok_chat(usage)
    srv.chat_fn = paid
    got = []
    r = step(st, srv, answer_fn=lambda p, t, c: got.append(t))
    assert r["outcome"] == "spent" and r["calls"] >= 1 and got and st[fg.KEY]["prompt_idx"] >= 1


def test_receipt_with_only_the_raw_amount_is_booked():
    st = fresh()
    srv = Srv()
    srv.receipts = [{"request_id": "r9", "state": "settled", "settled_spend_raw": "6500000000000000000", "model": "m"}]
    srv.chat_fn = lambda b, h: R(429, {"error": "x"})
    step(st, srv)
    assert float(tm.gateway_attribution()["unattributed"]) == pytest.approx(6.5)


def test_empty_catalog_keeps_the_bearer():
    st = fresh()
    srv = Srv()
    srv.models = {"data": []}
    assert step(st, srv)["outcome"] == "catalog_empty" and "vault" in st[fg.KEY] and st[fg.KEY]["model"] == "m"


def test_receipt_leaving_the_window_unsettled_is_counted():
    st = fresh()
    srv = Srv()
    srv.receipts = [rc("late", state="estimated", spend=0)]
    srv.chat_fn = lambda b, h: R(429, {"error": "x"})
    step(st, srv)
    st[fg.KEY]["wait_until"] = 0
    srv.receipts = [rc(f"n{i}") for i in range(20)]
    step(st, srv, now=NOW + 700)
    assert st[fg.KEY]["stats"]["receipts_lost"] == 1 and st[fg.KEY]["receipts_open"] == {}
    assert "1 biên nhận" in fg.report_line(st[fg.KEY], NOW + 700)


def test_failed_receipt_is_not_counted_as_lost():
    st = fresh()
    srv = Srv()
    srv.receipts = [rc("x", state="estimated", spend=0)]
    srv.chat_fn = lambda b, h: R(429, {"error": "x"})
    step(st, srv)
    st[fg.KEY]["wait_until"] = 0
    srv.receipts = [rc("x", state="released", spend=0), rc("y")]
    step(st, srv, now=NOW + 700)
    assert not st[fg.KEY]["stats"].get("receipts_lost")


# --- trần chi --------------------------------------------------------------------------

def test_run_cap_limits_the_call_budget(monkeypatch):
    monkeypatch.setenv("FLOP_GATEWAY_MAX_PER_RUN", "1")
    st = fresh()
    srv = Srv(quota=500000.0)
    r = step(st, srv)
    assert r["due"] <= 1 and all(c["body"]["max_cost_flop"] <= 1.1 for c in srv.chats)


def test_daily_cap_stops_buying_and_resets_next_day(monkeypatch):
    import time
    st = fresh()
    day = time.strftime("%Y-%m-%d", time.gmtime(NOW))
    st[fg.KEY]["day_spent"] = {"day": day, "spent": fg.DEFAULT_MAX_PER_DAY}
    srv = Srv()
    assert step(st, srv)["outcome"] == "daily_cap" and srv.chats == []
    assert step(st, srv, now=NOW + 86400)["outcome"] != "daily_cap"


def test_daily_cap_counts_spend_and_hung_requests():
    st = fresh()
    srv = Srv()
    srv.chat_fn = lambda b, h: ok_chat({"cost_flop": 2})
    step(st, srv)
    assert st[fg.KEY]["day_spent"]["spent"] >= 2
    st = fresh()
    srv.chat_fn = timeout
    step(st, srv)
    assert st[fg.KEY]["day_spent"]["spent"] > 0


def test_gateway_caps_are_status_neutral_for_the_watchdog():
    for oc in ("daily_cap", "attribution_hold"):
        state = {}
        out = flop_watchdog.observe(state, settled_onchain=0, status=f"0✓ gateway/{oc}", spent_today_onchain=0,
                                    now=NOW, alert_runs=1)
        assert out["alert"] is None


# --- đứng tên ví -----------------------------------------------------------------------

def _pending(amount="30"):
    tm.credit("100")
    tm.record_gateway_spend(amount, "inference-session gateway m", "r-1")


def test_hold_stops_buying_but_keeps_reading_receipts(monkeypatch, telegram):
    _pending()
    st = fresh()
    srv = Srv()
    srv.receipts = [rc("r2")]
    monkeypatch.setattr(ac, "_gateway_http", srv)
    pin_now(monkeypatch, NOW)
    status, settled, _ = ac.gateway_session_step(st, KEY, NOW)
    assert status.endswith("gateway/attribution_hold") and srv.chats == [] and settled == 1
    assert sum("DỪNG mua" in m for m in telegram) == 1
    ac.gateway_session_step(st, KEY, NOW + 600)
    assert sum("DỪNG mua" in m for m in telegram) == 1          # 1 lần/ngày


def test_hold_lifts_when_the_chain_shows_our_spend(monkeypatch, telegram):
    _pending()
    st = {flop_radar.KEY: {"attr_spent_raw": {"explorer.testnet.flop.network": 30 * 10 ** 18}}}
    ac.gateway_attribution_step(st, NOW)
    assert ac.attribution_hold(st, NOW) is None


def test_hold_can_be_turned_off(monkeypatch):
    _pending()
    monkeypatch.setenv("FLOP_ATTRIB_GATE", "off")
    assert ac.attribution_hold({}, NOW) is None


def test_small_unconfirmed_amount_does_not_hold_until_a_foreign_channel_is_seen():
    _pending("5")
    assert ac.attribution_hold({}, NOW) is None
    assert "người khác" in ac.attribution_hold({ac.FOREIGN_SEEN_KEY: NOW}, NOW)


def test_stale_explorer_reading_is_not_evidence():
    _pending()
    host = "explorer.testnet.flop.network"
    st = {flop_radar.KEY: {"attr_spent_raw": {host: 5 * 10 ** 18}, "attr_spent_at": {host: NOW - 7 * 3600}}}
    assert ac.onchain_spent(st, NOW) is None and ac.attribution_hold(st, NOW)


def test_unreadable_explorer_raises_an_alert(monkeypatch, telegram):
    _pending()
    monkeypatch.setattr(ac, "save_state", lambda d: None)
    assert ac.gateway_attribution_step({}, NOW) == "UNVERIFIED" and len(telegram) == 1


def test_confirmed_spend_enters_the_metric_and_the_balance():
    _pending()
    tm.confirm_gateway_attribution("10")
    led = tm.load_ledger()
    assert led["session_cumulative"]["FLOP"] == "10" and led["balances"]["FLOP"] == "90"
    assert tm.gateway_attribution() == {"unattributed": 20, "attributed": 10}


def test_report_separates_unconfirmed_gateway_spend():
    _pending()
    s = flop_report.summarize(tm.load_ledger(), NOW, token="FLOP", mode="testnet")
    assert s["gateway_unverified"] == "30" and s["session_total"] == "0" and s["meter_total"] == "0"
    assert "CHƯA xác nhận" in flop_report.format_report(s)


def test_radar_records_wallet_balance_and_reading_time():
    wallet = "5Wallet"

    def fetch(url):
        if url.endswith(f"/api/accounts/{wallet}"):
            return json.dumps({"balance": "25000000000000000000"})
        if url.endswith("/inference-spend"):
            return json.dumps({"totalSpentRaw": "0"})
        return None
    r = {}
    flop_radar.check_attribution(r, fetch, wallet=wallet, explorers=("https://explorer.testnet.flop.network",))
    h = "explorer.testnet.flop.network"
    assert r["attr_balance_raw"][h] == 25 * 10 ** 18 and r["attr_spent_at"][h] > 0
    assert ac.chain_balance({flop_radar.KEY: r}, r["attr_balance_at"][h]) == 25


# --- cấu hình và đường cấm --------------------------------------------------------------

def test_missing_enroll_endpoint_is_its_own_outcome_with_an_alert(monkeypatch, telegram):
    st = fresh()
    st[fg.KEY].pop("vault")
    srv = Srv()
    srv.enroll_ok = False
    monkeypatch.setattr(ac, "_gateway_http", srv)
    pin_now(monkeypatch, NOW)
    status, _, _ = ac.gateway_session_step(st, KEY, NOW)
    assert status.endswith("gateway/enroll_unavailable") and len(telegram) == 1


def test_testnet_ledger_refuses_a_devnet_network_setting(monkeypatch):
    monkeypatch.setenv("FLOP_GATEWAY_NETWORK", "devnet")
    st = fresh("devnet")
    srv = Srv(net="devnet")
    srv.receipts = [rc("d1")]
    monkeypatch.setattr(ac, "_gateway_http", srv)
    assert ac.gateway_session_step(st, KEY, NOW)[0] == "skip-network-config"
    assert not tm.load_ledger().get("gateway_unattributed")


def test_a_named_testnet_network_is_accepted(monkeypatch):
    monkeypatch.setenv("FLOP_GATEWAY_NETWORK", "flop-testnet-1")
    st = fresh("flop-testnet-1")
    srv = Srv(net="flop-testnet-1")
    monkeypatch.setattr(ac, "_gateway_http", srv)
    pin_now(monkeypatch, NOW)
    assert "gateway/" in ac.gateway_session_step(st, KEY, NOW)[0]


def test_mempool_backend_is_blocked_on_testnet_only(monkeypatch):
    assert ac._mempool_blocked() is True
    monkeypatch.delenv("TESTNET_ENABLED")
    assert ac._mempool_blocked() is False


def test_gemini_empty_reply_falls_back_to_the_next_provider(monkeypatch):
    monkeypatch.setattr(ac, "GEMINI_API_KEY", "x")
    monkeypatch.setattr(ac, "DEEPSEEK_API_KEY", "y")
    for name in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setattr(ac, name, "")
    monkeypatch.setattr(ac, "LLM_PROVIDER_ORDER", ["gemini", "deepseek"])
    monkeypatch.setattr(ac, "LLM_PROVIDER", "auto")
    monkeypatch.setattr(ac, "GEMINI_MODEL", "gm")
    monkeypatch.setattr(ac, "_gemini_model_cache", None)
    monkeypatch.setattr(ac, "_gemini_list_models", lambda: [])

    def post(url, json=None, headers=None, timeout=None):
        if "generativelanguage" in url:
            return R(200, {"candidates": [{"content": {"parts": [{"text": " "}]}}]})
        return R(200, {"choices": [{"message": {"content": "ds"}}]})
    monkeypatch.setattr(ac.requests, "post", post)
    assert ac._provider_reply("q", "s", 0.2) == ("ds", "deepseek")


def test_brief_answer_passes_the_output_guard():
    st = {}
    ac._brief_add(st, NOW, "q", "leak sk-" + "a" * 30, 1.0)
    ac._brief_add(st, NOW, "q", "see https://evil.example now", 1.0)
    items = st[ac.BRIEF_KEY]["items"]
    assert st[ac.BRIEF_KEY]["n"] == 2 and len(items) == 1 and "evil.example" not in items[0]["a"]


# --- audit vòng 2: lỗ hổng trong chính bản sửa ------------------------------------------

def test_run_cancelled_mid_call_cannot_lose_booked_spend(monkeypatch, telegram):
    class Killed(BaseException):
        """Như SIGTERM khi huỷ job: không đi qua `except Exception`."""

    def kill(body, headers):
        raise Killed()
    st = fresh()
    srv = Srv()
    srv.receipts = [rc("r1")]
    srv.chat_fn = kill
    monkeypatch.setattr(ac, "_gateway_http", srv)
    pin_now(monkeypatch, NOW)
    with pytest.raises(Killed):
        ac.gateway_session_step(st, KEY, NOW)
    disk = ac.load_state()
    assert disk[fg.KEY]["receipts_seen"] == ["r1"]
    snap = disk["ledger_snapshot_testnet"]              # sổ phải đi trước "đã thấy"
    assert snap["gateway_unattributed"]["FLOP"] == "6.5"


def test_daily_cap_and_replay_stop_survive_state_loss(monkeypatch, telegram):
    import time
    day = time.strftime("%Y-%m-%d", time.gmtime(NOW))
    st = {"gateway_inflight": {"inflight": None, "profile": fg.profile_of("testnet"), "at": NOW,
                               "day_spent": {"day": day, "spent": fg.DEFAULT_MAX_PER_DAY},
                               "replay_unsafe": NOW - 5}}
    srv = Srv()
    monkeypatch.setattr(ac, "_gateway_http", srv)
    pin_now(monkeypatch, NOW)
    status = ac.gateway_session_step(st, KEY, NOW)[0]
    assert status.endswith("gateway/daily_cap") and srv.chats == [] and st[fg.KEY]["replay_unsafe"]
    durable = st["gateway_inflight"]
    assert durable["day_spent"]["spent"] == fg.DEFAULT_MAX_PER_DAY and durable["replay_unsafe"]


def test_foreign_channel_marker_is_durable():
    assert ac.FOREIGN_SEEN_KEY in ac.DURABLE_STATE_KEYS and "llm_paid" in ac.DURABLE_STATE_KEYS


def test_tiny_confirmed_amount_does_not_open_the_gate(monkeypatch, telegram):
    _pending("500")
    monkeypatch.setattr(ac, "save_state", lambda d: None)
    st = {flop_radar.KEY: {"attr_spent_raw": {"explorer.testnet.flop.network": 10 ** 16}}}
    assert ac.gateway_attribution_step(st, NOW) == "MISMATCH" and len(telegram) == 1
    assert "0.01" in ac.attribution_hold(st, NOW)


def test_large_confirmed_history_tolerates_explorer_lag():
    _pending("1000")
    tm.confirm_gateway_attribution("1000")
    tm.record_gateway_spend("50", "inference-session gateway m", "r-2")
    st = {flop_radar.KEY: {"attr_spent_raw": {"explorer.testnet.flop.network": 1000 * 10 ** 18}}}
    assert ac.attribution_hold(st, NOW) is None and ac.attribution_room(st, NOW) == pytest.approx(950)


def test_exposure_counts_pending_receipts_and_hung_requests():
    _pending("5")
    st = {fg.KEY: {"receipts_open": {"a": [NOW, 6.5], "old-format": NOW}, "inflight": {"max_cost": 3.5},
                   "stats": {"receipts_lost_flop": 1}}}
    assert ac.attribution_room(st, NOW) == pytest.approx(20 - 5 - 6.5 - 3.5 - 1)


def test_room_caps_the_buy_within_the_run(monkeypatch, telegram):
    _pending("15")
    st = fresh()
    srv = Srv(quota=500000.0)
    monkeypatch.setattr(ac, "_gateway_http", srv)
    pin_now(monkeypatch, NOW)
    ac.gateway_session_step(st, KEY, NOW)
    assert srv.chats and sum(c["body"]["max_cost_flop"] for c in srv.chats) <= 5 * 1.1


def test_room_below_one_call_holds_instead_of_waiting_silently(monkeypatch, telegram):
    _pending("19.9")
    st = fresh()
    srv = Srv()
    monkeypatch.setattr(ac, "_gateway_http", srv)
    pin_now(monkeypatch, NOW)
    assert ac.gateway_session_step(st, KEY, NOW)[0].endswith("gateway/attribution_hold")
    assert srv.chats == [] and sum("DỪNG mua" in m for m in telegram) == 1


def test_gate_sees_receipts_from_the_previous_run(monkeypatch, telegram):
    # Biên nhận 'estimated' chỉ đọc được SAU khi vào run_step: gate phải chạy sau bước đó.
    _pending("5")
    st = fresh()
    srv = Srv()
    srv.receipts = [rc("e1", state="estimated", spend=0, reserved_spend=16)]
    monkeypatch.setattr(ac, "_gateway_http", srv)
    pin_now(monkeypatch, NOW)
    assert ac.gateway_session_step(st, KEY, NOW)[0].endswith("gateway/attribution_hold")


def test_named_testnet_uses_the_testnet_explorer(monkeypatch):
    monkeypatch.setenv("FLOP_GATEWAY_NETWORK", "flop-testnet-1")
    assert ac._explorer_base("flop-testnet-1") == "https://explorer.testnet.flop.network"
    _pending()
    st = {flop_radar.KEY: {"attr_spent_raw": {"explorer.testnet.flop.network": 30 * 10 ** 18}}}
    assert ac.onchain_spent(st, NOW) == 30


def test_report_does_not_count_gateway_calls_as_metering():
    tm.credit("100")
    for i in range(4):
        tm.record_gateway_spend("5", "inference-session gateway m", f"r{i}")
    s = flop_report.summarize(tm.load_ledger(), NOW, token="FLOP", mode="testnet")
    assert s["meter_count"] == 0
    tm.confirm_gateway_attribution("10")                # 2/4 phiên về tiền
    s = flop_report.summarize(tm.load_ledger(), NOW, token="FLOP", mode="testnet")
    assert s["session_count"] == 2 and s["meter_count"] == 0


def test_hydrate_keeps_the_ledger_with_more_confirmed_spend():
    _pending()
    stale = {**tm.load_ledger(), "mode": "testnet"}
    tm.confirm_gateway_attribution("30")
    newer = {**tm.load_ledger(), "mode": "testnet"}
    assert ac._ledger_progress(newer) > ac._ledger_progress(stale)


def test_stale_chain_balance_is_ignored():
    host = "explorer.testnet.flop.network"
    r = {"attr_balance_raw": {host: 25 * 10 ** 18}, "attr_balance_at": {host: NOW - 7 * 3600}}
    assert ac.chain_balance({flop_radar.KEY: r}, NOW) is None
    r["attr_balance_at"][host] = NOW - 60
    assert ac.chain_balance({flop_radar.KEY: r}, NOW) == 25


def test_watchdog_recovery_does_not_claim_real_spend():
    st = {}
    flop_watchdog.observe(st, settled_onchain=0, status="0✓ gateway/error", spent_today_onchain=0, now=NOW,
                          alert_runs=1)
    out = flop_watchdog.observe(st, settled_onchain=1, status="1✓ gateway/spent", spent_today_onchain=0, now=NOW + 60)
    assert "CHI THẬT" not in out["recovered"] and "chưa phải bằng chứng" in out["recovered"]


def _gemini_only(monkeypatch):
    monkeypatch.setattr(ac, "GEMINI_API_KEY", "x")
    for name in ("DEEPSEEK_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setattr(ac, name, "")
    monkeypatch.setattr(ac, "LLM_PROVIDER_ORDER", ["gemini"])
    monkeypatch.setattr(ac, "LLM_PROVIDER", "auto")
    monkeypatch.setattr(ac, "GEMINI_MODEL", "gm")
    monkeypatch.setattr(ac, "_gemini_model_cache", None)
    monkeypatch.setattr(ac, "_gemini_list_models", lambda: [f"gemini-{i}-flash" for i in range(30)])
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        raise ac.requests.ReadTimeout("t")
    monkeypatch.setattr(ac.requests, "post", post)
    return calls


def test_gemini_hang_is_bounded_per_reply(monkeypatch):
    calls = _gemini_only(monkeypatch)
    with pytest.raises(Exception):
        ac._provider_reply("q", "s", 0.2)
    assert len(calls) == ac.GEMINI_MAX_TRIES


def test_gemini_stops_when_the_run_budget_is_gone(monkeypatch):
    import time
    calls = _gemini_only(monkeypatch)
    monkeypatch.setattr(ac, "_RUN_STARTED", time.time() - 100000)
    with pytest.raises(Exception):
        ac._provider_reply("q", "s", 0.2)
    assert calls == []


def test_paid_llm_calls_stop_at_the_daily_cap(monkeypatch):
    monkeypatch.setattr(ac, "DEEPSEEK_API_KEY", "y")
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setattr(ac, name, "")
    monkeypatch.setattr(ac, "LLM_PROVIDER_ORDER", ["deepseek"])
    monkeypatch.setattr(ac, "LLM_PROVIDER", "auto")
    monkeypatch.setenv("FLOP_LLM_PAID_MAX_PER_DAY", "1")
    posts = []

    def post(url, json=None, headers=None, timeout=None):
        posts.append(url)
        return R(200, {"choices": [{"message": {"content": "ds"}}]})
    monkeypatch.setattr(ac.requests, "post", post)
    assert ac._provider_reply("q", "s", 0.2) == ("ds", "deepseek")
    with pytest.raises(RuntimeError):
        ac._provider_reply("q", "s", 0.2)
    assert len(posts) == 1
