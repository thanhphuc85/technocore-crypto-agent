"""Test flop_direct — mua compute TRỰC TIẾP từ một miner (kênh đứng tên ví agent).

CLI chính thức và explorer được giả (runner/fetch_channel tiêm vào): không Docker, không Node, không mạng."""
import json
import os
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import agent_cron as ac
import flop_direct as fd
import flop_gateway as fg
import token_manager as tm

NOW = 1_800_000_000
KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
ADDR = fg.wallet_address(KEY)
MINER = fg.ss58_encode(bytes([7]) * 32)
OTHER = fg.ss58_encode(bytes([9]) * 32)
CH = "0x" + "ab" * 32
CH2 = "0x" + "cd" * 32
MH = "0x" + "11" * 32
MH2 = "0x" + "22" * 32


def offer(miner=MINER, min_escrow=5 * 10 ** 16, model_hash=MH, forward=None, max_turns=1):
    """Một dòng /api/session-offers đúng định dạng SDK đọc ({"offer": {...}, "minerSignature": ...})."""
    o = {"miner": miner, "modelHash": model_hash, "precision": 0, "enclaveKey": "0x" + "00" * 32,
         "minEscrow": str(min_escrow), "maxTurns": max_turns, "maxDuration": "600", "expiry": "99999999",
         "nonce": "1"}
    if forward:
        o["forward"] = forward
    return {"offer": o, "minerSignature": "0x" + "ee" * 64}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("FLOP_DIRECT") or k.startswith("FLOP_WALLET"):
            monkeypatch.delenv(k)
    for k, v in {"FLOP_DIRECT_ENABLED": "on", "FLOP_DIRECT_MINER": MINER,
                 "FLOP_DIRECT_ENDPOINT": "https://miner.example", "FLOP_DIRECT_MODEL": "qwen/qwen2.5-0.5b-instruct",
                 "FLOP_WALLET_USE_DID": "on", "TESTNET_ENABLED": "true", "FLOP_DIRECT_MAX_PER_RUN": "1",
                 "FLOP_DIRECT_MAX_TOKENS": "8", "FLOP_DIRECT_PACE": "off",
                 "TOKEN_LEDGER_FILE": str(tmp_path / "ledger.json")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ac, "STATE_FILE", str(tmp_path / "state.json"))


class CLI:
    """CLI giả: `keys import` trả địa chỉ (mặc định đúng ví agent), `session run` theo kịch bản."""

    def __init__(self, session=None, address=ADDR):
        self.calls, self.address = [], address
        self.session = session or (0, json.dumps({"ok": True, "reply": "OK", "channel": CH, "settle_tx": "0x01",
                                                  "completion_tokens": "2"}), "")

    def __call__(self, cmd, env, timeout):
        self.calls.append({"cmd": list(cmd), "env": dict(env), "timeout": timeout})
        if "keys" in cmd and "import" in cmd:
            ks = cmd[cmd.index("--out") + 1]
            return 0, json.dumps({"ok": True, "address": self.address, "keystore": ks, "type": "ed25519"}), ""
        return self.session

    def sessions(self):
        return [c for c in self.calls if "session" in c["cmd"]]


def paid(agent=ADDR, miner_paid=20 * 10 ** 15, burned=10 ** 15, head="finalized"):
    return {"agent": agent, "status": "settled", "paymentStatus": "PAID", "minerPaid": str(miner_paid),
            "burned": str(burned), "observationHead": head}


def mst(state, miner=MINER):
    """Trạng thái riêng của một miner trong state[flop_direct]."""
    return state[fd.KEY]["miners"][fd._mkey(miner)]


def step(state, cli, explorer=None, book=True, network="testnet", **kw):
    explorer = explorer if explorer is not None else {}
    alerts = kw.pop("alerts", [])

    def book_fn(amount, channel, model, escrow=None):
        return tm.record_direct_spend(amount, f"inference-session direct {model}", channel, escrow=escrow)

    offers = kw.pop("offers", [offer()])
    offers_fn = kw.pop("offers_fn", lambda miner, mh: offers)
    return fd.run_step(state, agent_key=KEY, now=kw.pop("now", NOW), prompt=kw.pop("prompt", "What is 2+2?"),
                       network=network, fetch_channel=lambda cid: explorer.get(cid),
                       book_fn=book_fn if book else None, runner=cli, alert=alerts.append,
                       log=lambda m: None, offers_fn=offers_fn, **kw)


# --- cấu hình & chốt chặn -------------------------------------------------------------------

def test_off_by_default_makes_no_calls(monkeypatch):
    monkeypatch.delenv("FLOP_DIRECT_ENABLED")
    cli = CLI()
    assert step({}, cli)["outcome"] == "off" and cli.calls == []


@pytest.mark.parametrize("var,val,why", [
    ("FLOP_DIRECT_MINER", "", "thiếu FLOP_DIRECT_MINER"),
    ("FLOP_DIRECT_MINER", "5notanaddress", "không phải địa chỉ SS58"),
    ("FLOP_DIRECT_ENDPOINT", "http://miner.example", "https://"),
    ("FLOP_DIRECT_MODEL", "qwen; rm -rf /", "ký tự lạ"),
    ("FLOP_DIRECT_MAX_FLOP", "999", "<= 200"),
    ("FLOP_DIRECT_MAX_FLOP", "-1", "số dương"),
    ("FLOP_DIRECT_VIA", "force", "FLOP_DIRECT_ALLOW_FORCE=on"),
    ("FLOP_DIRECT_VIA", "auto", "FLOP_DIRECT_ALLOW_FORCE=on"),
])
def test_bad_configuration_opens_nothing(monkeypatch, var, val, why):
    monkeypatch.setenv(var, val)
    cli = CLI()
    r = step({}, cli)
    assert r["outcome"] == "unconfigured" and why in r["reason"] and cli.calls == []


def test_force_needs_the_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_VIA", "force")
    monkeypatch.setenv("FLOP_DIRECT_ALLOW_FORCE", "on")
    cli = CLI()
    assert step({}, cli)["outcome"] == "opened"
    assert cli.sessions()[0]["cmd"][-1] == "force"


def test_buying_from_the_agents_own_wallet_is_refused(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MINER", fg.ss58_encode(fg._raw_pub(KEY), prefix=0))   # cùng ví, prefix khác
    cli = CLI()
    assert step({}, cli)["outcome"] == "refused" and cli.calls == []


def test_denied_miner_is_refused(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_DENY_MINERS", f"{OTHER},{MINER}")
    cli = CLI()
    assert step({}, cli)["outcome"] == "refused" and cli.calls == []
    monkeypatch.delenv("FLOP_DIRECT_DENY_MINERS")
    assert step({}, cli, deny=[MINER])["outcome"] == "refused" and cli.calls == []


# --- khoá: chỉ qua biến môi trường, keystore tạm, đúng địa chỉ ------------------------------------

def test_secret_is_never_on_the_command_line_and_only_reaches_the_import():
    cli = CLI()
    step({}, cli)
    seed_hex = fg._raw_priv(fg.wallet_key(KEY)).hex()
    for c in cli.calls:
        assert all(seed_hex not in a for a in c["cmd"])
    imp, run = cli.calls
    assert imp["env"][fd.SECRET_ENV] == "0x" + seed_hex
    assert fd.SECRET_ENV not in run["env"] and fd.PASSWORD_ENV in run["env"]
    assert imp["env"][fd.PASSWORD_ENV] == run["env"][fd.PASSWORD_ENV] and len(run["env"][fd.PASSWORD_ENV]) >= 32


def test_docker_command_pins_the_image_digest_and_passes_env_by_name_only():
    cli = CLI()
    step({}, cli)
    imp, run = cli.calls
    assert imp["cmd"][:2] == ["docker", "run"] and fd.DEFAULT_IMAGE in imp["cmd"] and "@sha256:" in fd.DEFAULT_IMAGE
    assert ["-e", fd.SECRET_ENV] == imp["cmd"][imp["cmd"].index(fd.SECRET_ENV) - 1:imp["cmd"].index(fd.SECRET_ENV) + 1]
    assert fd.SECRET_ENV not in run["cmd"]
    flop_args = run["cmd"][run["cmd"].index(fd.DEFAULT_IMAGE) + 1:]       # sau image = tham số của CLI
    assert flop_args[flop_args.index("--keystore") + 1] == "/work/keystore.json"
    assert flop_args[flop_args.index("--network") + 1] == "testnet"
    assert flop_args[flop_args.index("--via") + 1] == "offer"


def test_temporary_keystore_folder_is_removed(monkeypatch):
    made = []
    real = fd.tempfile.mkdtemp
    monkeypatch.setattr(fd.tempfile, "mkdtemp", lambda **k: made.append(real(**k)) or made[-1])
    step({}, CLI())
    assert made and not os.path.exists(made[0])


def test_keystore_for_another_address_stops_everything():
    cli, alerts = CLI(address=OTHER), []
    st = {}
    r = step(st, cli, alerts=alerts)
    assert r["outcome"] == "refused" and cli.sessions() == [] and alerts
    assert st[fd.KEY]["wait_until"] == NOW + fd.UNSUPPORTED_BACKOFF_SEC       # dừng CHUNG, mọi miner


def test_local_cli_mode(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_CLI", "node /opt/flop/cli.js")
    cli = CLI()
    step({}, cli)
    run = cli.sessions()[0]["cmd"]
    assert run[:2] == ["node", "/opt/flop/cli.js"] and run[run.index("--keystore") + 1].endswith("keystore.json")


# --- ghi sổ theo explorer -----------------------------------------------------------------

def test_happy_path_books_only_after_the_explorer_shows_paid():
    st, cli = {}, CLI()
    r = step(st, cli)
    assert r["outcome"] == "opened" and st[fd.KEY]["pending"][0]["channel"] == CH
    assert tm.load_ledger().get("session_cumulative") is None              # chưa ghi: explorer chưa có
    nxt = CLI(session=(0, json.dumps({"ok": True, "channel": CH2, "settle_tx": "0x02"}), ""))
    r = step(st, nxt, explorer={CH: paid()}, now=NOW + 300)
    assert r["booked"] == 1 and r["booked_flop"] == pytest.approx(0.021)
    led = tm.load_ledger()
    assert led["session_cumulative"]["FLOP"] == "0.021" and led["unlock"]["spent_testnet"]["FLOP"] == "0.021"
    assert led["direct_attributed"]["FLOP"] == "0.021" and led["gateway_epoch_done"]["FLOP"] == "0.021"
    assert [p["channel"] for p in st[fd.KEY]["pending"]] == [CH2]          # kênh cũ xong -> mở kênh mới


def test_direct_spend_does_not_confirm_pending_gateway_spend():
    """Chi trực tiếp nằm trong tổng on-chain của ví nhưng đã có trong sổ -> không được dùng để xác nhận
    nhầm khoản gateway đang chờ (đứng tên broker)."""
    tm.credit("100")
    tm.record_gateway_spend("5", "inference-session gateway m", "gw-1")
    st = {}
    step(st, CLI())
    step(st, CLI(), explorer={CH: paid(miner_paid=3 * 10 ** 18, burned=0)}, now=NOW + 300)
    tm.confirm_gateway_attribution("3", read_at=NOW + 400)               # chain = đúng phần trực tiếp
    assert tm.gateway_attribution() == {"unattributed": 5, "attributed": 0}


def test_devnet_practice_never_books(monkeypatch):
    monkeypatch.setenv("TESTNET_ENABLED", "false")
    st = {}
    step(st, CLI(), book=False, network="devnet")
    r = step(st, CLI(), explorer={CH: paid()}, book=False, network="devnet", now=NOW + 300)
    assert r["booked"] == 0 and st[fd.KEY]["stats"]["paid"] == 1
    assert tm.record_direct_spend("1", "x", CH)["outcome"] == "skipped_simulation"


def test_channel_not_in_our_name_is_never_booked():
    st, alerts = {}, []
    step(st, CLI())
    r = step(st, CLI(), explorer={CH: paid(agent=OTHER)}, now=NOW + 300, alerts=alerts)
    assert r["booked"] == 0 and alerts and tm.load_ledger().get("session_cumulative") is None


def test_unfinalized_paid_channel_waits():
    st = {}
    step(st, CLI())
    nxt = CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), ""))
    r = step(st, nxt, explorer={CH: paid(head="best")}, now=NOW + 300)
    assert r["booked"] == 0 and CH in [p["channel"] for p in st[fd.KEY]["pending"]]


def test_refunded_channel_is_dropped_with_zero_spend():
    st = {}
    step(st, CLI())
    r = step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), "")),
             explorer={CH: {"agent": ADDR, "status": "timedout", "paymentStatus": "REFUNDED",
                            "observationHead": "finalized", "minerPaid": "0", "burned": "0"}}, now=NOW + 300)
    assert r["outcome"] == "opened" and st[fd.KEY]["stats"]["refunded"] == 1
    assert tm.load_ledger().get("session_cumulative") is None


def test_the_same_channel_is_booked_once_even_after_state_loss():
    st = {}
    step(st, CLI())
    saved = dict(st[fd.KEY])
    step(st, CLI(), explorer={CH: paid()}, now=NOW + 300)
    st2 = {fd.KEY: saved}                                                 # trạng thái cũ còn kênh chờ
    step(st2, CLI(), explorer={CH: paid()}, now=NOW + 600)
    assert tm.load_ledger()["session_cumulative"]["FLOP"] == "0.021"


# --- không chồng kênh, quá giờ, lỗi ----------------------------------------------------------

def test_a_settled_channel_awaiting_the_explorer_does_not_block_and_alerts_once_when_old():
    st, alerts = {}, []
    step(st, CLI())                                                    # CLI báo settle; explorer chưa có
    cli = CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), ""))
    assert step(st, cli, now=NOW + 300)["outcome"] == "opened"         # không chặn phiên mới
    idle = CLI()
    st[fd.KEY]["miners"][fd._mkey(MINER)]["wait_until"] = NOW + 10 ** 6   # chỉ để không mở thêm
    step(st, idle, now=NOW + fd.PENDING_ALERT_SEC + 10, alerts=alerts)
    step(st, idle, now=NOW + fd.PENDING_ALERT_SEC + 400, alerts=alerts)
    assert len(alerts) == 2 and all("khoá" in a for a in alerts)        # mỗi kênh báo đúng 1 lần


def test_a_failed_channel_blocks_only_its_own_miner():
    err = json.dumps({"ok": False, "code": "SESSION_FAILED", "message": "miner acknowledgement timed out",
                      "channel": CH2})
    st = {}
    step(st, CLI(session=(1, "", err)))
    mst(st)["wait_until"] = 0                                          # hết nghỉ, kênh lỗi vẫn chưa xong
    cli = CLI()
    r = step(st, cli, now=NOW + 300)
    assert r["outcome"] == "pending" and cli.calls == []


def test_too_many_channels_awaiting_the_explorer_stop_new_sessions(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PENDING", "2")
    st = {fd.KEY: {"pending": [{"channel": "0x" + f"{i:064x}", "ts": NOW, "network": "testnet", "miner": MINER}
                               for i in range(2)]}}
    cli = CLI()
    r = step(st, cli)
    assert r["outcome"] == "pending" and cli.calls == []


def test_timeout_holds_new_sessions_and_alerts():
    st, alerts = {}, []
    r = step(st, CLI(session=(None, "", "")), alerts=alerts)
    assert r["outcome"] == "unknown_outcome" and alerts
    cli = CLI()
    assert step(st, cli, now=NOW + 3600)["outcome"] == "hold_unknown" and cli.calls == []
    assert step(st, cli, now=NOW + fd.UNKNOWN_HOLD_SEC + 1)["outcome"] == "opened"


def test_failure_with_a_channel_is_tracked_until_resolved():
    err = json.dumps({"ok": False, "code": "SESSION_FAILED", "message": "miner acknowledgement timed out",
                      "channel": CH2, "recovery": "inspect finalized channel"})
    st = {}
    r = step(st, CLI(session=(1, "", err)))
    assert r["outcome"] == "error" and st[fd.KEY]["pending"][0]["channel"] == CH2
    assert mst(st)["wait_until"] == NOW + fd.ERROR_BACKOFF_SEC


def test_no_offer_backs_off_without_a_channel():
    err = json.dumps({"ok": False, "code": "SESSION_FAILED",
                      "message": "no live signed offer for the named miner and model within maxFlop", "channel": None})
    st = {}
    r = step(st, CLI(session=(1, "", err)))
    assert r["outcome"] == "no_offer" and st[fd.KEY].get("pending") == []


def test_unknown_settlement_outcome_holds_like_a_timeout():
    err = json.dumps({"ok": False, "code": "SESSION_FAILED",
                      "message": "cooperative settlement outcome unknown; inspect finalized history before recovery",
                      "channel": CH2})
    st, alerts = {}, []
    assert step(st, CLI(session=(1, "", err)), alerts=alerts)["outcome"] == "unknown_outcome" and alerts
    assert mst(st)["unknown_until"] == NOW + fd.UNKNOWN_HOLD_SEC


def test_unsupported_cli_option_stops_for_a_day():
    err = json.dumps({"ok": False, "code": "CLI_ERROR", "message": "error: unknown option '--via'"})
    st, alerts = {}, []
    assert step(st, CLI(session=(1, "", err)), alerts=alerts)["outcome"] == "unsupported" and alerts
    assert st[fd.KEY]["wait_until"] == NOW + fd.UNSUPPORTED_BACKOFF_SEC       # dừng CHUNG, mọi miner


def test_success_without_a_valid_channel_id_is_treated_as_unknown():
    st = {}
    r = step(st, CLI(session=(0, json.dumps({"ok": True, "channel": "nope"}), "")))
    assert r["outcome"] == "unknown_outcome" and mst(st)["unknown_until"] > NOW


def test_daily_escrow_cap(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "1")
    st, offers = {}, [offer(min_escrow=5 * 10 ** 17)]                       # 0.5 FLOP mỗi phiên theo offer
    for i in range(3):
        step(st, CLI(), explorer={CH: paid()}, now=NOW + i * 300, offers=offers)
        st[fd.KEY]["pending"] = []
    assert st[fd.KEY]["last_outcome"] == "daily_cap" and st[fd.KEY]["day"]["escrow"] == 1.0


# --- escrow theo offer miner công bố (escrow là chi phí) -----------------------------------------

def _max_flop(cli):
    run = cli.sessions()[0]["cmd"]
    return run[run.index("--max-flop") + 1]


def test_escrow_is_the_offers_min_escrow_when_not_set():
    cli = CLI()
    r = step({}, cli, offers=[offer(min_escrow=216 * 10 ** 15)])
    assert r["outcome"] == "opened" and _max_flop(cli) == "0.216"
    assert r["escrow"] == "0.216" and r["escrow_src"] == "offer"


def test_the_cheapest_offer_of_the_model_sets_the_escrow():
    cli = CLI()
    step({}, cli, offers=[offer(min_escrow=3 * 10 ** 17), offer(min_escrow=2 * 10 ** 17)])
    assert _max_flop(cli) == "0.2"


def test_exact_raw_amount_is_preserved(monkeypatch):
    cli = CLI()
    step({}, cli, offers=[offer(min_escrow=123456789012345678)])
    assert _max_flop(cli) == "0.123456789012345678"                    # đủ 18 chữ số lẻ, không làm tròn
    monkeypatch.setenv("FLOP_DIRECT_ESCROW_CAP", "2")
    monkeypatch.setenv("FLOP_DIRECT_MAX_OVERPAY", "0")                  # phiên 8 token: chỉ kiểm độ chính xác số
    cli = CLI()
    step({}, cli, offers=[offer(min_escrow=1050496031746020001)])       # kênh devnet thật: escrow 1.0505
    assert _max_flop(cli) == "1.050496031746020001"


def test_offer_above_the_escrow_cap_is_not_bought(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_OVERPAY", "0")                  # phiên 8 token: chỉ kiểm trần escrow
    monkeypatch.setenv("FLOP_DIRECT_ESCROW_CAP", "1")
    cli = CLI()
    r = step({}, cli, offers=[offer(min_escrow=2 * 10 ** 18)])          # 2 FLOP > trần mặc định 1
    assert r["outcome"] == "no_offer" and "FLOP_DIRECT_ESCROW_CAP" in r["reason"] and cli.calls == []
    monkeypatch.setenv("FLOP_DIRECT_ESCROW_CAP", "3")
    assert step({}, CLI(), offers=[offer(min_escrow=2 * 10 ** 18)])["outcome"] == "opened"
    monkeypatch.setenv("FLOP_DIRECT_ESCROW_CAP", "5000")                 # trần cứng 200 vẫn áp
    assert step({}, CLI(), offers=[offer(min_escrow=300 * 10 ** 18)])["outcome"] == "no_offer"


def test_no_offer_means_no_cli_call():
    cli, st = CLI(), {}
    r = step(st, cli, offers=[])
    assert r["outcome"] == "no_offer" and cli.calls == [] and mst(st)["wait_until"] == NOW + fd.ERROR_BACKOFF_SEC


@pytest.mark.parametrize("bad", [
    offer(miner=OTHER),                                  # miner khác
    offer(forward={"price": "1"}),                       # offer forward (không phải spot)
    offer(max_turns=0),
    offer(min_escrow=-1),
    offer(model_hash="0xnothex"),
    {"offer": "garbage"}, "garbage", None,
])
def test_unusable_offer_rows_are_ignored(bad):
    cli = CLI()
    assert step({}, cli, offers=[bad])["outcome"] == "no_offer" and cli.calls == []


def test_several_models_need_an_explicit_model_hash(monkeypatch):
    rows = [offer(min_escrow=10 ** 17, model_hash=MH), offer(min_escrow=4 * 10 ** 17, model_hash=MH2)]
    cli = CLI()
    r = step({}, cli, offers=rows)
    assert r["outcome"] == "no_offer" and "FLOP_DIRECT_MODEL_HASH" in r["reason"] and cli.calls == []
    monkeypatch.setenv("FLOP_DIRECT_MODEL_HASH", MH2)
    cli = CLI()
    assert step({}, cli, offers=rows)["outcome"] == "opened" and _max_flop(cli) == "0.4"


def test_model_hash_without_a_matching_offer(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MODEL_HASH", MH2)
    cli = CLI()
    assert step({}, cli, offers=[offer(model_hash=MH)])["outcome"] == "no_offer" and cli.calls == []


def test_bad_model_hash_setting(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MODEL_HASH", "abc")
    assert step({}, CLI())["outcome"] == "unconfigured"


def test_fixed_escrow_below_the_offer_minimum_is_refused_before_the_cli(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_FLOP", "0.05")
    cli = CLI()
    r = step({}, cli, offers=[offer(min_escrow=2 * 10 ** 17)])
    assert r["outcome"] == "no_offer" and "minEscrow" in r["reason"] and cli.calls == []


def test_fixed_escrow_at_or_above_the_offer_minimum_is_used(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_FLOP", "0.3")
    cli = CLI()
    r = step({}, cli, offers=[offer(min_escrow=2 * 10 ** 17)])
    assert r["outcome"] == "opened" and _max_flop(cli) == "0.3" and r["escrow_src"] == "fixed"


def test_offer_lookup_failure_backs_off_briefly():
    def boom(miner, mh):
        raise RuntimeError("explorer down")
    cli, st = CLI(), {}
    r = step(st, cli, offers_fn=boom)
    assert r["outcome"] == "offer_lookup_error" and cli.calls == [] and mst(st)["wait_until"] == NOW + 900


def test_force_mode_with_fixed_escrow_does_not_need_offers(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_VIA", "force")
    monkeypatch.setenv("FLOP_DIRECT_ALLOW_FORCE", "on")
    monkeypatch.setenv("FLOP_DIRECT_MAX_FLOP", "0.2")
    cli = CLI()
    assert step({}, cli, offers=[])["outcome"] == "opened" and _max_flop(cli) == "0.2"


def test_without_offers_or_fixed_escrow_nothing_runs():
    cli = CLI()
    r = fd.run_step({}, agent_key=KEY, now=NOW, prompt="p", network="testnet", fetch_channel=lambda c: None,
                    runner=cli, log=lambda m: None)
    assert r["outcome"] == "unconfigured" and cli.calls == []


def test_not_enough_run_time_skips(monkeypatch):
    cli = CLI()
    r = step({}, cli, deadline=1000.0, clock=lambda: 900.0)
    assert r["outcome"] == "run_budget" and cli.calls == []


def test_last_json_reads_the_cli_agent_line():
    assert fd.last_json('banner\n{"ok":true,"channel":"0x1"}\n') == {"ok": True, "channel": "0x1"}
    assert fd.last_json("not json") is None


# --- nối vào agent_cron -------------------------------------------------------------------

def test_agent_step_wires_explorer_prompts_and_booking(monkeypatch):
    monkeypatch.setenv("FLOP_GATEWAY_PROMPTS", "q1|q2")
    cli = CLI()
    monkeypatch.setattr(fd, "_run", cli)
    seen = []

    class _R:
        status_code = 200

        def __init__(self, data):
            self._d = data

        def json(self):
            return {"data": self._d}

        def raise_for_status(self):
            return None

    def get(url, **k):
        from urllib.parse import urlencode
        seen.append(url + ("?" + urlencode(k["params"]) if k.get("params") else ""))
        if "/api/session-offers" in url:
            class _O(_R):
                def json(self):
                    return {"data": [offer()], "pagination": {"total": 1}}
            return _O(None)
        return _R(paid())

    monkeypatch.setattr(ac.requests, "get", get)
    monkeypatch.setattr(ac, "kv_set", lambda *a, **k: True)
    monkeypatch.setattr(ac, "_telegram_send", lambda m: True)
    st = {}
    assert ac.direct_session_step(st, KEY, NOW) == "opened"
    assert "q1" in cli.sessions()[0]["cmd"][cli.sessions()[0]["cmd"].index("--prompt") + 1]
    status = ac.direct_session_step(st, KEY, NOW + 300)
    assert status.startswith("pending") or status.startswith("opened")
    assert any(u.endswith(f"/api/compute-channels/{CH}") and "explorer.testnet.flop.network" in u for u in seen)
    assert any("/api/session-offers?" in u and f"miner={MINER}" in u for u in seen)
    assert tm.load_ledger()["session_cumulative"]["FLOP"] == "0.021"


def test_agent_step_is_off_by_default(monkeypatch):
    monkeypatch.delenv("FLOP_DIRECT_ENABLED")
    assert ac.direct_session_step({}, KEY, NOW) == "off"


# --- nhiều phiên mỗi lượt + nhiều miner ------------------------------------------------------

M2 = fg.ss58_encode(bytes([5]) * 32)
M3 = fg.ss58_encode(bytes([6]) * 32)


class MultiCLI(CLI):
    """CLI giả: mỗi phiên một kênh mới; `by_miner` = kịch bản riêng cho từng miner (mặc định thành công)."""

    def __init__(self, by_miner=None, clock=None, cost=0):
        super().__init__()
        self.by_miner, self.n, self.clock, self.cost = by_miner or {}, 0, clock, cost

    def __call__(self, cmd, env, timeout):
        if "session" not in cmd:
            return super().__call__(cmd, env, timeout)
        self.calls.append({"cmd": list(cmd), "env": dict(env), "timeout": timeout})
        if self.clock is not None:
            self.clock[0] += self.cost
        miner = cmd[cmd.index("--miner") + 1]
        if miner in self.by_miner:
            return self.by_miner[miner]
        self.n += 1
        return 0, json.dumps({"ok": True, "channel": "0x" + f"{self.n:064x}", "settle_tx": "0x01"}), ""

    def miners(self):
        return [c["cmd"][c["cmd"].index("--miner") + 1] for c in self.sessions()]


def _offers_for(*miners):
    return lambda miner, mh: [offer(miner=miner, model_hash=mh or MH)] if miner in miners else []


@pytest.fixture
def three_miners(monkeypatch):
    monkeypatch.delenv("FLOP_DIRECT_MINER")
    monkeypatch.delenv("FLOP_DIRECT_ENDPOINT")
    monkeypatch.setenv("FLOP_DIRECT_MINERS", f"{MINER} https://m1.example\n{M2} https://m2.example qwen/qwen3-14b; "
                                             f"{M3} https://m3.example qwen/qwen3-8b {MH2}")


def test_several_sessions_per_run_with_one_keystore_and_distinct_prompts(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "4")
    cli, st = MultiCLI(), {}
    r = step(st, cli, prompts=["p0", "p1", "p2", "p3", "p4"])
    assert r["outcome"] == "opened" and r["opened"] == 4 and len(st[fd.KEY]["pending"]) == 4
    assert sum(1 for c in cli.calls if "import" in c["cmd"]) == 1         # keystore nạp MỘT lần
    prompts = [c["cmd"][c["cmd"].index("--prompt") + 1] for c in cli.sessions()]
    assert prompts == ["p0", "p1", "p2", "p3"]
    step(st, MultiCLI(), prompts=["p0", "p1", "p2", "p3", "p4"], now=NOW + 300)
    assert st[fd.KEY]["prompt_idx"] == 8


def test_sessions_stop_when_the_run_budget_runs_out(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "10")
    monkeypatch.setenv("FLOP_DIRECT_TIMEOUT_SEC", "100")
    clock = [0.0]
    cli = MultiCLI(clock=clock, cost=100)
    no_dur = offer()
    no_dur["offer"]["maxDuration"] = "0"                                  # không có thời hạn -> timeout theo cấu hình
    r = step({}, cli, deadline=450.0, clock=lambda: clock[0], offers=[no_dur])
    assert r["opened"] == 4 and "thời gian" in r["reason"]              # bắt đầu ở 0..300 ok; 400+130 > 450


def test_daily_cap_stops_mid_run(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "10")
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "0.12")
    st = {}
    r = step(st, MultiCLI())                                             # offer 0.05 FLOP mỗi phiên
    assert r["opened"] == 2 and "trần" in r["reason"] and st[fd.KEY]["day"]["escrow"] == pytest.approx(0.1)


def test_miners_list_is_parsed_and_rotated(monkeypatch, three_miners):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "4")
    cli, st = MultiCLI(), {}
    step(st, cli, offers_fn=_offers_for(MINER, M2, M3))
    assert cli.miners() == [MINER, M2, M3, MINER]
    s = {c["cmd"][c["cmd"].index("--miner") + 1]: c["cmd"] for c in cli.sessions()}
    assert s[M2][s[M2].index("--endpoint") + 1] == "https://m2.example"
    assert s[M2][s[M2].index("--model") + 1] == "qwen/qwen3-14b"
    assert s[MINER][s[MINER].index("--model") + 1] == "qwen/qwen2.5-0.5b-instruct"   # mặc định FLOP_DIRECT_MODEL
    cli2 = MultiCLI()
    step(st, cli2, offers_fn=_offers_for(MINER, M2, M3), now=NOW + 300)
    assert cli2.miners()[0] == M2                                        # lượt sau tiếp vòng xoay


def test_miner_entry_model_hash_is_used_for_its_offers(monkeypatch, three_miners):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "3")
    seen = []

    def offers_fn(miner, mh):
        seen.append((miner, mh))
        return [offer(miner=miner, model_hash=mh or MH)]

    step({}, MultiCLI(), offers_fn=offers_fn)
    assert (M3, MH2) in seen


def test_a_miner_without_offers_is_skipped_for_the_others(monkeypatch, three_miners):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "3")
    cli, st = MultiCLI(), {}
    r = step(st, cli, offers_fn=_offers_for(M2, M3))
    assert set(cli.miners()) == {M2, M3} and MINER not in cli.miners() and r["opened"] == 3
    assert mst(st, MINER)["wait_until"] == NOW + fd.ERROR_BACKOFF_SEC


def test_offers_are_looked_up_once_per_miner_per_run(monkeypatch, three_miners):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "6")
    calls = []

    def offers_fn(miner, mh):
        calls.append(miner)
        return [offer(miner=miner, model_hash=mh or MH)]

    step({}, MultiCLI(), offers_fn=offers_fn)
    assert sorted(calls) == sorted([MINER, M2, M3])


def test_a_timed_out_miner_is_held_while_the_others_continue(monkeypatch, three_miners):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "3")
    cli, st, alerts = MultiCLI(by_miner={M2: (None, "", "")}), {}, []
    r = step(st, cli, offers_fn=_offers_for(MINER, M2, M3), alerts=alerts)
    assert r["opened"] == 3 and cli.miners().count(M2) == 1 and alerts
    assert mst(st, M2)["unknown_until"] == NOW + fd.UNKNOWN_HOLD_SEC
    cli2 = MultiCLI()
    step(st, cli2, offers_fn=_offers_for(MINER, M2, M3), now=NOW + 300)
    assert M2 not in cli2.miners()


def test_a_failed_channel_blocks_only_that_miner(monkeypatch, three_miners):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "3")
    err = json.dumps({"ok": False, "message": "miner acknowledgement timed out", "channel": CH2})
    st = {}
    step(st, MultiCLI(by_miner={M3: (1, "", err)}), offers_fn=_offers_for(MINER, M2, M3))
    for v in st[fd.KEY]["miners"].values():
        v["wait_until"] = 0
    cli = MultiCLI()
    step(st, cli, offers_fn=_offers_for(MINER, M2, M3), now=NOW + 300)
    assert M3 not in cli.miners() and set(cli.miners()) == {MINER, M2}


def test_unsupported_option_stops_every_miner(monkeypatch, three_miners):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "3")
    err = json.dumps({"ok": False, "message": "error: unknown option '--via'"})
    cli = MultiCLI(by_miner={MINER: (1, "", err)})
    st = {}
    r = step(st, cli, offers_fn=_offers_for(MINER, M2, M3))
    assert r["outcome"] == "unsupported" and cli.miners() == [MINER]
    assert step(st, MultiCLI(), offers_fn=_offers_for(MINER, M2, M3), now=NOW + 300)["outcome"] == "backoff"


def test_own_wallet_and_denied_entries_are_dropped_but_others_used(monkeypatch):
    monkeypatch.delenv("FLOP_DIRECT_MINER")
    monkeypatch.delenv("FLOP_DIRECT_ENDPOINT")
    own = fg.ss58_encode(fg._raw_pub(KEY), prefix=0)
    monkeypatch.setenv("FLOP_DIRECT_MINERS", f"{own} https://a.example, {OTHER} https://b.example, {M2} https://c.example")
    monkeypatch.setenv("FLOP_DIRECT_DENY_MINERS", OTHER)
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "2")
    cli = MultiCLI()
    r = step({}, cli, offers_fn=_offers_for(own, OTHER, M2))
    assert set(cli.miners()) == {M2} and r["opened"] == 2


def test_duplicate_miner_under_another_prefix_is_listed_once(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MINERS", f"{MINER} https://a.example; "
                                             f"{fg.ss58_encode(bytes([7]) * 32, prefix=0)} https://b.example")
    cfg, errs = fd.config()
    assert errs == [] and len(cfg["miners"]) == 1 and cfg["miners"][0]["endpoint"] == "https://a.example"


@pytest.mark.parametrize("raw,why", [
    ("5notanaddress https://a.example", "SS58"),
    (f"{MINER}", "cần"),
    (f"{MINER} http://a.example", "https://"),
    (f"{MINER} https://a.example qwen!model", "ký tự lạ"),
    (f"{MINER} https://a.example qwen 0xnothex", "hash"),
])
def test_bad_miner_entries_are_reported(monkeypatch, raw, why):
    monkeypatch.setenv("FLOP_DIRECT_MINERS", raw)
    cli = MultiCLI()
    r = step({}, cli)
    assert r["outcome"] == "unconfigured" and why in r["reason"] and cli.calls == []


# --- định cỡ phiên theo việc thật (giá × GFLOP, công suất miner, học từ kênh đã settle) ------------

from decimal import Decimal  # noqa: E402

LONG = "Explain the trade-offs of optimistic versus pessimistic locking in distributed databases. " * 4


def _plan(**kw):
    base = dict(prompt=LONG, max_tokens=1024, gn_per_token=28.0, cap_gfs=130.0, price=Decimal("0.003"),
                min_escrow=Decimal("0.05"), escrow_cap=Decimal("100"), settle_target=300, margin=0.2)
    base.update(kw)
    return fd.plan_session(**base)


def test_big_model_on_a_strong_miner_is_sized_up_to_the_escrow_cap():
    plan, why = _plan()
    ptok = len(LONG) // 4
    unit = Decimal("0.003") * Decimal("28") * Decimal("1.2")
    assert plan["max_tokens"] == int(Decimal(100) / unit) - ptok            # trần escrow cắt trước thời gian
    assert Decimal(plan["escrow"]) <= Decimal(100) and Decimal(plan["escrow"]) > Decimal(95)
    assert plan["gn_est"] == pytest.approx(28.0 * (ptok + plan["max_tokens"]))


def test_the_settle_time_bounds_the_session_on_a_weaker_miner():
    plan, _ = _plan(cap_gfs=32.0)
    ptok = len(LONG) // 4
    assert plan["max_tokens"] == int(32.0 * 300 / 28.0) - ptok
    assert plan["gn_est"] <= 32.0 * 300 + 1e-6                              # settle trong ~300 s


def test_offer_max_duration_shortens_the_window():
    plan, _ = _plan(cap_gfs=130.0, max_duration=60)
    assert plan["gn_est"] <= 130.0 * 60 + 1e-6


def test_a_miner_too_weak_for_the_model_is_skipped():
    plan, why = _plan(gn_per_token=54.0, cap_gfs=10.0)
    assert plan is None and "GFLOP/s" in why


def test_escrow_never_goes_below_the_offer_minimum():
    plan, _ = _plan(prompt="hi", max_tokens=16, gn_per_token=1.0, min_escrow=Decimal("0.5"))
    assert plan["escrow"] == "0.5"


def test_offer_minimum_above_the_cap_is_refused():
    plan, why = _plan(min_escrow=Decimal("150"))
    assert plan is None and "FLOP_DIRECT_ESCROW_CAP" in why


def test_cap_too_small_for_even_the_minimum_session():
    plan, why = _plan(escrow_cap=Decimal("0.5"))
    assert plan is None and "trần" in why


@pytest.mark.parametrize("slug,size", [("Qwen/Qwen3-14B", 14.0), ("qwen/qwen2.5-0.5b-instruct", 0.5),
                                       ("NousResearch/Arete-27B", 27.0), ("Qwen/Qwen3.8-27B", 27.0),
                                       ("flop-ops-demo", None), ("llama-3-70b-instruct", 70.0)])
def test_model_size_from_slug(slug, size):
    assert fd.model_size_b(slug) == size


def test_session_uses_the_planned_tokens_and_escrow(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_TOKENS", "1024")
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "2000")
    monkeypatch.setenv("FLOP_DIRECT_MODEL", "Qwen/Qwen3-14B")
    cli = CLI()
    r = step({}, cli, prompt=LONG, miner_cap_fn=lambda m: 130.0, price_fn=lambda mh: Decimal("0.003"))
    run = cli.sessions()[0]["cmd"]
    tok, esc = int(run[run.index("--max-tokens") + 1]), Decimal(run[run.index("--max-flop") + 1])
    assert r["outcome"] == "opened" and 800 < tok <= 1024 and Decimal(95) < esc <= Decimal(100)


def test_unknown_miner_capacity_falls_back_to_a_small_safe_session(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_TOKENS", "1024")
    monkeypatch.setenv("FLOP_DIRECT_MODEL", "Qwen/Qwen3-14B")
    cli = CLI()
    step({}, cli, prompt=LONG)                                               # không biết cap -> 10 GFLOP/s
    run = cli.sessions()[0]["cmd"]
    assert int(run[run.index("--max-tokens") + 1]) == int(10 * 300 / 28.0) - len(LONG) // 4


def test_the_rest_of_the_daily_cap_shrinks_the_last_session(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_TOKENS", "1024")
    monkeypatch.setenv("FLOP_DIRECT_MODEL", "Qwen/Qwen3-14B")
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "5")
    st, cli = {}, MultiCLI()
    r = step(st, cli, prompt=LONG, miner_cap_fn=lambda m: 130.0)            # ~100 FLOP/phiên > trần ngày 25
    esc = Decimal(cli.sessions()[0]["cmd"][cli.sessions()[0]["cmd"].index("--max-flop") + 1])
    assert r["opened"] == 1 and Decimal(24) < esc <= Decimal(25)            # thu cho vừa 25, không bỏ hẳn
    assert r["reason"] and "trần" in r["reason"]
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "2000")
    r = step({}, MultiCLI(), prompt=LONG, miner_cap_fn=lambda m: 130.0)
    assert r["opened"] == 5


def test_settled_channels_teach_gflops_per_token_and_miner_capacity(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_TOKENS", "1024")
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "2000")
    monkeypatch.setenv("FLOP_DIRECT_MODEL", "Qwen/Qwen3-14B")
    st = {}
    cli = CLI(session=(0, json.dumps({"ok": True, "channel": CH, "completion_tokens": "400"}), ""))
    step(st, cli, prompt=LONG, miner_cap_fn=lambda m: 130.0)
    ptok = len(LONG) // 4
    info = {**paid(), "aggregateGn": str(20 * (ptok + 400)), "capGflopsPerSec": 60}
    nxt = CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), ""))
    step(st, nxt, prompt=LONG, explorer={CH: info}, now=NOW + 300, miner_cap_fn=lambda m: 130.0)
    assert st[fd.KEY]["gnpt"][MH] == pytest.approx(20.0)                     # GFLOP/token đo được
    assert mst(st)["cap"] == 60.0                                            # công suất thật thay ước lượng
    run = nxt.sessions()[0]["cmd"]
    assert int(run[run.index("--max-tokens") + 1]) == int(60 * 300 / 20.0) - ptok


def test_learning_is_smoothed():
    g, ms = {"gnpt": {MH: 10.0}}, {}
    fd._learn(g, ms, {"model_hash": MH, "ptok": 10, "completion_tokens": 90, "miner": MINER},
              {"aggregateGn": "3000", "capGflopsPerSec": 50})
    assert g["gnpt"][MH] == pytest.approx(10 * 0.5 + 30 * 0.5) and ms[fd._mkey(MINER)]["cap"] == 50.0


def test_learning_ignores_bad_numbers():
    g, ms = {}, {}
    fd._learn(g, ms, {"model_hash": MH, "ptok": 0, "completion_tokens": None, "miner": MINER},
              {"aggregateGn": "abc", "capGflopsPerSec": "x"})
    assert g == {} and ms == {}


def test_price_from_the_registry_and_override(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_TOKENS", "64")
    cli = CLI()
    step({}, cli, prompt="hi there", price_fn=lambda mh: Decimal("0.03"))   # giá x10
    esc_registry = Decimal(cli.sessions()[0]["cmd"][cli.sessions()[0]["cmd"].index("--max-flop") + 1])
    monkeypatch.setenv("FLOP_DIRECT_PRICE_PER_GN", "0.003")
    cli = CLI()
    step({}, cli, prompt="hi there", price_fn=lambda mh: Decimal("0.03"))   # biến môi trường thắng
    esc_env = Decimal(cli.sessions()[0]["cmd"][cli.sessions()[0]["cmd"].index("--max-flop") + 1])
    assert esc_registry == pytest.approx(esc_env * 10, rel=0.01)


def test_capacity_lookup_failure_uses_the_safe_default(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_TOKENS", "1024")
    monkeypatch.setenv("FLOP_DIRECT_MODEL", "Qwen/Qwen3-14B")

    def boom(miner):
        raise RuntimeError("explorer down")

    cli = CLI()
    assert step({}, cli, prompt=LONG, miner_cap_fn=boom)["outcome"] == "opened"
    run = cli.sessions()[0]["cmd"]
    assert int(run[run.index("--max-tokens") + 1]) == int(10 * 300 / 28.0) - len(LONG) // 4


# --- lỗi rõ ràng trước khi mở kênh không được "ăn" trần ngày -------------------------------------------------

def test_a_clean_refusal_before_any_channel_releases_the_daily_reservation(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "0.12")
    err = json.dumps({"ok": False, "code": "INSUFFICIENT_FUNDS", "message": "insufficient balance"})
    st = {}
    r = step(st, CLI(session=(1, "", err)))
    assert r["outcome"] == "error" and st[fd.KEY]["day"]["escrow"] == 0.0


@pytest.mark.parametrize("session", [
    (1, "", json.dumps({"ok": False, "message": "miner acknowledgement timed out", "channel": CH2})),  # có kênh
    (1, "", json.dumps({"ok": False, "message": "session outcome unknown"})),                        # mơ hồ
    (1, "", "Segmentation fault"),                                                                    # không JSON
    (None, "", ""),                                                                                   # quá giờ
])
def test_ambiguous_or_channel_failures_keep_the_daily_reservation(session):
    st = {}
    step(st, CLI(session=session))
    assert st[fd.KEY]["day"]["escrow"] == pytest.approx(0.05)


# --- trải đều trong ngày (đường cộng dồn, đuổi kịp khi lượt lỗi) -----------------------------------------------

DAY_START = NOW - NOW % 86400                                                  # NOW = 08:00 UTC


def _pcfg(**kw):
    base = {"per_day": 2000.0, "pace": True, "pace_window": 20.0, "pace_jitter": 0.0}
    base.update(kw)
    return base


def test_pace_limit_is_a_cumulative_line_reaching_the_full_cap_before_day_end():
    assert fd.pace_limit(_pcfg(), DAY_START) == (pytest.approx(100.0), True)            # 00:00 -> 1/20
    assert fd.pace_limit(_pcfg(), DAY_START + 8 * 3600) == (pytest.approx(900.0), True)
    assert fd.pace_limit(_pcfg(), DAY_START + 19 * 3600) == (2000.0, False)             # hết cửa sổ: cả trần
    assert fd.pace_limit(_pcfg(), DAY_START + 23 * 3600 + 3599) == (2000.0, False)
    assert fd.pace_limit(_pcfg(pace=False), DAY_START) == (2000.0, False)
    assert fd.pace_limit(_pcfg(per_day=0.0), DAY_START) == (0.0, False)


def test_pace_jitter_is_bounded_and_stable_within_the_hour():
    ratios = {round(fd.pace_limit(_pcfg(pace_jitter=0.2), DAY_START + 8 * 3600 + s, salt="w")[0]
                    / fd.pace_limit(_pcfg(), DAY_START + 8 * 3600 + s)[0], 9) for s in (0, 900, 3599)}
    assert len(ratios) == 1 and 0.8 - 1e-9 <= ratios.pop() <= 1.2 + 1e-9                # cùng hệ số cả giờ
    other_hours = {round(fd.pace_limit(_pcfg(pace_jitter=0.2), DAY_START + h * 3600, salt="w")[0]
                         / ((h + 1) / 20 * 2000), 6) for h in range(15)}
    assert len(other_hours) > 1                                                         # thật sự lệch theo giờ


def test_pace_is_on_by_default(monkeypatch):
    monkeypatch.delenv("FLOP_DIRECT_PACE")
    cfg, errs = fd.config()
    assert errs == [] and cfg["pace"] is True and cfg["pace_window"] == 20 and cfg["pace_jitter"] == 0.2


@pytest.fixture
def paced_big(monkeypatch):
    for k, v in {"FLOP_DIRECT_PACE": "on", "FLOP_DIRECT_PACE_JITTER_PCT": "0", "FLOP_DIRECT_MAX_PER_DAY": "2000",
                 "FLOP_DIRECT_MAX_TOKENS": "1024", "FLOP_DIRECT_MODEL": "Qwen/Qwen3-14B",
                 "FLOP_DIRECT_MAX_PER_RUN": "10"}.items():
        monkeypatch.setenv(k, v)


def _day_state(escrow):
    return {fd.KEY: {"day": {"day": time.strftime("%Y-%m-%d", time.gmtime(NOW)), "escrow": escrow}}}


def test_behind_schedule_catches_up_within_the_run(paced_big):
    cli = MultiCLI()
    st = _day_state(300.0)                                                              # 08:00: được 900
    r = step(st, cli, prompt=LONG, miner_cap_fn=lambda m: 130.0)
    assert r["opened"] == 6 and st[fd.KEY]["day"]["escrow"] == pytest.approx(900.0, abs=1)


def test_on_schedule_waits_and_says_why(paced_big):
    cli = MultiCLI()
    r = step(_day_state(860.0), cli, prompt=LONG, miner_cap_fn=lambda m: 130.0)        # còn 40 < phần giờ 83
    assert r["outcome"] == "paced" and cli.sessions() == [] and "trải đều" in r["reason"]


def test_after_the_window_the_remainder_is_spent_even_in_a_small_session(paced_big):
    cli = MultiCLI()
    st = _day_state(1960.0)
    r = step(st, cli, prompt=LONG, miner_cap_fn=lambda m: 130.0, now=DAY_START + 20 * 3600)
    assert r["opened"] == 1 and st[fd.KEY]["day"]["escrow"] == pytest.approx(2000.0, abs=0.5)


def test_failed_runs_lose_nothing_over_a_day(paced_big):
    """Mô phỏng 1 ngày, lượt 5 phút; hỏng 6 giờ liền (02:00–08:00) + cứ 3 lượt hỏng 1 -> vẫn đủ 2000."""
    class Settled:                                                                      # mọi kênh settle kịp
        def get(self, cid):
            return paid()

    st, opened, cli = {}, 0, MultiCLI()
    for i in range(288):
        t = DAY_START + i * 300
        hour = (t - DAY_START) // 3600
        if 2 <= hour < 8 or i % 3 == 0:
            continue
        r = step(st, cli, explorer=Settled(), prompt=LONG, miner_cap_fn=lambda m: 130.0, now=t)
        opened += r.get("opened", 0)
        assert st[fd.KEY]["day"]["escrow"] <= fd.pace_limit(fd.config()[0], t)[0] + 1e-6
    assert st[fd.KEY]["day"]["escrow"] == pytest.approx(2000.0, abs=0.5) and opened <= 30


# --- audit 2026-11-13: 6 mục -------------------------------------------------------------------------------------

def test_learning_skips_gflops_per_token_without_completion_tokens():
    g, ms = {"gnpt": {MH: 28.0}}, {}
    fd._learn(g, ms, {"model_hash": MH, "ptok": 80, "miner": MINER},             # CLI không trả completion_tokens
              {"aggregateGn": str(28 * 1080), "capGflopsPerSec": 90})
    assert g["gnpt"][MH] == 28.0 and ms[fd._mkey(MINER)]["cap"] == 90.0          # công suất vẫn học


def test_missing_completion_tokens_does_not_stop_later_sessions(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "2000")
    monkeypatch.setenv("FLOP_DIRECT_MAX_TOKENS", "1024")
    monkeypatch.setenv("FLOP_DIRECT_MODEL", "Qwen/Qwen3-14B")
    st = {}
    step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH}), "")), prompt=LONG,
         miner_cap_fn=lambda m: 130.0)
    info = {**paid(), "aggregateGn": str(28 * (len(LONG) // 4 + 1000)), "capGflopsPerSec": 130}
    nxt = CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), ""))
    r = step(st, nxt, prompt=LONG, explorer={CH: info}, now=NOW + 300, miner_cap_fn=lambda m: 130.0)
    run = nxt.sessions()[0]["cmd"]
    assert r["outcome"] == "opened" and int(run[run.index("--max-tokens") + 1]) > 800


def test_zero_min_escrow_offer_is_usable():
    cli = CLI()
    r = step({}, cli, offers=[offer(min_escrow=0)])
    run = cli.sessions()[0]["cmd"]
    assert r["outcome"] == "opened" and Decimal(run[run.index("--max-flop") + 1]) > 0


def test_docker_containers_are_named_per_call():
    cli = CLI()
    step({}, cli)
    names = [c["cmd"][c["cmd"].index("--name") + 1] for c in cli.calls]
    assert len(names) == 2 and len(set(names)) == 2 and all(n.startswith("flop-direct-") for n in names)
    for c in cli.calls:                                                       # tên nằm TRƯỚC image
        assert c["cmd"].index("--name") < c["cmd"].index(fd.DEFAULT_IMAGE)


def test_timeout_kills_the_named_container(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["docker", "run"]:
            raise fd.subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return fd.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(fd.subprocess, "run", fake_run)
    cmd = ["docker", "run", "--rm", "--name", "flop-direct-abc", "img", "session", "run"]
    assert fd._run(cmd, {}, 5) == (None, "", "")
    assert calls[-1] == ["docker", "kill", "flop-direct-abc"]
    calls.clear()
    assert fd._run(["node", "cli.js"], {}, 5) == (0, "", "")                  # lệnh cục bộ: không gọi docker kill
    assert ["docker", "kill"] not in [c[:2] for c in calls]


def test_balance_shrinks_the_session_to_what_the_wallet_holds(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "2000")
    monkeypatch.setenv("FLOP_DIRECT_MAX_TOKENS", "1024")
    monkeypatch.setenv("FLOP_DIRECT_MODEL", "Qwen/Qwen3-14B")
    cli = CLI()
    r = step({}, cli, prompt=LONG, miner_cap_fn=lambda m: 130.0, balance_fn=lambda a: Decimal("40.5"))
    run = cli.sessions()[0]["cmd"]
    assert r["outcome"] == "opened" and Decimal(run[run.index("--max-flop") + 1]) <= Decimal(40)
    assert Decimal(run[run.index("--max-flop") + 1]) > Decimal(38)


def test_low_balance_stops_without_calling_the_cli_or_backing_off():
    cli, st = CLI(), {}
    r = step(st, cli, balance_fn=lambda a: Decimal("0.3"))                   # < phí chừa 0.5
    assert r["outcome"] == "low_balance" and cli.calls == [] and not mst(st).get("wait_until")
    r = step(st, CLI(), balance_fn=lambda a: Decimal("0.52"), offers=[offer(min_escrow=5 * 10 ** 16)])
    assert r["outcome"] == "low_balance"                                       # còn 0.02 < minEscrow 0.05


def test_balance_is_drawn_down_across_sessions_in_one_run(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "10")
    monkeypatch.setenv("FLOP_DIRECT_MAX_FLOP", "0.2")
    cli = MultiCLI()
    r = step({}, cli, balance_fn=lambda a: Decimal("1.15"))                    # 0.65 khả dụng -> 3 phiên 0.2
    assert r["opened"] == 3 and r["outcome"] == "opened" and "số dư" in r["reason"]


def test_balance_read_failure_keeps_the_old_behaviour():
    def boom(addr):
        raise RuntimeError("explorer down")

    cli = CLI()
    assert step({}, cli, balance_fn=boom)["outcome"] == "opened"


def test_fee_reserve_is_configurable(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_FEE_RESERVE", "0")
    assert step({}, CLI(), balance_fn=lambda a: Decimal("0.3"))["outcome"] == "opened"
    monkeypatch.setenv("FLOP_DIRECT_FEE_RESERVE", "-1")
    assert step({}, CLI())["outcome"] == "unconfigured"


def _rich_state(n_ok=30, n_err=3):
    pend = [{"channel": "0x" + f"{i:064x}", "ts": NOW, "network": "testnet", "miner": MINER, "model": "m",
             "model_hash": MH, "ptok": 10, "completion_tokens": 100, "max_flop": "99.5", "settle_tx": "0x" + "ab" * 32}
            for i in range(n_ok)]
    pend += [{"channel": "0x" + f"{1000 + i:064x}", "ts": NOW, "network": "testnet", "miner": M2,
              "error": "miner acknowledgement timed out " * 5} for i in range(n_err)]
    return {fd.KEY: {"day": {"day": "2027-01-15", "escrow": 900.0}, "unknown_until": NOW + 99,
                     "miners": {fd._mkey(MINER): {"wait_until": NOW + 5, "cap": 130.0, "learned_junk": 1}},
                     "gnpt": {MH: 28.0}, "stats": {"paid": 4}, "pending": pend, "last_outcome": "opened"}}


def test_durable_view_round_trips_the_safety_state():
    st = _rich_state(3, 1)
    view = fd.durable_view(st[fd.KEY])
    new = {}
    assert fd.restore(new, json.loads(json.dumps(view)))
    g = new[fd.KEY]
    assert g["day"]["escrow"] == 900.0 and g["unknown_until"] == NOW + 99 and g["gnpt"] == {MH: 28.0}
    assert g["miners"][fd._mkey(MINER)] == {"wait_until": NOW + 5, "cap": 130.0}
    assert [p["channel"] for p in g["pending"]] == [p["channel"] for p in st[fd.KEY]["pending"]]
    assert "settle_tx" not in g["pending"][0] and g["pending"][-1]["error"]


def test_restore_never_overrides_local_state():
    st = {fd.KEY: {"day": {"day": "x", "escrow": 1.0}}}
    assert not fd.restore(st, {"day": {"day": "x", "escrow": 999.0}}) and st[fd.KEY]["day"]["escrow"] == 1.0
    assert not fd.restore({}, "garbage")


def test_durable_view_trims_oldest_ok_pending_but_keeps_errors():
    g = _rich_state(30, 3)[fd.KEY]
    v = fd.durable_view(g, level=3)                                           # bỏ 10 kênh cũ nhất không lỗi
    assert len(v["pending"]) == 23 and sum(1 for p in v["pending"] if p.get("error")) == 3
    assert v["pending"][0]["channel"] == g["pending"][10]["channel"] and "model_hash" not in v["pending"][0]
    assert "stats" not in v


def test_agent_persists_direct_state_to_its_own_kv_key_within_the_limit(monkeypatch):
    writes = []
    monkeypatch.setattr(ac, "kv_set", lambda pk, did, key, val: writes.append((key, val)) or True)
    st = _rich_state(40, 3)
    assert ac.direct_kv_persist(st, KEY)
    assert len(writes) == 1 and writes[0][0] == ac.DIRECT_KV_KEY and len(writes[0][1]) <= ac.KV_VALUE_MAX
    body = ac._kv_open(writes[0][1], KEY)
    assert body["day"]["escrow"] == 900.0 and sum(1 for p in body["pending"] if p.get("error")) == 3
    assert ac.direct_kv_persist(st, KEY) and len(writes) == 1                # không đổi -> không ghi lại
    st[fd.KEY]["day"]["escrow"] = 1000.0
    assert ac.direct_kv_persist(st, KEY) and len(writes) == 2


def test_failed_kv_write_is_retried_next_run(monkeypatch):
    ok = [False]
    monkeypatch.setattr(ac, "kv_set", lambda *a: ok[0])
    st = _rich_state(2, 0)
    assert not ac.direct_kv_persist(st, KEY) and ac.DIRECT_KV_HASH not in st
    ok[0] = True
    assert ac.direct_kv_persist(st, KEY) and st.get(ac.DIRECT_KV_HASH)


def test_agent_restores_direct_state_from_kv_after_cache_loss(monkeypatch):
    sealed = ac._kv_seal(KEY, fd.durable_view(_rich_state(2, 1)[fd.KEY]))
    monkeypatch.setattr(ac, "kv_get", lambda key: sealed if key == ac.DIRECT_KV_KEY else None)
    st = {}
    assert ac.direct_kv_hydrate(st, KEY) and st[fd.KEY]["day"]["escrow"] == 900.0
    assert len(st[fd.KEY]["pending"]) == 3
    other = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)           # bản ký bằng khoá khác -> bỏ
    monkeypatch.setattr(ac, "kv_get", lambda key: ac._kv_seal(other, {"day": {"day": "x", "escrow": 0.0}}))
    st2 = {}
    assert not ac.direct_kv_hydrate(st2, KEY) and fd.KEY not in st2
    monkeypatch.setattr(ac, "kv_get", lambda key: (_ for _ in ()).throw(RuntimeError("down")))
    assert not ac.direct_kv_hydrate({}, KEY)


def test_agent_miner_capacity_matches_other_ss58_prefixes(monkeypatch):
    monkeypatch.setenv("FLOP_GATEWAY_PROMPTS", "Explain optimistic versus pessimistic locking in distributed databases.")
    monkeypatch.setenv("FLOP_DIRECT_MAX_TOKENS", "1024")
    monkeypatch.setenv("FLOP_DIRECT_MODEL", "Qwen/Qwen3-14B")
    cli = CLI()
    monkeypatch.setattr(fd, "_run", cli)
    other_prefix = fg.ss58_encode(bytes([7]) * 32, prefix=0)                  # cùng ví MINER, prefix khác

    class _R:
        status_code = 200
        text = ""

        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

        def raise_for_status(self):
            return None

    def get(url, **k):
        if "/api/session-offers" in url:
            return _R({"data": [offer()]})
        if url.endswith("/api/compute-channels"):
            return _R({"data": [{"miner": other_prefix, "capGflopsPerSec": 130}]})
        if "/api/models" in url:
            return _R({"models": []})
        if "/api/accounts/" in url:
            return _R({"balance": str(10 ** 21)})
        return _R({})

    monkeypatch.setattr(ac.requests, "get", get)
    monkeypatch.setattr(ac, "kv_set", lambda *a, **k: True)
    monkeypatch.setattr(ac, "_telegram_send", lambda m: True)
    assert ac.direct_session_step({}, KEY, NOW) == "opened"
    run = cli.sessions()[0]["cmd"]
    ptok = len(run[run.index("--prompt") + 1]) // 4
    assert int(run[run.index("--max-tokens") + 1]) > int(10 * 300 / 28.0) - ptok    # đã dùng cap 130, không phải 10


# --- audit vòng 2 (2026-11-13): CLI 0.4.4 trong image ------------------------------------------------------------

def _dur_offer(seconds, **kw):
    o = offer(**kw)
    o["offer"]["maxDuration"] = str(seconds)
    return o


def test_cli_gets_the_indexer_url_for_offer_discovery():
    cli = CLI()
    step({}, cli, indexer_url="https://explorer.testnet.flop.network/")
    run = cli.sessions()[0]
    assert run["env"]["FLOP_INDEXER_URL"] == "https://explorer.testnet.flop.network"
    assert run["cmd"][run["cmd"].index("FLOP_INDEXER_URL") - 1] == "-e"              # chỉ TÊN trên dòng lệnh
    cli2 = CLI()
    step({}, cli2)
    assert "FLOP_INDEXER_URL" not in cli2.sessions()[0]["env"]


def test_session_timeout_covers_the_channel_max_duration():
    cli = CLI()
    step({}, cli, offers=[_dur_offer(600)])
    assert cli.sessions()[0]["timeout"] == 600 + fd.MAXDUR_MARGIN_SEC
    cli = CLI()
    step({}, cli, offers=[_dur_offer(120)])
    assert cli.sessions()[0]["timeout"] == fd.DEFAULT_TIMEOUT_SEC                    # không thấp hơn cấu hình


def test_a_session_that_cannot_outlive_its_channel_is_not_started():
    cli = CLI()
    r = step({}, cli, offers=[_dur_offer(900)], deadline=NOW + 900.0, clock=lambda: float(NOW))
    assert r["outcome"] == "run_budget" and cli.sessions() == [] and str(900 + fd.MAXDUR_MARGIN_SEC) in r["reason"]


def test_a_long_offer_is_skipped_for_a_miner_that_fits(monkeypatch, three_miners):
    def offers_fn(miner, mh):
        return [_dur_offer(900 if miner == MINER else 200, miner=miner, model_hash=mh or MH)]

    cli = MultiCLI()
    r = step({}, cli, offers_fn=offers_fn, deadline=NOW + 900.0, clock=lambda: float(NOW))
    assert MINER not in cli.miners() and r["opened"] >= 1


def test_sizing_uses_the_shortest_offer_duration(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_TOKENS", "1024")
    monkeypatch.setenv("FLOP_DIRECT_MODEL", "Qwen/Qwen3-14B")
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "2000")
    cli = CLI()
    step({}, cli, prompt=LONG, offers=[_dur_offer(600), _dur_offer(60)], miner_cap_fn=lambda m: 130.0)
    run = cli.sessions()[0]
    assert int(run["cmd"][run["cmd"].index("--max-tokens") + 1]) == int(130 * 60 / 28.0) - len(LONG) // 4
    assert run["timeout"] == 600 + fd.MAXDUR_MARGIN_SEC                                 # chờ theo offer DÀI nhất


def test_escrow_fits_every_precision_the_miner_offers():
    lo, hi = offer(min_escrow=5 * 10 ** 16), offer(min_escrow=2 * 10 ** 17)
    lo["offer"]["precision"], hi["offer"]["precision"] = 0, 1
    esc, why, mh = fd.choose_escrow([lo, hi], MINER, None, Decimal(100))
    assert esc == "0.2" and mh == MH
    same = offer(min_escrow=2 * 10 ** 17)
    same["offer"]["precision"] = 0
    assert fd.choose_escrow([lo, same], MINER, None, Decimal(100))[0] == "0.05"      # cùng precision: thấp nhất


def test_cli_timeout_marks_the_run_for_orphan_discovery_and_finds_the_channel():
    st, alerts = {}, []
    step(st, CLI(session=(None, "", "")), alerts=alerts)
    assert st[fd.KEY]["orphan_since"] == NOW and mst(st)["unknown_until"] == NOW + fd.UNKNOWN_HOLD_SEC
    rows = [{"channelId": CH, "agent": ADDR, "miner": MINER, "openedAt": (NOW + 5) * 1000},
            {"channelId": CH2, "agent": OTHER, "miner": MINER, "openedAt": (NOW + 5) * 1000},          # ví khác
            {"channelId": "0x" + "ef" * 32, "agent": ADDR, "miner": MINER, "openedAt": (NOW - 3600) * 1000}]  # cũ
    r = step(st, CLI(), now=NOW + 300, own_channels_fn=lambda a: rows, alerts=alerts)
    pend = st[fd.KEY]["pending"]
    assert [p["channel"] for p in pend] == [CH] and pend[0]["error"] and any("mồ côi" in a or "CLI không báo id" in a
                                                                             for a in alerts)
    assert r["outcome"] in ("pending", "hold_unknown")                                 # miner bị chặn tới khi kênh xong
    step(st, CLI(), explorer={CH: paid()}, now=NOW + 600, own_channels_fn=lambda a: rows)
    assert st[fd.KEY]["pending"] == [] and fd._short(CH) in st[fd.KEY]["done"]
    assert tm.load_ledger()["session_cumulative"]["FLOP"] == "0.021"                 # kênh mồ côi PAID -> ghi sổ
    step(st, CLI(), explorer={CH: paid()}, now=NOW + 900, own_channels_fn=lambda a: rows)
    assert st[fd.KEY]["pending"] == []                                                # không thêm lại kênh đã xong


def test_orphan_search_stops_after_its_window():
    st = {fd.KEY: {"orphan_since": NOW}}
    seen = []
    step(st, CLI(), now=NOW + fd.ORPHAN_WINDOW_SEC + 1, own_channels_fn=lambda a: seen.append(a) or [])
    assert seen == [] and "orphan_since" not in st[fd.KEY]


def test_orphan_lookup_failure_is_harmless():
    def boom(addr):
        raise RuntimeError("explorer down")

    st = {fd.KEY: {"orphan_since": NOW}}
    assert step(st, CLI(), now=NOW + 300, own_channels_fn=boom)["outcome"] == "opened"


def test_unknown_outcome_message_without_channel_also_starts_orphan_discovery():
    err = json.dumps({"ok": False, "message": "settlement outcome unknown"})
    st = {}
    step(st, CLI(session=(1, "", err)))
    assert st[fd.KEY]["orphan_since"] == NOW


def test_ledger_write_failure_keeps_the_channel_pending(monkeypatch):
    st = {}
    step(st, CLI())
    real, broken = tm._save_ledger, [True]
    monkeypatch.setattr(tm, "_save_ledger", lambda state, path=None: False if broken[0] else real(state, path))
    other = (0, json.dumps({"ok": True, "channel": CH2}), "")
    r = step(st, CLI(session=other), explorer={CH: paid()}, now=NOW + 300)
    assert r["booked"] == 0 and any(p["channel"] == CH for p in st[fd.KEY]["pending"])
    broken[0] = False
    step(st, CLI(session=other), explorer={CH: paid()}, now=NOW + 600)
    assert all(p["channel"] != CH for p in st[fd.KEY]["pending"])


def test_record_direct_spend_reports_a_failed_save(monkeypatch):
    monkeypatch.setattr(tm, "_save_ledger", lambda state, path=None: False)
    assert tm.record_direct_spend("1.5", "m", CH)["outcome"] == "save_failed"


def test_channels_missing_from_the_explorer_for_two_days_are_dropped_with_an_alert():
    st, alerts = {}, []
    step(st, CLI())
    step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), "")), now=NOW + 3600)  # 404 một giờ: vẫn giữ
    assert any(p["channel"] == CH for p in st[fd.KEY]["pending"])
    step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), "")), now=NOW + fd.MISSING_DROP_SEC + 1,
         alerts=alerts)
    assert all(p["channel"] != CH for p in st[fd.KEY]["pending"])
    assert st[fd.KEY]["stats"]["missing"] == 1 and any("không có trên explorer" in a for a in alerts)


def test_open_channels_are_never_dropped_by_age():
    st = {}
    step(st, CLI())
    still_open = {"agent": ADDR, "status": "open", "paymentStatus": "LOCKED", "observationHead": "finalized"}
    step(st, CLI(), explorer={CH: still_open}, now=NOW + fd.MISSING_DROP_SEC + 1)
    assert any(p["channel"] == CH for p in st[fd.KEY]["pending"])


def test_direct_deadline_defaults_and_clamps(monkeypatch):
    monkeypatch.setattr(ac, "_RUN_STARTED", 1000.0)
    monkeypatch.delenv("FLOP_DIRECT_RUN_BUDGET_SEC", raising=False)
    assert ac.direct_deadline() == 2020.0
    monkeypatch.setenv("FLOP_DIRECT_RUN_BUDGET_SEC", "5000")
    assert ac.direct_deadline() == 2080.0
    monkeypatch.setattr(ac, "_RUN_STARTED", None)
    assert ac.direct_deadline() == float("inf")


def test_agent_filters_expired_offers_and_passes_the_indexer(monkeypatch):
    monkeypatch.setenv("FLOP_GATEWAY_PROMPTS", "q1")
    cli = CLI()
    monkeypatch.setattr(fd, "_run", cli)
    seen = []
    expired = offer(min_escrow=1)
    expired["offer"]["expiry"] = "100"
    live = offer(min_escrow=5 * 10 ** 16)
    live["offer"]["expiry"] = "99999999"

    class _R:
        status_code = 200
        text = ""

        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

        def raise_for_status(self):
            return None

    def get(url, **k):
        seen.append((url, dict(k.get("params") or {})))
        if url.endswith("/api/stats"):
            return _R({"latestBlockNumber": 5000})
        if "/api/session-offers" in url:
            return _R({"data": [expired, live]})
        return _R({})

    monkeypatch.setattr(ac.requests, "get", get)
    monkeypatch.setattr(ac, "kv_set", lambda *a, **k: True)
    monkeypatch.setattr(ac, "_telegram_send", lambda m: True)
    assert ac.direct_session_step({}, KEY, NOW) == "opened"
    offer_q = [p for u, p in seen if "/api/session-offers" in u][0]
    assert offer_q["minExpiry"] == "5001"
    run = cli.sessions()[0]
    assert Decimal(run["cmd"][run["cmd"].index("--max-flop") + 1]) >= Decimal("0.05")  # không theo offer hết hạn
    assert run["env"]["FLOP_INDEXER_URL"] == "https://explorer.testnet.flop.network"


# --- audit vòng 3: settle cưỡng bức, đối soát khi tắt, đổi mạng -----------------------------------------------------

class FinCLI(CLI):
    """CLI giả có lệnh `channel finalize`."""

    def __init__(self, fin=(0, json.dumps({"ok": True, "channel_id": CH, "tx_hash": "0x" + "aa" * 32}), ""), **kw):
        super().__init__(**kw)
        self.fin = fin

    def __call__(self, cmd, env, timeout):
        if "finalize" in cmd:
            self.calls.append({"cmd": list(cmd), "env": dict(env), "timeout": timeout})
            return self.fin
        return super().__call__(cmd, env, timeout)

    def finalizes(self):
        return [c for c in self.calls if "finalize" in c["cmd"]]


FORCED = (0, json.dumps({"ok": True, "channel": CH, "cooperative": False, "completion_tokens": "2"}), "")
OTHER_SESSION = (0, json.dumps({"ok": True, "channel": CH2}), "")
STILL_OPEN = {"agent": ADDR, "status": "open", "paymentStatus": "LOCKED", "observationHead": "finalized"}


def test_force_settled_session_is_finalized_after_the_challenge_window():
    st, alerts = {}, []
    step(st, CLI(session=FORCED))
    assert st[fd.KEY]["pending"][0]["force"] is True
    cli = FinCLI(session=OTHER_SESSION)
    step(st, cli, explorer={CH: STILL_OPEN}, now=NOW + 7 * 3600, alerts=alerts)
    assert cli.finalizes() == [] and not any(CH[:18] in a for a in alerts)            # chờ 7 ngày là bình thường
    cli = FinCLI(session=OTHER_SESSION)
    t = NOW + fd.FINALIZE_AFTER_SEC + 1
    step(st, cli, explorer={CH: STILL_OPEN}, now=t)
    fin = cli.finalizes()
    c = fin[0]["cmd"]
    assert len(fin) == 1 and c[-1] == CH and c[len(c) - 1 - c[::-1].index("--network") + 1] == "testnet"
    rec = [p for p in st[fd.KEY]["pending"] if p["channel"] == CH][0]
    assert rec["fin_tries"] == 1 and rec["fin_tx"].startswith("0xaa")
    cli = FinCLI(session=OTHER_SESSION)
    step(st, cli, explorer={CH: STILL_OPEN}, now=t + 3600)
    assert cli.finalizes() == []                                                        # mỗi ngày một lần
    step(st, FinCLI(session=OTHER_SESSION), explorer={CH: paid()}, now=t + 7200)
    assert all(p["channel"] != CH for p in st[fd.KEY]["pending"])
    assert tm.load_ledger()["session_cumulative"]["FLOP"] == "0.021"                    # PAID sau finalize -> ghi sổ


def test_finalize_gives_up_after_three_failures_with_an_alert():
    st, alerts = {}, []
    step(st, CLI(session=FORCED))
    bad = (1, "", json.dumps({"ok": False, "message": "ChallengeWindowOpen"}))
    t = NOW + fd.FINALIZE_AFTER_SEC + 1
    for i in range(4):
        cli = FinCLI(fin=bad, session=OTHER_SESSION)
        step(st, cli, explorer={CH: STILL_OPEN}, now=t + i * fd.FINALIZE_RETRY_SEC, alerts=alerts)
        assert len(cli.finalizes()) == (1 if i < 3 else 0)
    assert sum(1 for a in alerts if "finalize" in a) == 1


def test_disabled_agent_still_books_channels_left_pending(monkeypatch):
    st = {}
    step(st, CLI())
    monkeypatch.setenv("FLOP_DIRECT_ENABLED", "off")
    cli = CLI()
    r = step(st, cli, explorer={CH: paid()}, now=NOW + 300)
    assert r["outcome"] == "off" and r["booked"] == 1 and cli.calls == [] and st[fd.KEY]["pending"] == []
    assert step(st, CLI(), now=NOW + 600)["outcome"] == "off"


def test_bad_config_or_denied_miners_still_reconcile(monkeypatch):
    st = {}
    step(st, CLI())
    monkeypatch.setenv("FLOP_DIRECT_VIA", "nonsense")
    r = step(st, CLI(), explorer={CH: paid()}, now=NOW + 300)
    assert r["outcome"] == "unconfigured" and r["booked"] == 1
    monkeypatch.setenv("FLOP_DIRECT_VIA", "offer")
    st2 = {}
    step(st2, CLI(session=OTHER_SESSION))
    monkeypatch.setenv("FLOP_DIRECT_DENY_MINERS", MINER)
    r = step(st2, CLI(), explorer={CH2: paid()}, now=NOW + 300)
    assert r["outcome"] == "refused" and r["booked"] == 1


def test_channels_of_the_other_network_are_dropped_without_lookup():
    st = {}
    step(st, CLI(), network="devnet")
    seen = []
    explorer = type("E", (), {"get": lambda self, c: seen.append(c) or None})()
    step(st, CLI(session=OTHER_SESSION), explorer=explorer, now=NOW + 300)
    assert CH not in seen and all(p["channel"] != CH for p in st[fd.KEY]["pending"])
    assert st[fd.KEY]["stats"]["other_network"] == 1


def test_learned_capacity_survives_an_early_stop():
    st = {}
    step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH, "completion_tokens": "2"}), "")))
    st[fd.KEY]["wait_until"] = NOW + 99999                                              # chốt chung dừng sớm
    step(st, CLI(), explorer={CH: {**paid(), "capGflopsPerSec": 77}}, now=NOW + 300)
    assert mst(st)["cap"] == 77.0


def test_agent_reconciles_while_disabled(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_ENABLED", "off")
    called = []
    monkeypatch.setattr(fd, "run_step", lambda state, **kw: called.append(1) or {"outcome": "off", "opened": 0})
    monkeypatch.setattr(ac, "kv_set", lambda *a, **k: True)
    assert ac.direct_session_step({}, KEY, NOW) == "off" and called == []
    assert ac.direct_session_step({fd.KEY: {"pending": [{"channel": CH}]}}, KEY, NOW) == "off" and called == [1]


# --- audit vòng 4: "settled mà chưa PAID" không phải hoàn tiền; finalize không gọi lại ---------------------------------

POSTED = {"agent": ADDR, "status": "settled", "paymentStatus": "LOCKED", "observationHead": "finalized"}


@pytest.mark.parametrize("info,kind", [
    ({"agent": ADDR, "status": "timedout", "paymentStatus": "REFUNDED", "observationHead": "finalized"}, "refunded"),
    ({"agent": ADDR, "status": "settled", "paymentStatus": "REFUNDED", "observationHead": "finalized"}, "refunded"),
    ({"agent": ADDR, "status": "expired", "paymentStatus": "LOCKED", "observationHead": "finalized"}, "refunded"),
    (POSTED, "posted"),
    ({**POSTED, "status": "closed"}, "posted"),
    ({**POSTED, "observationHead": "best"}, "open"),
    ({"agent": ADDR, "status": "open", "paymentStatus": "LOCKED", "observationHead": "finalized"}, "open"),
])
def test_only_explicit_refunds_are_refunds(info, kind):
    assert fd.classify_channel(info, ADDR)[0] == kind


def test_settled_but_unpaid_channel_is_kept_and_finalized_later():
    st, alerts = {}, []
    step(st, CLI())                                                               # phiên thường (không cờ cưỡng bức)
    step(st, CLI(session=OTHER_SESSION), explorer={CH: POSTED}, now=NOW + 7 * 3600, alerts=alerts)
    rec = [p for p in st[fd.KEY]["pending"] if p["channel"] == CH][0]
    assert rec["force"] is True and "refunded" not in (st[fd.KEY].get("stats") or {})
    assert not any(CH[:18] in a for a in alerts)                                  # không báo "kẹt" sau 6 giờ
    cli = FinCLI(session=OTHER_SESSION)
    step(st, cli, explorer={CH: POSTED}, now=NOW + fd.FINALIZE_AFTER_SEC + 1)
    assert len(cli.finalizes()) == 1


def test_successful_finalize_is_not_repeated_and_a_slow_explorer_alerts_once():
    st, alerts = {}, []
    step(st, CLI(session=FORCED))
    t = NOW + fd.FINALIZE_AFTER_SEC + 1
    step(st, FinCLI(session=OTHER_SESSION), explorer={CH: POSTED}, now=t)
    calls = 0
    for i in range(1, 6):
        cli = FinCLI(session=OTHER_SESSION)
        step(st, cli, explorer={CH: POSTED}, now=t + i * fd.FINALIZE_RETRY_SEC, alerts=alerts)
        calls += len(cli.finalizes())
    assert calls == 0                                                              # đã có tx: không gọi lại
    assert sum(1 for a in alerts if "đã finalize" in a) == 1
    assert not any("thất bại" in a for a in alerts)


def test_finalize_tx_survives_the_kv_round_trip():
    g = {"pending": [{"channel": CH, "ts": NOW, "network": "testnet", "miner": MINER, "force": True,
                      "fin_tries": 1, "fin_at": NOW, "fin_tx": "0x" + "aa" * 32, "fin_alerted": NOW}]}
    new = {}
    assert fd.restore(new, json.loads(json.dumps(fd.durable_view(g, level=1))))
    p = new[fd.KEY]["pending"][0]
    assert p["fin_tx"] == "0x" + "aa" * 32 and p["force"] is True and p["fin_alerted"] == NOW


# --- số dư: ví mất CẢ escrow (1% phí kiểm toán không về ví) ---------------------------------------------------------

def test_cooperative_settlement_deducts_the_whole_escrow_from_the_balance():
    tm.credit("100")
    st = {}
    step(st, CLI())
    info = {**paid(miner_paid=99 * 10 ** 15, burned=0), "escrow": str(10 ** 17), "cooperative": True}
    step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), "")), explorer={CH: info}, now=NOW + 300)
    led = tm.load_ledger()
    assert led["balances"]["FLOP"] == "99.9"                    # trừ escrow 0.1, không phải 0.099
    assert led["session_cumulative"]["FLOP"] == "0.099"         # thước đo airdrop = số chain ghi cho ví
    assert led["direct_count"]["FLOP"] == 1 and led["entries"][-1]["escrow"] == "0.1"


def test_force_settlement_deducts_only_what_was_paid():
    tm.credit("100")
    st = {}
    step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH, "cooperative": False}), "")))
    info = {**paid(miner_paid=3 * 10 ** 15, burned=0), "escrow": str(10 ** 17), "cooperative": False}
    step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), "")), explorer={CH: info}, now=NOW + 300)
    assert tm.load_ledger()["balances"]["FLOP"] == "99.997"


def test_record_direct_spend_without_escrow_keeps_the_old_behaviour():
    tm.credit("10")
    tm.record_direct_spend("1.5", "inference-session direct m", CH)
    assert tm.load_ledger()["balances"]["FLOP"] == "8.5"


def test_balance_line_accounts_for_pending_direct_escrow():
    st = {fd.KEY: {"pending": [{"channel": CH, "network": "testnet", "max_flop": "100"}]}}
    assert "⚠️" not in ac.balance_line(st, Decimal("900"), "1000")                   # 100 đang khoá trong kênh
    assert "100 escrow" in ac.balance_line(st, Decimal("900"), "1000")
    assert "⚠️ lệch 25" in ac.balance_line(st, Decimal("925"), "1000")              # faucet nạp mà sổ thiếu
    assert "⚠️" in ac.balance_line({}, Decimal("998.5"), "1000")
    assert "⚠️" not in ac.balance_line({}, Decimal("998.5"), "1000", direct_count=200)   # phí mở 200 kênh


# --- bộ canh chi thật cho mua trực tiếp ------------------------------------------------------------------------------

def test_settled_counts_paid_channels_even_without_booking():
    st = {}
    step(st, CLI(), network="devnet", book=False)
    r = step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), "")), explorer={CH: paid()},
             network="devnet", book=False, now=NOW + 300)
    assert r["settled"] == 1 and r["booked"] == 0


def test_agent_alerts_when_direct_buying_stalls(monkeypatch):
    sent = []
    monkeypatch.setattr(ac, "_telegram_send", lambda m: sent.append(m) or True)
    st = {}
    for i in range(6):
        ac._DIRECT_LAST = ({"outcome": "low_balance", "settled": 0, "reason": "hết FLOP", "balance": 0.2},
                           "testnet", "https://explorer.testnet.flop.network")
        ac.direct_watchdog_step(st, NOW + i * 300)
    assert len(sent) == 1 and "MUA TRỰC TIẾP (testnet)" in sent[0] and ac._DIRECT_LAST is None
    ac.direct_watchdog_step(st, NOW + 9999)                                 # lượt không chạy bước direct: bỏ qua
    assert len(sent) == 1


def test_agent_direct_watchdog_is_quiet_when_disabled(monkeypatch):
    sent = []
    monkeypatch.setattr(ac, "_telegram_send", lambda m: sent.append(m) or True)
    monkeypatch.setenv("FLOP_DIRECT_ENABLED", "off")
    st = {}
    for i in range(10):
        ac._DIRECT_LAST = ({"outcome": "off"}, "testnet", "")
        ac.direct_watchdog_step(st, NOW + i * 300)
    assert sent == [] and not st.get("real_spend_watch_direct")


def test_direct_session_step_hands_its_result_to_the_watchdog(monkeypatch):
    monkeypatch.setattr(fd, "run_step", lambda state, **kw: {"outcome": "no_offer", "opened": 0, "settled": 0})
    monkeypatch.setattr(ac, "kv_set", lambda *a, **k: True)
    monkeypatch.setattr(ac, "kv_get", lambda *a, **k: None)
    ac._DIRECT_LAST = None
    ac.direct_session_step({}, KEY, NOW)
    assert ac._DIRECT_LAST and ac._DIRECT_LAST[0]["outcome"] == "no_offer" and ac._DIRECT_LAST[1] == "testnet"


# --- kênh mồ côi: giữ escrow cho cổng gateway tới khi tìm ra kênh ----------------------------------------------------

def test_cli_timeout_holds_escrow_until_the_orphan_is_found():
    st = {}
    step(st, CLI(session=(None, "", "")))
    held = st[fd.KEY]["unknown_escrow"]
    assert len(held) == 1 and held[0][1] == "testnet" and Decimal(held[0][2]) > 0
    rows = [{"channelId": CH, "agent": ADDR, "miner": MINER, "openedAt": (NOW + 5) * 1000, "escrow": str(4 * 10 ** 16)}]
    step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), "")), now=NOW + 300,
         own_channels_fn=lambda a: rows)
    rec = [p for p in st[fd.KEY]["pending"] if p["channel"] == CH][0]
    assert rec["max_flop"] == "0.04" and st[fd.KEY]["unknown_escrow"] == []   # escrow thật thay khoản giữ


def test_orphan_hold_is_dropped_with_the_search_window():
    st = {}
    step(st, CLI(session=(None, "", "")))
    step(st, CLI(), now=NOW + fd.ORPHAN_WINDOW_SEC + 1, own_channels_fn=lambda a: [])
    assert "unknown_escrow" not in st[fd.KEY] and "orphan_since" not in st[fd.KEY]


# --- kênh bị hoàn trả phần giữ trong trần ngày ------------------------------------------------------------------------

REFUNDED = {"agent": ADDR, "status": "timedout", "paymentStatus": "REFUNDED", "observationHead": "finalized",
            "minerPaid": "0", "burned": "0"}
REFUND_ERR = (1, "", json.dumps({"ok": False, "code": "SESSION_FAILED", "channel": CH,
                                 "message": "channel refunded: verified work cannot settle within its calibrated lifetime"}))


def test_refunded_channel_gives_its_escrow_back_to_the_daily_cap(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "0.12")
    st = {}
    step(st, CLI(session=REFUND_ERR))
    assert st[fd.KEY]["day"]["escrow"] == pytest.approx(0.05)
    assert [p.get("max_flop") for p in st[fd.KEY]["pending"]] == ["0.05"]       # bản ghi lỗi giữ escrow
    for v in st[fd.KEY]["miners"].values():
        v["wait_until"] = 0
    step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), "")), explorer={CH: REFUNDED}, now=NOW + 300)
    assert st[fd.KEY]["day"]["escrow"] == pytest.approx(0.05)                   # trả 0.05 của kênh hoàn, giữ 0.05 mới


def test_twenty_refunds_no_longer_burn_the_day(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "0.2")
    st, opened, t = {}, 0, NOW
    for i in range(20):
        cid = "0x" + f"{i + 1:064x}"
        err = (1, "", json.dumps({"ok": False, "channel": cid, "message": "channel refunded: verified work"}))
        prev = "0x" + f"{i:064x}"
        for v in (st.get(fd.KEY) or {}).get("miners", {}).values():
            v["wait_until"] = 0
        r = step(st, CLI(session=err), explorer={prev: REFUNDED}, now=t)
        opened += 1 if r["outcome"] == "error" else 0
        t += 300
    assert opened == 20                                                          # trước: dừng sau 4 lần (0.2 / 0.05)


def test_refund_of_a_channel_opened_yesterday_does_not_touch_today(monkeypatch):
    st = {}
    step(st, CLI(session=REFUND_ERR))
    st[fd.KEY]["day"] = {"day": "x", "escrow": 0.05}
    tomorrow = NOW + 86400
    for v in st[fd.KEY]["miners"].values():
        v["wait_until"] = 0
    step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), "")), explorer={CH: REFUNDED}, now=tomorrow)
    assert st[fd.KEY]["day"]["escrow"] == pytest.approx(0.05)                   # chỉ phiên mới hôm nay


def test_pending_escrow_survives_the_trimmed_kv_view():
    g = {"pending": [{"channel": CH, "ts": NOW, "network": "testnet", "miner": MINER, "max_flop": "99.5",
                      "model_hash": MH, "ptok": 10}]}
    v = fd.durable_view(g, level=1)
    assert v["pending"][0]["max_flop"] == "99.5" and "model_hash" not in v["pending"][0]


# --- gửi Telegram lỗi thì lượt sau báo lại -------------------------------------------------------------------------

def test_failed_alert_is_retried_next_run(monkeypatch):
    sent, ok = [], [False]
    monkeypatch.setattr(ac, "_telegram_send", lambda m: sent.append(m) or ok[0])
    st = {}

    def run(i):
        ac._DIRECT_LAST = ({"outcome": "low_balance", "settled": 0, "reason": "hết FLOP"}, "testnet", "")
        ac.direct_watchdog_step(st, NOW + i * 300)

    for i in range(6):
        run(i)
    assert len(sent) == 1 and not st["real_spend_watch_direct"].get("alerted_ts")   # gửi lỗi: chưa coi là đã báo
    ok[0] = True
    run(6)
    assert len(sent) == 2 and st["real_spend_watch_direct"]["alerted_ts"] == NOW + 6 * 300
    run(7)
    assert len(sent) == 2                                                             # đã báo: chờ realert


def test_unmark_alert_restores_the_previous_mark():
    import flop_watchdog as wd
    st = {wd.DIRECT_KEY: {"misses": 9, "alerted_ts": 500}}
    wd.unmark_alert(st, wd.DIRECT_KEY, 100)
    assert st[wd.DIRECT_KEY] == {"misses": 9, "alerted_ts": 100}
    wd.unmark_alert(st, wd.DIRECT_KEY, None)
    assert "alerted_ts" not in st[wd.DIRECT_KEY]


# --- cửa sổ tìm kênh mồ côi gia hạn theo lần ngắt GẦN NHẤT ------------------------------------------------------------

def test_a_later_cli_cut_extends_the_orphan_search():
    st = {}
    step(st, CLI(session=(None, "", "")))                                      # ngắt lần 1 (t0)
    for v in st[fd.KEY]["miners"].values():
        v["unknown_until"] = 0
    later = NOW + fd.ORPHAN_WINDOW_SEC - 3600
    step(st, CLI(session=(None, "", "")), now=later, own_channels_fn=lambda a: [])   # ngắt lần 2, gần hết cửa sổ
    assert len(st[fd.KEY]["unknown_escrow"]) == 2
    t = NOW + fd.ORPHAN_WINDOW_SEC + 3600                                      # cửa sổ của lần 1 đã qua
    rows = [{"channelId": CH, "agent": ADDR, "miner": MINER, "openedAt": later * 1000, "escrow": str(5 * 10 ** 16)}]
    for v in st[fd.KEY]["miners"].values():
        v["unknown_until"] = 0
    step(st, CLI(session=(0, json.dumps({"ok": True, "channel": CH2}), "")), now=t, own_channels_fn=lambda a: rows)
    g = st[fd.KEY]
    assert any(p["channel"] == CH for p in g["pending"])                       # kênh của lần ngắt 2 vẫn được tìm ra
    assert g.get("orphan_since") and len(g["unknown_escrow"]) == 0             # khoản giữ lần 1 hết hạn; lần 2 đã khớp


def test_an_unfound_hold_expires_with_an_alert():
    st, alerts = {}, []
    step(st, CLI(session=(None, "", "")))
    step(st, CLI(), now=NOW + fd.ORPHAN_WINDOW_SEC + 1, own_channels_fn=lambda a: [], alerts=alerts)
    assert not st[fd.KEY].get("unknown_escrow") and "orphan_since" not in st[fd.KEY]
    assert any("không tìm thấy kênh" in a for a in alerts)


# --- H1: lượt bị giết GIỮA phiên không được quên kênh / trần ngày ------------------------------------------------------

class SeqCLI(CLI):
    """CLI giả trả lần lượt từng kết quả phiên; một phần tử là ngoại lệ thì NÉM nó (runner chết giữa phiên)."""

    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    def __call__(self, cmd, env, timeout):
        if "keys" in cmd and "import" in cmd:
            return super().__call__(cmd, env, timeout)
        self.calls.append({"cmd": list(cmd), "env": dict(env), "timeout": timeout})
        r = self.results.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def test_checkpoint_runs_before_every_cli_session_with_the_attempt_mark(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "2")
    st, snaps = {}, []
    ok1 = (0, json.dumps({"ok": True, "channel": CH}), "")
    ok2 = (0, json.dumps({"ok": True, "channel": CH2}), "")
    step(st, SeqCLI([ok1, ok2]), checkpoint=lambda: snaps.append(json.loads(json.dumps(st[fd.KEY]))))
    before = [s for s in snaps if s.get("attempt")]
    assert len(before) == 2                                                     # một mốc trước MỖI phiên
    assert before[0]["attempt"]["miner"] == MINER and before[0]["day"]["escrow"] == pytest.approx(0.05)
    assert [p["channel"] for p in before[1]["pending"]] == [CH]                 # kênh phiên 1 đã xuống đĩa trước phiên 2
    assert "attempt" not in st[fd.KEY]                                          # phiên xong: không còn mốc


def test_a_run_killed_mid_session_is_recovered_as_an_unknown_outcome(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "2")
    st, snaps = {}, []
    ok1 = (0, json.dumps({"ok": True, "channel": CH}), "")
    with pytest.raises(RuntimeError):
        step(st, SeqCLI([ok1, RuntimeError("runner chết giữa phiên 2")]),
             checkpoint=lambda: snaps.append(json.loads(json.dumps(st[fd.KEY]))))
    disk = snaps[-1]                                                            # thứ duy nhất còn lại trên đĩa
    assert [p["channel"] for p in disk["pending"]] == [CH]                      # phiên 1 không bị quên
    assert disk["day"]["escrow"] == pytest.approx(0.1)                          # trần ngày giữ cả phiên 2
    assert fd.durable_view(disk)["attempt"]["escrow"] == "0.05"                 # đi cả lên KV
    a_ts = disk["attempt"]["ts"]
    rows = [{"channelId": CH2, "agent": ADDR, "miner": MINER, "openedAt": (a_ts + 8) * 1000,
             "escrow": str(5 * 10 ** 16)}]
    st2, alerts, cli = {fd.KEY: disk}, [], CLI()
    step(st2, cli, now=NOW + 300, own_channels_fn=lambda a: rows, alerts=alerts)
    g = st2[fd.KEY]
    assert "attempt" not in g and any("NGẮT giữa phiên" in a for a in alerts)
    assert {p["channel"] for p in g["pending"]} == {CH, CH2}                    # kênh của phiên bị ngắt được tìm ra
    assert g["unknown_escrow"] == [] and mst(st2)["unknown_until"] == a_ts + fd.UNKNOWN_HOLD_SEC
    assert cli.sessions() == []                                                 # miner đó bị giữ, không mua tiếp


def test_agent_saves_direct_state_when_the_step_raises(monkeypatch):
    sent = {}
    monkeypatch.setattr(ac, "kv_set", lambda pk, did, k, v: sent.update({k: v}) or True)
    monkeypatch.setattr(ac, "kv_get", lambda *a, **k: None)

    def dying(state, **kw):
        state[fd.KEY] = {"attempt": {"ts": NOW, "miner": MINER, "escrow": "1", "network": "testnet"}}
        kw["checkpoint"]()
        raise RuntimeError("boom")

    monkeypatch.setattr(fd, "run_step", dying)
    assert ac.direct_session_step({}, KEY, NOW) == "error"
    assert ac.load_state()[fd.KEY]["attempt"]["miner"] == MINER
    assert ac._kv_open(sent[ac.DIRECT_KV_KEY], KEY)["attempt"]["escrow"] == "1"  # checkpoint đẩy cả KV


# --- H4: CLI thoát lỗi KHÔNG kèm JSON = không rõ kết quả ----------------------------------------------------------------

@pytest.mark.parametrize("session", [(137, "", "Killed"), (1, "", "Segmentation fault"), (1, "", "")])
def test_non_json_failure_starts_orphan_discovery(session):
    st, alerts = {}, []
    r = step(st, CLI(session=session), alerts=alerts)
    g = st[fd.KEY]
    assert r["outcome"] == "unknown_outcome" and g["orphan_since"] == NOW
    assert len(g["unknown_escrow"]) == 1 and g["unknown_escrow"][0][3] == MINER
    assert mst(st)["unknown_until"] == NOW + fd.UNKNOWN_HOLD_SEC and alerts
    assert g["day"]["escrow"] == pytest.approx(0.05)                            # không trả lại phần giữ


def test_docker_that_never_started_the_container_is_a_clean_refusal():
    st = {}
    r = step(st, CLI(session=(125, "", "docker: Error response from daemon: Conflict.")))
    g = st[fd.KEY]
    assert r["outcome"] == "error" and "orphan_since" not in g and not g.get("unknown_escrow")
    assert g["day"]["escrow"] == 0.0


def test_unknown_option_without_json_is_still_unsupported():
    st = {}
    r = step(st, CLI(session=(1, "", "error: unknown option '--via'")))
    assert r["outcome"] == "unsupported" and "orphan_since" not in st[fd.KEY]
    assert st[fd.KEY]["day"]["escrow"] == 0.0


# --- H2: không nhặt lại kênh đã ghi sổ, không ghi sổ hai lần -----------------------------------------------------------

def _ch(i):
    import hashlib
    return "0x" + hashlib.sha256(f"ch-{i}".encode()).hexdigest()


def test_orphan_search_only_adopts_the_cut_sessions_channel():
    st = {}
    step(st, CLI(session=(None, "", "")))                                       # ngắt lúc NOW, miner MINER
    later = [{"channelId": _ch(i), "agent": ADDR, "miner": MINER, "openedAt": (NOW + 7 * 3600 + i * 300) * 1000}
             for i in range(60)]                                                 # phiên SAU đó (đã ghi sổ, rời `done`)
    other = [{"channelId": _ch(100), "agent": ADDR, "miner": OTHER, "openedAt": (NOW + 10) * 1000}]
    alerts = []
    step(st, CLI(), now=NOW + 12 * 3600, own_channels_fn=lambda a: later + other, alerts=alerts)
    def orphans():
        return [p["channel"] for p in st[fd.KEY].get("pending") or [] if "mồ côi" in str(p.get("error"))]

    assert orphans() == [] and len(st[fd.KEY]["unknown_escrow"]) == 1
    assert not any("tìm thấy kênh" in a for a in alerts)
    mine = [{"channelId": _ch(200), "agent": ADDR, "miner": MINER, "openedAt": (NOW + 15) * 1000}]
    step(st, CLI(), now=NOW + 12 * 3600 + 300, own_channels_fn=lambda a: later + other + mine)
    assert orphans() == [_ch(200)] and st[fd.KEY]["unknown_escrow"] == []


def test_legacy_three_field_hold_still_matches_near_its_run():
    st = {fd.KEY: {"orphan_since": NOW, "orphan_last": NOW, "unknown_escrow": [[NOW, "testnet", "0.05"]]}}
    rows = [{"channelId": CH, "agent": ADDR, "miner": MINER, "openedAt": (NOW + 600) * 1000},
            {"channelId": CH2, "agent": ADDR, "miner": MINER, "openedAt": (NOW + 6 * 3600) * 1000}]
    step(st, CLI(session=(0, json.dumps({"ok": True, "channel": "0x" + "99" * 32}), "")), now=NOW + 300,
         own_channels_fn=lambda a: rows)
    chans = [p["channel"] for p in st[fd.KEY]["pending"]]
    assert CH in chans and CH2 not in chans


def test_direct_dedupe_survives_heavy_gateway_traffic():
    assert tm.record_direct_spend("1", "m", CH)["outcome"] == "direct_settled"
    for j in range(tm.GATEWAY_REFS_KEEP + 50):                                  # biên nhận gateway xen giữa
        tm.record_gateway_spend("0.5", "inference-session gateway m", f"req-{j}", log=lambda m: None)
    assert tm.record_direct_spend("1", "m", CH)["outcome"] == "duplicate"
    assert tm.record_direct_spend("1", "m", "0x" + CH[2:].upper())["outcome"] == "duplicate"   # hoa/thường


def test_direct_dedupe_still_reads_hashes_written_by_older_ledgers():
    import hashlib
    old = hashlib.sha256(f"direct:{CH}".encode()).hexdigest()[:16]
    tm.save_ledger({"balances": {"FLOP": "5"}, "entries": [], "gateway_refs": [old]})
    assert tm.record_direct_spend("1", "m", CH)["outcome"] == "duplicate"


def test_direct_refs_fit_the_signed_kv_ledger(monkeypatch):
    for i in range(tm.DIRECT_REFS_KEEP + 20):
        tm.record_direct_spend("1", "inference-session direct some/model-name-14B", _ch(i), escrow="1.01",
                               log=lambda m: None)
    for j in range(tm.GATEWAY_REFS_KEEP + 20):
        tm.record_gateway_spend("0.5", "inference-session gateway some/model", f"req-{j}", log=lambda m: None)
    sent = {}
    monkeypatch.setattr(ac, "kv_set", lambda pk, did, k, v: sent.update({k: v}) or True)
    ac.persist_ledger_to_kv(KEY, "did")
    raw = sent["token_ledger_testnet"]
    body = ac._kv_open(raw, KEY)
    assert len(raw) <= ac.KV_VALUE_MAX
    assert len(body["direct_refs"]) == tm.DIRECT_REFS_KEEP and len(body["gateway_refs"]) == tm.GATEWAY_REFS_KEEP


# --- H3: bản KV MỚI hơn thắng state.json cũ (lưu cache lỗi một lượt) ---------------------------------------------------

def _kv(monkeypatch):
    kv = {}
    monkeypatch.setattr(ac, "kv_set", lambda pk, did, k, v: kv.update({k: v}) or True)
    monkeypatch.setattr(ac, "kv_get", lambda k: kv.get(k))
    return kv


def test_kv_persist_bumps_rev_only_when_the_content_changes(monkeypatch):
    kv = _kv(monkeypatch)
    st = {fd.KEY: {"day": {"day": "d", "escrow": 1.0}, "pending": []}}
    assert ac.direct_kv_persist(st, KEY) and st[fd.KEY]["rev"] == 1
    writes = dict(kv)
    assert ac.direct_kv_persist(st, KEY) and st[fd.KEY]["rev"] == 1 and kv == writes   # không đổi: không ghi lại
    st[fd.KEY]["day"] = {"day": "d", "escrow": 2.0}
    ac.direct_kv_persist(st, KEY)
    assert st[fd.KEY]["rev"] == 2 and ac._kv_open(kv[ac.DIRECT_KV_KEY], KEY)["rev"] == 2


def test_newer_kv_copy_beats_a_stale_state_json(monkeypatch):
    _kv(monkeypatch)
    newer = {fd.KEY: {"rev": 7, "day": {"day": "d", "escrow": 9.0}, "done": [fd._short(CH2)],
                      "pending": [{"channel": CH, "ts": NOW, "network": "testnet", "miner": MINER}],
                      "attempt": {"ts": NOW, "miner": MINER, "escrow": "1", "network": "testnet"}}}
    ac.direct_kv_persist(newer, KEY)                               # lượt N: KV ghi được (rev 8), cache thì lỗi
    gone = "0x" + "77" * 32
    stale = {fd.KEY: {"rev": 5, "day": {"day": "d", "escrow": 1.0},
                      "pending": [{"channel": CH2, "ts": NOW}, {"channel": gone, "ts": NOW}]}}   # cache lượt N-1
    assert ac.direct_kv_hydrate(stale, KEY)
    g = stale[fd.KEY]
    assert g["rev"] == 8 and g["day"]["escrow"] == 9.0 and g["attempt"]["miner"] == MINER
    assert [p["channel"] for p in g["pending"]] == [CH, gone]       # CH2 đã xong ở lượt N; `gone` KV không có -> giữ
    older = {fd.KEY: {"rev": 99, "day": {"day": "d", "escrow": 3.0}, "pending": []}}
    assert not ac.direct_kv_hydrate(older, KEY) and older[fd.KEY]["day"]["escrow"] == 3.0   # local mới hơn: giữ


def test_newer_gateway_inflight_blob_beats_the_local_one(monkeypatch):
    kv = _kv(monkeypatch)
    kv[ac.STATE_KV_KEY] = ac._kv_seal(KEY, {"gateway_inflight": {"at": NOW, "inflight": {"key": "new"}}},
                                         ac.STATE_KV_KEY)
    monkeypatch.setattr(ac.time, "time", lambda: NOW + 60)
    st = {"gateway_inflight": {"at": NOW - 300, "inflight": {"key": "old"}}}
    ac.hydrate_durable_from_kv(st, KEY)
    assert st["gateway_inflight"]["inflight"]["key"] == "new"
    st2 = {"gateway_inflight": {"at": NOW + 5, "inflight": {"key": "local"}}}
    ac.hydrate_durable_from_kv(st2, KEY)
    assert st2["gateway_inflight"]["inflight"]["key"] == "local"


def _gateway_seen(monkeypatch, state):
    import flop_gateway as gw
    seen = {}

    def fake_run(state, **kw):
        seen.update(json.loads(json.dumps(state.get(gw.KEY))))
        state[gw.KEY] = dict(state.get(gw.KEY) or {}, last_run=kw["now"])
        return {"outcome": "pace_wait", "calls": 0}

    monkeypatch.setattr(gw, "run_step", fake_run)
    monkeypatch.setenv("FLOP_GATEWAY_PROMPTS", "Q one|Q two")
    monkeypatch.setattr(ac, "build_market_context", lambda *a, **k: "")
    monkeypatch.setattr(ac, "_telegram_send", lambda m: True)
    ac.gateway_session_step(state, KEY, NOW)
    return seen


def test_gateway_step_takes_the_newer_durable_inflight_and_the_higher_day_count(monkeypatch):
    import flop_gateway as gw
    day = time.strftime("%Y-%m-%d", time.gmtime(NOW))
    prof = gw.profile_of("testnet")
    st = {gw.KEY: {"profile": prof, "last_run": NOW - 600, "inflight": {"key": "old", "ts": NOW - 600},
                   "day_spent": {"day": day, "spent": 5.0}},
          "gateway_inflight": {"at": NOW - 300, "profile": prof, "inflight": None,
                               "day_spent": {"day": day, "spent": 9.0}}}
    seen = _gateway_seen(monkeypatch, st)
    assert seen.get("inflight") is None and seen["day_spent"]["spent"] == 9.0   # lượt N đã đối soát + chi thêm
    st = {gw.KEY: {"profile": prof, "last_run": NOW - 600, "day_spent": {"day": day, "spent": 5.0}},
          "gateway_inflight": {"at": NOW - 300, "profile": prof, "inflight": {"key": "new", "ts": NOW - 300},
                               "day_spent": {"day": day, "spent": 2.0}}}
    seen = _gateway_seen(monkeypatch, st)
    assert seen["inflight"]["key"] == "new" and seen["day_spent"]["spent"] == 5.0   # trần ngày: số lớn hơn


# --- M1: offer quá đắt so với việc thật ------------------------------------------------------------------------------

def test_plan_refuses_an_offer_far_above_the_work():
    kw = dict(prompt="q" * 40, max_tokens=64, gn_per_token=2.0, cap_gfs=10.0, price=Decimal("0.003"),
              escrow_cap=Decimal(100), settle_target=300, margin=0.2)
    plan, why = fd.plan_session(min_escrow=Decimal(50), max_overpay=3.0, overpay_min=Decimal(1), **kw)
    assert plan is None and "quá đắt" in why
    assert fd.plan_session(min_escrow=Decimal(50), **kw)[0]["escrow"] == "50"            # tắt kiểm tra: như cũ
    assert fd.plan_session(min_escrow=Decimal("0.5"), max_overpay=3.0, overpay_min=Decimal(1), **kw)[0]  # dư < 1 FLOP


def test_overpriced_miner_is_skipped_with_one_alert(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "2000")         # trần ngày 25 mặc định sẽ chặn trước (daily_cap)
    st, alerts, cli = {}, [], CLI()
    r = step(st, cli, offers=[offer(min_escrow=50 * 10 ** 18)], alerts=alerts)
    assert r["outcome"] == "no_offer" and "quá đắt" in r["reason"] and cli.sessions() == []
    assert mst(st)["wait_why"] == "offer overpriced" and sum("quá đắt" in a for a in alerts) == 1
    step(st, CLI(), offers=[offer(min_escrow=50 * 10 ** 18)], alerts=alerts, now=NOW + 2 * 3600)
    assert sum("quá đắt" in a for a in alerts) == 1                                   # không báo lặp
    step(st, CLI(), alerts=alerts, now=NOW + 4 * 3600)                                   # miner hạ giá
    assert "overpay_alerted" not in mst(st)


# --- M7: trần chung cả hai đường ---------------------------------------------------------------------------------------

def test_combined_room_stops_direct_purchases(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_RUN", "3")
    cli = CLI()
    r = step({}, cli, spend_room=lambda: 0.07)                      # đủ 1 phiên 0.05, không đủ phiên thứ 2
    assert len(cli.sessions()) == 1 and "FLOP_MAX_SPEND_PER_DAY" in (r.get("reason") or "")
    cli = CLI()
    r = step({}, cli, spend_room=lambda: 0)
    assert r["outcome"] == "daily_cap" and cli.sessions() == []


def test_combined_spend_room_sums_both_paths(monkeypatch):
    import flop_gateway as gw
    day = time.strftime("%Y-%m-%d", time.gmtime(NOW))
    st = {gw.KEY: {"day_spent": {"day": day, "spent": 1000.0}}, fd.KEY: {"day": {"day": day, "escrow": 1400.0}}}
    assert ac.combined_spend_room(st, NOW) == pytest.approx(100.0)              # mặc định 2500
    monkeypatch.setenv("FLOP_MAX_SPEND_PER_DAY", "2000")
    assert ac.combined_spend_room(st, NOW) == 0.0
    assert ac.combined_spend_room(st, NOW + 86400) == 2000.0                     # sang ngày mới: đếm lại
    monkeypatch.setenv("FLOP_MAX_SPEND_PER_DAY", "0")
    assert ac.combined_spend_room(st, NOW) is None
    monkeypatch.setenv("FLOP_MAX_SPEND_PER_DAY", "nan")
    assert ac.combined_spend_room(st, NOW) == pytest.approx(100.0)               # rác -> mặc định, không tắt trần


def test_nan_direct_daily_cap_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "nan")
    assert fd.config()[0]["per_day"] == fd.DEFAULT_MAX_PER_DAY


# --- M5: bản niêm phong KV gắn đích (ns/khoá) + giờ --------------------------------------------------------------------

def test_sealed_record_cannot_be_moved_to_another_key():
    raw = ac._kv_seal(KEY, {"rev": 1, "day": {"day": "d", "escrow": 0.0}}, ac.STATE_KV_KEY)
    assert ac._kv_open(raw, KEY, ac.STATE_KV_KEY)["rev"] == 1
    assert ac._kv_open(raw, KEY, ac.DIRECT_KV_KEY) is None                     # chép sang khoá khác: bỏ
    forged = json.loads(raw)
    forged["k"] = ac._kv_target(ac.DIRECT_KV_KEY)                               # sửa đích: chữ ký hỏng
    assert ac._kv_open(json.dumps(forged), KEY, ac.DIRECT_KV_KEY) is None
    assert ac._kv_seal_time(raw) is not None


def test_legacy_seals_are_accepted_only_until_the_cutoff(monkeypatch):
    raw = ac._kv_seal(KEY, {"x": 1})                                            # bản cũ: chỉ ký thân
    monkeypatch.setattr(ac.time, "time", lambda: ac.KV_LEGACY_SEAL_UNTIL - 60)
    assert ac._kv_open(raw, KEY, ac.STATE_KV_KEY) == {"x": 1}
    monkeypatch.setattr(ac.time, "time", lambda: ac.KV_LEGACY_SEAL_UNTIL + 60)
    assert ac._kv_open(raw, KEY, ac.STATE_KV_KEY) is None


def test_restoring_direct_state_from_an_old_kv_copy_alerts(monkeypatch):
    kv = _kv(monkeypatch)
    sent = []
    monkeypatch.setattr(ac, "_telegram_send", lambda m: sent.append(m) or True)
    monkeypatch.setattr(ac.time, "time", lambda: NOW)
    kv[ac.DIRECT_KV_KEY] = ac._kv_seal(KEY, {"rev": 3, "pending": []}, ac.DIRECT_KV_KEY)
    monkeypatch.setattr(ac.time, "time", lambda: NOW + ac.DIRECT_KV_STALE_SEC + 60)
    st = {}
    assert ac.direct_kv_hydrate(st, KEY) and st[fd.KEY]["rev"] == 3 and any("niêm phong" in m for m in sent)
