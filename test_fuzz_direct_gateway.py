"""Kiểm thử NGẪU NHIÊN main() thật, bật cùng lúc gateway (đứng tên broker) và mua trực tiếp, 42 lượt cách nhau 6 giờ.

Mỗi lượt có thể gặp nhiều sự cố cùng lúc: CLI bị ngắt sau khi kênh đã settle, kênh bị hoàn, settle cưỡng bức (rồi
finalize sau 7 ngày), explorer sập, mất Actions cache (state.json + sổ cục bộ; KV còn), Telegram lỗi, chain reset.
Bất biến: gateway đứng tên broker KHÔNG BAO GIỜ được xác nhận; không kênh nào ghi sổ hai lần; mỗi kênh PAID trên
chain hoặc đã ghi sổ ĐÚNG số chain trả, hoặc còn được theo dõi (danh sách chờ / khoản giữ của lượt bị ngắt); bước
direct không ném lỗi. FUZZ_SEEDS=<n> để chạy nhiều seed hơn (mặc định 8; đã chạy 130 seed khi viết)."""
import hashlib
import json
import random
import time as _clock
from decimal import Decimal

import pytest

import agent_cron as ac
import flop_direct as fd
import flop_gateway as gw
import token_manager as tm
from test_agent_cron import SEED_HEX
from test_flop_gateway import FakeGateway, Resp

MINER = gw.ss58_encode(bytes([7]) * 32)
MH = "0x" + "11" * 32
RAW = 10 ** 18
FEE = 8 * 10 ** 15


@pytest.mark.parametrize("seed", list(range(int(__import__("os").environ.get("FUZZ_SEEDS", "8")))))
def test_fuzz(monkeypatch, tmp_path, seed, capsys):
    rnd = random.Random(seed)
    env = {"TESTNET_ENABLED": "true", "FLOP_SESSION_ENABLED": "on", "FLOP_WALLET_USE_DID": "on",
           "FLOP_FAUCET_ENABLED": "off", "FLOP_GATEWAY_PROMPTS": "Q one|Q two|Q three",
           "FLOP_GATEWAY_CALL_GAP_SEC": "0", "FLOP_GATEWAY_SKIP_PCT": "0", "FLOP_PACE_JITTER_PCT": "0",
           "TOKEN_LEDGER_FILE": str(tmp_path / "led.json"), "FLOP_PACER_FILE": str(tmp_path / "p.json"),
           "FLOP_DIRECT_ENABLED": "on", "FLOP_DIRECT_MINER": MINER, "FLOP_DIRECT_ENDPOINT": "https://miner.example",
           "FLOP_DIRECT_MODEL": "Qwen/Qwen3-14B", "FLOP_DIRECT_MAX_PER_DAY": "2000",
           "FLOP_DIRECT_PACE": rnd.choice(["on", "off"]), "FLOP_DIRECT_MAX_PER_RUN": str(rnd.choice([1, 2])),
           "FLOP_TESTNET_RADAR": "off"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FLOP_SESSION_BACKEND", raising=False)
    monkeypatch.setattr(ac, "SEED_HEX", SEED_HEX)
    monkeypatch.setattr(ac, "RUNNER_ROLE", "primary")
    monkeypatch.setattr(ac, "TESTNET_RADAR", False)
    monkeypatch.setattr(ac, "TESTNET_RADAR_H", 0)
    state = {}
    monkeypatch.setattr(ac, "load_state", lambda: json.loads(json.dumps(state)))
    monkeypatch.setattr(ac, "save_state", lambda d: state.update(json.loads(json.dumps(d))))
    kv = {}
    monkeypatch.setattr(ac, "kv_set", lambda pk, did, k, v: kv.update({k: v}) or True)
    monkeypatch.setattr(ac, "kv_get", lambda k: kv.get(k))
    for name in ("write_heartbeat", "_write_summary", "check_price_alert"):
        monkeypatch.setattr(ac, name, lambda *a, **k: None)
    monkeypatch.setattr(ac, "broadcast_telemetry", lambda *a, **k: True)
    monkeypatch.setattr(ac, "broadcast_manifest", lambda *a, **k: True)
    monkeypatch.setattr(ac, "auto_respond", lambda *a, **k: (0, 0))
    tg_ok = [True]
    monkeypatch.setattr(ac, "_telegram_send", lambda t: tg_ok[0])
    monkeypatch.setattr(ac, "_server_ok_count", 1)
    monkeypatch.setattr(ac, "build_market_context", lambda *a, **k: "BTC $1")

    fake = FakeGateway(network="testnet", quota=300.0, expires_ms=0, cost=6.5)
    fake.receipts, fake.receipt_state = [], "settled"
    real = fake.__call__

    def gw_http(method, url, headers=None, json=None, timeout=20):
        if url.endswith("/keys/me") and method == "GET":
            return Resp(data={"credential": "owner", "remaining_quota": fake.quota, "expires_at": None})
        return real(method, url, headers=headers, json=json, timeout=timeout)
    fake.override = {}
    monkeypatch.setattr(ac, "_gateway_http", gw_http)

    addr = gw.wallet_address(gw.wallet_key(ac.load_private_key()))
    chain = {"balance": 5000 * RAW, "spent": 0, "channels": {}, "down": False, "gen": 0}
    tm.credit("5000")

    class R:
        def __init__(self, data, status=200):
            self._d, self.status_code = data, status
            self.text = json.dumps(data)
            self.headers = {}

        def json(self):
            return self._d

        def raise_for_status(self):
            if self.status_code >= 400:
                raise ac.requests.HTTPError(str(self.status_code))

    def get(url, params=None, **k):
        params = params or {}
        if chain["down"] and "explorer" in url:
            return R({"error": "bad gateway"}, 502)
        if url.endswith(f"/api/accounts/{addr}/inference-spend"):
            return R({"address": addr, "totalSpentRaw": str(chain["spent"])})
        if url.endswith(f"/api/accounts/{addr}"):
            return R({"address": addr, "balance": str(chain["balance"])})
        if "/api/compute-channels/" in url:
            c = chain["channels"].get(url.rsplit("/", 1)[-1])
            return R({"data": c}) if c else R({"error": "not found"}, 404)
        if url.endswith("/api/compute-channels"):
            if params.get("agent"):
                return R({"data": [dict(c, channelId=k2) for k2, c in chain["channels"].items()]})
            return R({"data": [{"miner": MINER, "capGflopsPerSec": 130}]})
        if url.endswith("/api/session-offers"):
            return R({"data": [{"offer": {"miner": MINER, "modelHash": MH, "minEscrow": str(5 * 10 ** 16),
                                          "maxTurns": 1, "maxDuration": "300", "expiry": str(10 ** 12),
                                          "precision": 0}, "minerSignature": "0x" + "ee" * 64}]})
        if url.endswith("/api/models"):
            return R({"models": [{"modelHash": MH, "epochPricePerGflop": "3000000000000000"}]})
        if url.endswith("/api/stats"):
            return R({"latestBlockNumber": 5000})
        return R({}, 404)
    monkeypatch.setattr(ac.requests, "get", get)

    n = [0]
    events = []
    born, cur_run = {}, [0]

    def cli(cmd, envx, timeout):
        if "import" in cmd:
            return 0, json.dumps({"ok": True, "address": addr}), ""
        if "finalize" in cmd:
            cid = cmd[-1]
            c = chain["channels"].get(cid)
            if not c or c["paymentStatus"] != "LOCKED":
                return 1, "", json.dumps({"ok": False, "message": "channel not in SettlementPosted"})
            esc = int(c["escrow"])
            paid = esc * 30 // 100
            chain["balance"] += esc - paid
            chain["spent"] += paid
            c.update(status="settled", paymentStatus="PAID", minerPaid=str(paid))
            return 0, json.dumps({"ok": True, "channel_id": cid, "tx_hash": "0x" + "aa" * 32}), ""
        esc = int(Decimal(cmd[cmd.index("--max-flop") + 1]) * RAW)
        n[0] += 1
        cid = "0x" + hashlib.sha256(f"{seed}-{chain['gen']}-{n[0]}".encode()).hexdigest()
        chain["balance"] -= esc + FEE
        base = {"agent": addr, "miner": MINER, "observationHead": "finalized", "burned": "0", "escrow": str(esc),
                "aggregateGn": "30000", "capGflopsPerSec": 130, "openedAt": int(_clock.time()) * 1000}
        kind = rnd.choices(["ok", "timeout", "refund", "force"], [65, 10, 15, 10])[0]
        events.append(kind)
        born[cid] = (cur_run[0], kind)
        if kind == "refund":
            chain["balance"] += esc
            chain["channels"][cid] = dict(base, status="timedout", paymentStatus="REFUNDED", minerPaid="0")
            return 1, "", json.dumps({"ok": False, "code": "SESSION_FAILED", "channel": cid,
                                      "message": "channel refunded: verified work cannot settle"})
        if kind == "force":
            chain["channels"][cid] = dict(base, status="settled", paymentStatus="LOCKED", minerPaid="0",
                                          cooperative=False)
            return 0, json.dumps({"ok": True, "channel": cid, "completion_tokens": "900", "cooperative": False}), ""
        paid = esc * 99 // 100
        chain["spent"] += paid
        chain["channels"][cid] = dict(base, status="settled", paymentStatus="PAID", minerPaid=str(paid),
                                      cooperative=True)
        if kind == "timeout":
            return None, "", ""
        return 0, json.dumps({"ok": True, "channel": cid, "completion_tokens": "900", "cooperative": True}), ""
    monkeypatch.setattr(fd, "_run", cli)

    import time as _t
    import os as _os
    t0 = 1_800_000_000
    runs = 40
    reset_at = rnd.choice([None, None, rnd.randrange(5, 30)])
    old_channels = set()
    for i in range(runs):
        chain["down"] = rnd.random() < 0.08
        tg_ok[0] = rnd.random() > 0.2
        if rnd.random() < 0.05:
            events.append(f"cache_loss@{i}")
            state.clear()
            if _os.path.exists(_os.environ["TOKEN_LEDGER_FILE"]):
                _os.remove(_os.environ["TOKEN_LEDGER_FILE"])
        if reset_at == i:
            events.append(f"chain_reset@{i}")
            old_channels |= set(chain["channels"])
            chain.update(spent=0, channels={}, gen=chain["gen"] + 1)
        monkeypatch.setattr(_t, "time", lambda i=i: t0 + i * 6 * 3600)
        cur_run[0] = i
        ac.main()
    chain["down"] = False                                          # 2 lượt yên ổn cuối để đối soát
    for i in range(runs, runs + 2):
        monkeypatch.setattr(_t, "time", lambda i=i: t0 + i * 6 * 3600)
        cur_run[0] = i
        ac.main()

    out = capsys.readouterr().out
    led = tm.load_ledger()
    g = state.get(fd.KEY) or {}
    pend = {p["channel"] for p in g.get("pending") or []}
    entries = [e for e in led.get("entries", []) if e.get("via") == "direct"]
    refs = [e["ref"] for e in entries]
    by_ref = {e["ref"]: Decimal(e["amount"]) for e in entries}
    problems = []
    if float((led.get("gateway_attributed") or {}).get("FLOP", "0")) != 0.0:
        problems.append(f"gateway xác nhận nhầm {led.get('gateway_attributed')}")
    if len(refs) != len(set(refs)):
        problems.append("ghi sổ trùng kênh")
    hashes = set(led.get("gateway_refs") or []) | set(led.get("direct_refs") or [])
    for cid, c in chain["channels"].items():
        if c["paymentStatus"] == "PAID":
            booked = hashlib.sha256(f"direct:{cid}".encode()).hexdigest()[:16] in hashes
            if cid in by_ref and by_ref[cid] != Decimal(int(c["minerPaid"])) / Decimal(RAW):
                problems.append(f"sai số tiền {cid[:10]}: sổ {by_ref[cid]} chain {int(c['minerPaid']) / RAW}")
            held = bool(g.get("unknown_escrow")) and int(c["openedAt"]) // 1000 >= int(g.get("orphan_since") or 0) - 300
            if not booked and cid not in pend and not held:
                problems.append(f"kênh PAID {cid[:10]} không ghi sổ cũng không theo dõi (sinh {born.get(cid)})")
    for bad in ("[direct] bỏ qua (", "Traceback", "[real-spend] bộ canh mua trực tiếp bỏ qua"):
        if bad in out:
            problems.append(f"lỗi trong log: {bad} -> {out[out.index(bad):out.index(bad) + 160]!r}")
    assert not problems, f"seed {seed} events {events[:60]}: {problems}"
