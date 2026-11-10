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


def _chain_read(raw, at=NOW):
    """Số chi đứng tên ví trên explorer testnet, kèm mốc đọc (số không có mốc bị coi là cũ)."""
    h = "explorer.testnet.flop.network"
    return {"attr_spent_raw": {h: raw}, "attr_spent_at": {h: at}}


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
    st = {flop_radar.KEY: _chain_read(30 * 10 ** 18)}
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
    st = {flop_radar.KEY: _chain_read(10 ** 16)}
    assert ac.gateway_attribution_step(st, NOW) == "MISMATCH" and len(telegram) == 1
    assert "0.01" in ac.attribution_hold(st, NOW)


def test_large_confirmed_history_tolerates_explorer_lag():
    _pending("1000")
    tm.confirm_gateway_attribution("1000")
    tm.record_gateway_spend("50", "inference-session gateway m", "r-2")
    st = {flop_radar.KEY: _chain_read(1000 * 10 ** 18)}
    assert ac.attribution_hold(st, NOW) is None and ac.attribution_room(st, NOW) == pytest.approx(950)


def test_exposure_counts_pending_receipts_and_hung_requests():
    _pending("5")
    st = {fg.KEY: {"receipts_open": {"a": [NOW, 6.5], "old-format": NOW}, "inflight": {"max_cost": 3.5},
                   "receipts_lost_recent": [[NOW - 60, 1]]}}
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
    st = {flop_radar.KEY: _chain_read(30 * 10 ** 18)}
    assert ac.onchain_spent(st, NOW) == 30


def test_chain_reading_without_a_read_time_is_stale_not_fresh():
    # Trước đây số không có mốc đọc được coi là vừa đọc -> xác nhận "đứng tên ví" bằng số không rõ tuổi.
    _pending()
    st = {flop_radar.KEY: {"attr_spent_raw": {"explorer.testnet.flop.network": 30 * 10 ** 18}}}
    assert ac.onchain_spent(st, NOW) is None
    assert ac.gateway_attribution_step(st, NOW) == "UNVERIFIED"
    assert tm.gateway_attribution()["attributed"] == 0
    assert ac.attribution_hold(st, NOW)                         # cổng vẫn đóng


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


# --- báo cáo ngày + thống kê thợ đào bền -------------------------------------------------

def _report(monkeypatch, tmp_path, telegram, mode_testnet, start=""):
    import time as _t
    if mode_testnet:
        monkeypatch.setenv("TESTNET_ENABLED", "true")
    else:
        monkeypatch.delenv("TESTNET_ENABLED", raising=False)
    monkeypatch.setenv("FLOP_TESTNET_START", start)
    monkeypatch.setenv("TOKEN_LEDGER_FILE", str(tmp_path / f"r{mode_testnet}.json"))
    monkeypatch.setattr(ac, "DAILY_REPORT_ENABLED", True)
    tm.credit("100")
    if mode_testnet:
        tm.spend("3", "inference-session:a", submit_tx=lambda tx: {"tx_hash": "0x1"}, rpc="https://r.invalid",
                 log=lambda m: None)
    else:
        tm.spend("3", "inference-session:a", log=lambda m: None)
    assert ac.maybe_send_daily_report({}, int(_t.time()), extras={"kibble": "2✓ x", "session": "3✓", "thợ đào": "8"})
    return telegram[-1]


def test_report_in_simulation_has_no_day_count_and_names_fake_miners(monkeypatch, tmp_path, telegram):
    txt = _report(monkeypatch, tmp_path, telegram, False, "2026-11-03")
    assert "dự phóng" not in txt and "/90" not in txt
    assert "thợ đào mô phỏng: 8" in txt and "phiên lượt cuối: 3✓" in txt and "kibble lượt cuối" in txt


def test_report_projects_only_with_a_published_testnet_start(monkeypatch, tmp_path, telegram):
    assert "dự phóng" not in _report(monkeypatch, tmp_path, telegram, True, "")
    txt = _report(monkeypatch, tmp_path, telegram, True, "2026-01-01")
    assert "dự phóng" in txt and "thợ đào: 8" in txt


def test_miner_stats_come_back_from_signed_kv_after_state_loss(monkeypatch):
    kv = {}
    monkeypatch.setattr(ac, "kv_set", lambda pk, did, k, v: kv.update({k: v}) or True)
    monkeypatch.setattr(ac, "kv_get", lambda k: kv.get(k))
    monkeypatch.setattr(ac, "_miner_stats", {"counts": {"5Miner": 7}, "denied": 1})
    ac.persist_miner_stats(KEY, "did")
    assert ac.hydrate_miner_stats({}, KEY) == {"counts": {"5Miner": 7}, "denied": 1}
    local = {ac._miner_stats_key(): {"counts": {"5Local": 2}}}
    assert ac.hydrate_miner_stats(local, KEY) == local[ac._miner_stats_key()]    # state.json thắng


def test_forged_miner_stats_on_kv_are_ignored(monkeypatch):
    monkeypatch.setattr(ac, "kv_get", lambda k: json.dumps({"body": {"counts": {"x": 9}}, "sig": "AAAA"}))
    assert ac.hydrate_miner_stats({}, KEY) == {}


def test_miner_stats_are_trimmed_to_the_kv_limit(monkeypatch):
    wrote = {}
    monkeypatch.setattr(ac, "kv_set", lambda pk, did, k, v: wrote.update({k: v}) or True)
    counts = {("5" + str(i).zfill(47)): i + 1 for i in range(200)}
    monkeypatch.setattr(ac, "_miner_stats", {"counts": counts, "denied": 0})
    ac.persist_miner_stats(KEY, "did")
    sealed = wrote[ac._miner_stats_key()]
    kept = json.loads(sealed)["body"]["counts"]
    assert len(sealed) <= ac.KV_VALUE_MAX and 0 < len(kept) < 200 and max(counts.values()) in kept.values()


def test_nothing_is_written_without_miner_stats(monkeypatch):
    monkeypatch.setattr(ac, "kv_set", lambda *a, **k: pytest.fail("không có gì để ghi"))
    monkeypatch.setattr(ac, "_miner_stats", {})
    ac.persist_miner_stats(KEY, "did")


# --- audit vòng 3: mạng reset, biên nhận trước enroll, số tiền phi lý ---------------------

def _confirmed(amount="300"):
    """Sổ đã có `amount` FLOP gateway được chain xác nhận ở kỳ hiện tại (chain ghi đúng chừng đó)."""
    tm.credit("1000")
    tm.record_gateway_spend(amount, "inference-session gateway m", "old-1")
    st = {flop_radar.KEY: _chain_read(int(amount) * 10 ** 18, at=NOW - 7200)}
    ac.gateway_attribution_step(st, NOW - 7200)
    assert tm.gateway_attribution()["unattributed"] == 0


def test_chain_reset_opens_a_new_epoch_instead_of_locking_the_gate():
    _confirmed("300")
    tm.record_gateway_spend("20", "inference-session gateway m", "new-1")
    for at in (NOW - 3600, NOW - 1800):                           # không có genesis: cần 3 lần đọc thấp
        st = {flop_radar.KEY: _chain_read(20 * 10 ** 18, at=at)}
        ac.gateway_attribution_step(st, at)
        assert tm.gateway_attribution()["unattributed"] == 20     # chưa kết luận reset
    st = {flop_radar.KEY: _chain_read(20 * 10 ** 18, at=NOW)}
    ac.gateway_attribution_step(st, NOW)
    assert tm.gateway_attribution() == {"unattributed": 0, "attributed": 320}
    assert ac.attribution_hold(st, NOW) is None
    # Kỳ mới tiếp tục bình thường.
    tm.record_gateway_spend("15", "inference-session gateway m", "new-2")
    tm.confirm_gateway_attribution("35", read_at=NOW + 3600)
    assert tm.gateway_attribution() == {"unattributed": 0, "attributed": 335}
    assert tm.load_ledger()["session_cumulative"]["FLOP"] == "335"


def test_one_low_explorer_reading_does_not_confirm_pending_spend_with_old_history():
    _confirmed("300")
    tm.record_gateway_spend("50", "inference-session gateway m", "new-1")
    assert tm.confirm_gateway_attribution("0", read_at=NOW - 3600)["outcome"] == "low_reading"
    tm.confirm_gateway_attribution("300", read_at=NOW)               # explorer hồi phục: số cũ
    assert tm.gateway_attribution()["unattributed"] == 50            # không bị "xác nhận" bằng chi cũ


def test_the_same_low_reading_is_not_counted_twice():
    _confirmed("300")
    tm.record_gateway_spend("20", "inference-session gateway m", "new-1")
    for _ in range(3):
        tm.confirm_gateway_attribution("20", read_at=NOW)
    assert tm.gateway_attribution()["unattributed"] == 20


def test_ledger_without_epoch_keys_detects_a_reset_that_already_happened():
    """Sổ đã nằm trên KV từ trước bản sửa: chỉ có gateway_attributed, không có khoá kỳ."""
    _confirmed("300")
    led = tm.load_ledger()
    for k in ("gateway_chain_hw", "gateway_epoch_done", "gateway_chain_low"):
        led.pop(k, None)
    tm.save_ledger(led)
    tm.record_gateway_spend("20", "inference-session gateway m", "new-1")
    for at in (NOW - 3600, NOW - 1800, NOW):
        tm.confirm_gateway_attribution("20", read_at=at)
    assert tm.gateway_attribution() == {"unattributed": 0, "attributed": 320}


def test_chain_epoch_keys_go_to_kv_with_the_ledger(monkeypatch):
    _confirmed("300")
    sent = {}
    monkeypatch.setattr(ac, "kv_set", lambda pk, did, k, v: sent.update({k: v}) or True)
    ac.persist_ledger_to_kv(KEY, ac.did_of(KEY))
    body = json.loads(sent[ac._ledger_kv_key()])["body"]
    assert body["gateway_chain_hw"] == {"FLOP": "300"} and body["gateway_epoch_done"] == {"FLOP": "300"}


def test_receipts_created_before_the_first_enrollment_are_not_booked():
    st, srv = {}, Srv()                                            # ví enroll lần đầu trên mạng này
    srv.receipts = [rc(f"old{i}", spend=2.3, created_at=(NOW - 10 * 86400) * 1000) for i in range(18)]
    srv.receipts.append(rc("mine", spend=1.5, created_at=(NOW - 60) * 1000))
    r = step(st, srv)
    assert st[fg.KEY]["first_enrolled_at"] == NOW
    assert r["pre_enroll_skipped"] == 18
    assert tm.gateway_attribution()["unattributed"] == pytest.approx(1.5)
    assert fg.unsettled_exposure(st[fg.KEY]) == 0
    r2 = step(st, srv, now=NOW + 300)                             # lượt sau không đếm lại
    assert r2["pre_enroll_skipped"] == 0 and tm.gateway_attribution()["unattributed"] == pytest.approx(1.5)


def test_re_enrolling_on_the_same_network_keeps_late_receipts():
    st, srv = fresh(), Srv()
    st[fg.KEY].update(first_enrolled_at=NOW - 5000, enrolled_at=NOW - 100)
    srv.receipts = [rc("late", spend=3.0, created_at=(NOW - 3000) * 1000)]   # trước lần enroll LẠI
    step(st, srv)
    assert tm.gateway_attribution()["unattributed"] == pytest.approx(3.0)


def test_raw_unit_amount_is_held_back_reported_once_and_booked_when_fixed():
    st, srv = fresh(), Srv()
    srv.receipts = [rc("r1", spend="6500000000000000000")]
    r = step(st, srv)
    assert r["implausible_new"] == 1 and tm.gateway_attribution()["unattributed"] == 0
    assert fg.unsettled_exposure(st[fg.KEY]) < 1000
    r = step(st, srv, now=NOW + 300)
    assert r["implausible_new"] == 0 and tm.gateway_attribution()["unattributed"] == 0
    srv.receipts = [rc("r1", spend=6.5)]                           # gateway sửa số
    step(st, srv, now=NOW + 600)
    assert tm.gateway_attribution()["unattributed"] == pytest.approx(6.5)


def test_implausible_amount_rules(monkeypatch):
    assert fg.implausible_amount({}, 6.5) == ""
    assert fg.implausible_amount({}, 6.5e18)
    assert fg.implausible_amount({"reserved_spend": 1.0}, 10.0)
    assert fg.implausible_amount({"reserved_spend": 1.0}, 2.5) == ""
    assert fg.implausible_amount({}, float("nan"))
    monkeypatch.setenv("FLOP_GATEWAY_MAX_PER_RUN", "0")             # trần lượt tắt -> trần tuyệt đối
    assert fg.implausible_amount({}, 5000) == "" and fg.implausible_amount({}, 2e6)


def test_implausible_receipt_raises_one_telegram_alert(monkeypatch, telegram):
    st, srv = fresh(), Srv()
    srv.receipts = [rc("r1", spend="6500000000000000000")]
    monkeypatch.setattr(ac, "_gateway_http", srv)
    pin_now(monkeypatch, NOW)
    ac.gateway_session_step(st, KEY, NOW)
    ac.gateway_session_step(st, KEY, NOW + 300)
    assert sum("phi lý" in m for m in telegram) == 1
    assert ac.attribution_hold(st, NOW) is None


# --- audit vòng 4: lỗ hổng trong bản sửa vòng 3 + cổng tự khoá + tên mạng ------------------------

GEN_A, GEN_B = "0x" + "aa" * 16, "0x" + "bb" * 16


def _confirmed_on(chain_id, amount="300"):
    tm.credit("1000")
    tm.record_gateway_spend(amount, "inference-session gateway m", "old-1")
    tm.confirm_gateway_attribution(amount, read_at=NOW - 9000, chain_id=chain_id)
    assert tm.gateway_attribution()["unattributed"] == 0


def _gstep(monkeypatch, st, now):
    """gateway_session_step với `now` thật sự là `now` (run_step nhận đúng mốc đó)."""
    real = fg.run_step
    monkeypatch.setattr(fg, "run_step", lambda state, **k: real(state, **{**k, "now": now}))
    try:
        return ac.gateway_session_step(st, KEY, now)
    finally:
        monkeypatch.setattr(fg, "run_step", real)


def test_low_readings_on_the_same_genesis_never_open_a_new_epoch():
    """Explorer index lại trả 0 nhiều giờ rồi trả lại số cũ: genesis không đổi -> không phải reset."""
    _confirmed_on(GEN_A)
    tm.record_gateway_spend("50", "inference-session gateway m", "broker-attributed")
    for at in (NOW - 7200, NOW - 3600, NOW - 1800, NOW - 600):
        assert tm.confirm_gateway_attribution("0", read_at=at, chain_id=GEN_A)["outcome"] == "low_reading"
    tm.confirm_gateway_attribution("300", read_at=NOW, chain_id=GEN_A)
    assert tm.gateway_attribution() == {"unattributed": 50, "attributed": 300}
    assert tm.load_ledger()["session_cumulative"]["FLOP"] == "300"


def test_a_new_genesis_opens_the_epoch_on_the_first_low_reading():
    _confirmed_on(GEN_A)
    tm.record_gateway_spend("20", "inference-session gateway m", "new-1")
    tm.confirm_gateway_attribution("20", read_at=NOW, chain_id=GEN_B)
    assert tm.gateway_attribution() == {"unattributed": 0, "attributed": 320}
    assert tm.load_ledger()["gateway_chain_id"] == {"FLOP": GEN_B}


def test_a_new_genesis_without_a_drop_does_not_reuse_old_history():
    """Gateway báo genesis khác nhưng tổng on-chain không tụt: không đủ để kết luận reset."""
    _confirmed_on(GEN_A)
    tm.record_gateway_spend("20", "inference-session gateway m", "new-1")
    tm.confirm_gateway_attribution("300", read_at=NOW - 600, chain_id=GEN_B)
    assert tm.gateway_attribution()["unattributed"] == 20
    assert tm.load_ledger()["gateway_chain_id"] == {"FLOP": GEN_A}       # vẫn so với genesis của kỳ
    tm.confirm_gateway_attribution("5", read_at=NOW, chain_id=GEN_B)     # giờ mới tụt -> reset thật
    assert tm.gateway_attribution() == {"unattributed": 15, "attributed": 305}


def test_run_step_records_the_gateway_genesis_and_the_ledger_uses_it():
    st, srv = fresh(), Srv()
    plain = srv.__call__

    def with_genesis(method, url, headers=None, json=None, timeout=20):
        if url.endswith("/health"):
            return R(200, {"network": {"network": "testnet", "observed_genesis_hash": GEN_A.upper().replace("0X", "0x")}})
        return plain(method, url, headers=headers, json=json, timeout=timeout)

    step(st, with_genesis)
    assert st[fg.KEY]["genesis"] == GEN_A
    _pending("30")
    st[flop_radar.KEY] = _chain_read(30 * 10 ** 18)
    ac.gateway_attribution_step(st, NOW)
    assert tm.load_ledger()["gateway_chain_id"] == {"FLOP": GEN_A}


def test_genesis_pin_blocks_another_chain(monkeypatch):
    srv = Srv()
    plain = srv.__call__

    def with_genesis(method, url, headers=None, json=None, timeout=20):
        if url.endswith("/health"):
            return R(200, {"network": {"network": "testnet", "observed_genesis_hash": GEN_A}})
        return plain(method, url, headers=headers, json=json, timeout=timeout)

    monkeypatch.setenv("FLOP_GATEWAY_GENESIS", GEN_B)
    assert step(fresh(), with_genesis)["outcome"] == "network_mismatch" and srv.chats == []
    assert step(fresh(), srv)["outcome"] == "network_mismatch"            # /health không có genesis
    monkeypatch.setenv("FLOP_GATEWAY_GENESIS", GEN_A)
    assert step(fresh(), with_genesis)["outcome"] == "spent"


def test_state_loss_does_not_reset_the_first_enrollment_mark(monkeypatch, telegram):
    """Mất state.json: mốc lọc lấy lại từ khoá bền -> biên nhận của chính mình settle trước đó vẫn ghi sổ."""
    srv = Srv()
    monkeypatch.setattr(ac, "_gateway_http", srv)
    st = {}
    _gstep(monkeypatch, st, NOW - 7200)                                   # enroll lần đầu, mua
    assert st["gateway_inflight"]["first_enrolled_at"] == NOW - 7200
    srv.receipts = [rc("mine-late", spend=4.0, created_at=(NOW - 3600) * 1000)]
    st2 = {"gateway_inflight": st["gateway_inflight"]}                    # thứ hydrate từ KV trả lại
    _gstep(monkeypatch, st2, NOW)
    assert st2[fg.KEY]["first_enrolled_at"] == NOW - 7200
    assert tm.gateway_attribution()["unattributed"] == pytest.approx(4.0)


def test_lost_receipts_stop_counting_as_pending_after_a_day():
    g = {"receipts_lost_recent": [[NOW - 90000, 30], [NOW - 60, 2.5]], "stats": {"receipts_lost_flop": 999}}
    assert fg.lost_exposure(g, NOW) == pytest.approx(2.5)
    assert fg.unsettled_exposure(g, NOW) == pytest.approx(2.5)
    assert fg.lost_exposure(g) == pytest.approx(32.5)


def test_cumulative_lost_total_no_longer_locks_the_gate():
    _confirmed("30")
    st = {flop_radar.KEY: _chain_read(30 * 10 ** 18),
          fg.KEY: {"stats": {"receipts_lost": 5, "receipts_lost_flop": 32.5}}}
    assert ac.attribution_hold(st, NOW) is None and ac.attribution_room(st, NOW) == pytest.approx(30)


def test_chain_spend_beyond_the_ledger_offsets_recently_lost_receipts():
    _confirmed("30")
    g = {"receipts_lost_recent": [[NOW - 600, 25]]}
    st = {flop_radar.KEY: _chain_read(30 * 10 ** 18), fg.KEY: g}
    assert ac.attribution_room(st, NOW) == pytest.approx(30 - 25)         # chain chưa ghi phần mất dấu
    st = {flop_radar.KEY: _chain_read(60 * 10 ** 18), fg.KEY: g}          # chain ghi dư 30 so với sổ
    assert ac.attribution_room(st, NOW) == pytest.approx(60)


def test_a_lost_receipt_is_recorded_with_its_time():
    st, srv = fresh(), Srv()
    srv.receipts = [rc("late", state="estimated", spend=0, reserved_spend=6.5)]
    srv.chat_fn = lambda b, h: R(429, {"error": "x"})
    step(st, srv)
    st[fg.KEY]["wait_until"] = 0
    srv.receipts = [rc(f"n{i}") for i in range(20)]
    step(st, srv, now=NOW + 700)
    assert st[fg.KEY]["receipts_lost_recent"] == [[NOW + 700, 6.5]]
    assert fg.lost_exposure(st[fg.KEY], NOW + 700 + 86400) == 0


def _held(monkeypatch, amount="30"):
    tm.credit("100")
    tm.record_gateway_spend(amount, "inference-session gateway m", "broker-1")
    srv = Srv()
    monkeypatch.setattr(ac, "_gateway_http", srv)
    st = fresh()
    return st, srv


def _run_at(monkeypatch, st, now, chain_raw=0):
    st[flop_radar.KEY] = _chain_read(chain_raw, at=now)
    return _gstep(monkeypatch, st, now)[0].split("/")[-1]


def test_gate_probes_once_per_interval_while_holding(monkeypatch, telegram):
    st, srv = _held(monkeypatch)
    assert _run_at(monkeypatch, st, NOW) == "attribution_hold"
    assert _run_at(monkeypatch, st, NOW + 3 * 3600) == "attribution_hold" and srv.chats == []
    assert _run_at(monkeypatch, st, NOW + 6 * 3600) == "spent" and len(srv.chats) == 1    # lượt thăm dò
    assert st[ac.ATTRIB_PROBE_KEY] == {"at": NOW + 6 * 3600, "pend": "30", "chain": 0.0}
    assert _run_at(monkeypatch, st, NOW + 6 * 3600 + 300) == "attribution_hold" and len(srv.chats) == 1
    assert _run_at(monkeypatch, st, NOW + 12 * 3600) == "spent" and len(srv.chats) == 2
    assert st[ac.ATTRIB_PROBE_KEY]["chain"] == 0.0                         # giữ mốc của lượt đầu


def test_probe_call_is_one_minimal_call():
    st, srv = fresh(), Srv(quota=500000.0)
    r = step(st, srv, gate=lambda: ("giữ", 0.0), probe=lambda: True)
    assert r["outcome"] == "spent" and r["probe"] is True and len(srv.chats) == 1
    assert srv.chats[0]["body"]["max_tokens"] < 200


def test_probe_can_be_turned_off(monkeypatch, telegram):
    monkeypatch.setenv("FLOP_ATTRIB_PROBE_HOURS", "0")
    st, srv = _held(monkeypatch)
    for h in (0, 6, 12, 24):
        assert _run_at(monkeypatch, st, NOW + h * 3600) == "attribution_hold"
    assert srv.chats == []


def test_probe_also_covers_room_smaller_than_one_call(monkeypatch, telegram):
    st, srv = _held(monkeypatch, "19.99")
    assert _run_at(monkeypatch, st, NOW) == "attribution_hold"
    assert _run_at(monkeypatch, st, NOW + 6 * 3600) == "spent" and len(srv.chats) == 1


def test_probe_seen_on_chain_writes_off_foreign_spend_and_reopens_the_gate(monkeypatch, telegram):
    st, srv = _held(monkeypatch)
    st[ac.FOREIGN_SEEN_KEY] = NOW - 100
    _run_at(monkeypatch, st, NOW)
    t = NOW + 6 * 3600
    assert _run_at(monkeypatch, st, t) == "spent" and len(srv.chats) == 1
    srv.receipts = [rc("probe", spend=0.05, created_at=t * 1000)]          # lượt thăm dò settle
    assert _run_at(monkeypatch, st, t + 300) == "attribution_hold"         # đã ghi sổ, chain chưa đọc lại
    st[flop_radar.KEY] = _chain_read(5 * 10 ** 16, at=t + 600)             # explorer: ví mình +0.05
    ac.gateway_attribution_step(st, t + 600)
    led = tm.load_ledger()
    assert led["gateway_written_off"] == {"FLOP": "30"}
    ga = tm.gateway_attribution()
    assert ga["unattributed"] == 0 and float(ga["attributed"]) == pytest.approx(0.05)
    assert led["session_cumulative"]["FLOP"] == "0.05"                      # khoản cũ KHÔNG vào thước đo
    assert not st.get(ac.FOREIGN_SEEN_KEY) and ac.ATTRIB_PROBE_KEY not in st
    assert sum("thăm dò" in m for m in telegram) == 1
    assert ac.attribution_hold(st, t + 600) is None
    before = len(srv.chats)
    assert _run_at(monkeypatch, st, t + 900, chain_raw=5 * 10 ** 16) == "spent" and len(srv.chats) > before


def test_probe_not_seen_on_chain_changes_nothing(monkeypatch, telegram):
    st, srv = _held(monkeypatch)
    _run_at(monkeypatch, st, NOW)
    t = NOW + 6 * 3600
    _run_at(monkeypatch, st, t)
    st[flop_radar.KEY] = _chain_read(0, at=t + 600)                        # chain vẫn không ghi gì cho ví
    ac.gateway_attribution_step(st, t + 600)
    assert tm.gateway_attribution()["unattributed"] == 30
    assert "gateway_written_off" not in tm.load_ledger() and ac.ATTRIB_PROBE_KEY in st
    assert ac.attribution_hold(st, t + 600) is not None


def test_written_off_spend_goes_to_kv_with_the_ledger(monkeypatch):
    _pending("30")
    assert tm.write_off_gateway_pending("12") == 12
    sent = {}
    monkeypatch.setattr(ac, "kv_set", lambda pk, did, k, v: sent.update({k: v}) or True)
    ac.persist_ledger_to_kv(KEY, ac.did_of(KEY))
    assert json.loads(sent[ac._ledger_kv_key()])["body"]["gateway_written_off"] == {"FLOP": "12"}
    assert tm.gateway_attribution()["unattributed"] == 18


def test_testnet_with_its_own_name_can_be_declared(monkeypatch, telegram):
    srv = Srv(net="aurora-1")
    monkeypatch.setattr(ac, "_gateway_http", srv)
    pin_now(monkeypatch, NOW)
    assert ac.gateway_session_step(fresh(), KEY, NOW)[0].endswith("gateway/network_mismatch")
    monkeypatch.setenv("FLOP_TESTNET_NETWORK_NAME", "Aurora-1")
    assert ac.gateway_session_step(fresh("aurora-1"), KEY, NOW)[0].endswith("gateway/spent") and srv.chats
    assert ac._explorer_base("aurora-1") == "https://explorer.testnet.flop.network"


def test_declared_testnet_name_cannot_be_a_devnet(monkeypatch):
    srv = Srv(net="flop-devnet")
    monkeypatch.setattr(ac, "_gateway_http", srv)
    monkeypatch.setenv("FLOP_TESTNET_NETWORK_NAME", "flop-devnet")
    assert ac.gateway_session_step(fresh("flop-devnet"), KEY, NOW)[0] == "skip-network-config"
    assert srv.chats == []


def test_hold_marks_are_cleared_once_the_gate_lets_a_normal_buy_through(monkeypatch, telegram):
    srv = Srv()
    monkeypatch.setattr(ac, "_gateway_http", srv)
    st = fresh()
    st.update({ac.ATTRIB_HOLD_SINCE_KEY: NOW - 99999, ac.ATTRIB_PROBE_KEY: {"at": NOW - 9, "chain": 0.0, "pend": "30"}})
    assert _run_at(monkeypatch, st, NOW) == "spent"
    assert ac.ATTRIB_HOLD_SINCE_KEY not in st and ac.ATTRIB_PROBE_KEY not in st
    _pending("30")                                                # đợt dừng MỚI: lượt đầu chỉ ghi mốc
    assert _run_at(monkeypatch, st, NOW + 300) == "attribution_hold"
    assert st[ac.ATTRIB_HOLD_SINCE_KEY] == NOW + 300 and len(srv.chats) >= 1
    n = len(srv.chats)
    assert _run_at(monkeypatch, st, NOW + 600) == "attribution_hold" and len(srv.chats) == n

