"""Test flop_direct — mua compute TRỰC TIẾP từ một miner (kênh đứng tên ví agent).

CLI chính thức và explorer được giả (runner/fetch_channel tiêm vào): không Docker, không Node, không mạng."""
import json
import os

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


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("FLOP_DIRECT") or k.startswith("FLOP_WALLET"):
            monkeypatch.delenv(k)
    for k, v in {"FLOP_DIRECT_ENABLED": "on", "FLOP_DIRECT_MINER": MINER,
                 "FLOP_DIRECT_ENDPOINT": "https://miner.example", "FLOP_DIRECT_MODEL": "qwen/qwen2.5-0.5b-instruct",
                 "FLOP_WALLET_USE_DID": "on", "TESTNET_ENABLED": "true",
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


def step(state, cli, explorer=None, book=True, network="testnet", **kw):
    explorer = explorer if explorer is not None else {}
    alerts = kw.pop("alerts", [])

    def book_fn(amount, channel, model):
        return tm.record_direct_spend(amount, f"inference-session direct {model}", channel)

    return fd.run_step(state, agent_key=KEY, now=kw.pop("now", NOW), prompt=kw.pop("prompt", "What is 2+2?"),
                       network=network, fetch_channel=lambda cid: explorer.get(cid),
                       book_fn=book_fn if book else None, runner=cli, alert=alerts.append,
                       log=lambda m: None, **kw)


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
    ("FLOP_DIRECT_MAX_FLOP", "9", "<= 5"),
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
    assert st[fd.KEY]["wait_until"] == NOW + fd.UNSUPPORTED_BACKOFF_SEC


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
    r = step(st, CLI(), explorer={CH: paid(head="best")}, now=NOW + 300)
    assert r["outcome"] == "pending" and r["booked"] == 0


def test_refunded_channel_is_dropped_with_zero_spend():
    st = {}
    step(st, CLI())
    r = step(st, CLI(), explorer={CH: {"agent": ADDR, "status": "settled", "paymentStatus": "LOCKED",
                                       "observationHead": "finalized", "minerPaid": "0", "burned": "0"}},
             now=NOW + 300)
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

def test_an_unfinished_channel_blocks_a_new_one_and_alerts_once_when_old():
    st, alerts = {}, []
    step(st, CLI())
    cli = CLI()
    assert step(st, cli, now=NOW + 300)["outcome"] == "pending" and cli.calls == []
    step(st, cli, now=NOW + fd.PENDING_ALERT_SEC + 10, alerts=alerts)
    step(st, cli, now=NOW + fd.PENDING_ALERT_SEC + 400, alerts=alerts)
    assert len(alerts) == 1 and "khoá" in alerts[0] and cli.calls == []


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
    assert st[fd.KEY]["wait_until"] == NOW + fd.ERROR_BACKOFF_SEC


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
    assert st[fd.KEY]["unknown_until"] == NOW + fd.UNKNOWN_HOLD_SEC


def test_unsupported_cli_option_stops_for_a_day():
    err = json.dumps({"ok": False, "code": "CLI_ERROR", "message": "error: unknown option '--via'"})
    st, alerts = {}, []
    assert step(st, CLI(session=(1, "", err)), alerts=alerts)["outcome"] == "unsupported" and alerts
    assert st[fd.KEY]["wait_until"] == NOW + fd.UNSUPPORTED_BACKOFF_SEC


def test_success_without_a_valid_channel_id_is_treated_as_unknown():
    st = {}
    r = step(st, CLI(session=(0, json.dumps({"ok": True, "channel": "nope"}), "")))
    assert r["outcome"] == "unknown_outcome" and st[fd.KEY]["unknown_until"] > NOW


def test_daily_escrow_cap(monkeypatch):
    monkeypatch.setenv("FLOP_DIRECT_MAX_FLOP", "0.5")
    monkeypatch.setenv("FLOP_DIRECT_MAX_PER_DAY", "1")
    st = {}
    for i in range(3):
        step(st, CLI(), explorer={CH: paid()}, now=NOW + i * 300)
        st[fd.KEY]["pending"] = []
    assert st[fd.KEY]["last_outcome"] == "daily_cap" and st[fd.KEY]["day"]["escrow"] == 1.0


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

    monkeypatch.setattr(ac.requests, "get", lambda url, **k: seen.append(url) or _R(paid()))
    monkeypatch.setattr(ac, "kv_set", lambda *a, **k: True)
    monkeypatch.setattr(ac, "_telegram_send", lambda m: True)
    st = {}
    assert ac.direct_session_step(st, KEY, NOW) == "opened"
    assert "q1" in cli.sessions()[0]["cmd"][cli.sessions()[0]["cmd"].index("--prompt") + 1]
    status = ac.direct_session_step(st, KEY, NOW + 300)
    assert status.startswith("pending") or status.startswith("opened")
    assert any(u.endswith(f"/api/compute-channels/{CH}") and "explorer.testnet.flop.network" in u for u in seen)
    assert tm.load_ledger()["session_cumulative"]["FLOP"] == "0.021"


def test_agent_step_is_off_by_default(monkeypatch):
    monkeypatch.delenv("FLOP_DIRECT_ENABLED")
    assert ac.direct_session_step({}, KEY, NOW) == "off"
