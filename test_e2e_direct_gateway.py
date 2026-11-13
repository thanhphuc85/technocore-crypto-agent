"""E2E: main() THẬT ở testnet qua nhiều lượt, bật CÙNG LÚC gateway (đứng tên broker) và mua trực tiếp.

Thế giới giả ở tầng I/O: gateway giả (test_flop_gateway.FakeGateway), CLI flop giả (flop_direct._run), explorer giả
(requests.get). Phiên trực tiếp settle hợp tác ngay: ví mất CẢ escrow, miner nhận 99%, 1% phí kiểm toán; explorer
cộng minerPaid vào inference-spend của ví. Chi qua gateway KHÔNG vào chain của ví (đúng hiện trạng devnet: broker)."""
import json

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


@pytest.mark.parametrize("gateway_own,scenario", [(False, "normal"), (True, "normal"), (False, "cli_timeout"),
                                                   (False, "chain_reset")])
def test_gateway_and_direct_together_over_many_runs(monkeypatch, tmp_path, gateway_own, scenario):
    """cli_timeout: lượt 3 CLI bị ngắt sau khi kênh đã settle trên chain (agent không biết id) — kênh mồ côi phải
    được tìm lại và ghi sổ, và phần chain tăng đó KHÔNG được đọc thành gateway đứng tên ví mình. Sau đó miner bị giữ
    6 giờ nên mọi kênh đã ghi sổ: sổ = chain đến từng đơn vị raw — trước đây float làm sổ vượt chain ~1e-14 và 3 lần
    đọc "thấp hơn mốc" mở kỳ chain mới nhầm."""
    env = {"TESTNET_ENABLED": "true", "FLOP_SESSION_ENABLED": "on", "FLOP_WALLET_USE_DID": "on",
           "FLOP_FAUCET_ENABLED": "off", "FLOP_GATEWAY_PROMPTS": "Q one|Q two|Q three",
           "FLOP_GATEWAY_CALL_GAP_SEC": "0", "FLOP_GATEWAY_SKIP_PCT": "0", "FLOP_PACE_JITTER_PCT": "0",
           "TOKEN_LEDGER_FILE": str(tmp_path / "led.json"), "FLOP_PACER_FILE": str(tmp_path / "p.json"),
           "FLOP_DIRECT_ENABLED": "on", "FLOP_DIRECT_MINER": MINER, "FLOP_DIRECT_ENDPOINT": "https://miner.example",
           "FLOP_DIRECT_MODEL": "Qwen/Qwen3-14B", "FLOP_DIRECT_MAX_PER_DAY": "2000", "FLOP_DIRECT_PACE": "off",
           "FLOP_DIRECT_MAX_PER_RUN": "1", "FLOP_TESTNET_RADAR": "off"}
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
    tg = []
    monkeypatch.setattr(ac, "_telegram_send", lambda t: tg.append(t) or True)
    monkeypatch.setattr(ac, "_server_ok_count", 1)
    monkeypatch.setattr(ac, "build_market_context", lambda *a, **k: "BTC $1")

    # --- gateway giả (đứng tên broker: không đụng chain của ví) ---
    fake = FakeGateway(network="testnet", quota=300.0, expires_ms=0, cost=6.5)
    fake.receipts, fake.receipt_state = [], "settled"
    real = fake.__call__

    def gw_http(method, url, headers=None, json=None, timeout=20):
        if url.endswith("/keys/me") and method == "GET":
            return Resp(data={"credential": "owner", "remaining_quota": fake.quota, "expires_at": None})
        r = real(method, url, headers=headers, json=json, timeout=timeout)
        if gateway_own and method == "POST" and url.endswith("/chat/completions") and r.status_code == 200:
            chain["spent"] += int(6.5 * RAW)               # gateway đứng tên VÍ MÌNH: chain cộng cho ví,
            chain["balance"] -= int(6.5 * RAW)             # và kênh đứng tên ví thì ví trả escrow
        return r
    fake.override = {}
    monkeypatch.setattr(ac, "_gateway_http", gw_http)

    # --- chain + explorer giả ---
    addr = gw.wallet_address(gw.wallet_key(ac.load_private_key()))
    chain = {"balance": 1000 * RAW, "spent": 0, "channels": {}}
    tm.credit("1000")

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
        if url.endswith(f"/api/accounts/{addr}/inference-spend"):
            return R({"address": addr, "totalSpentRaw": str(chain["spent"])})
        if url.endswith(f"/api/accounts/{addr}"):
            return R({"address": addr, "balance": str(chain["balance"])})
        if "/api/compute-channels/" in url:
            cid = url.rsplit("/", 1)[-1]
            c = chain["channels"].get(cid)
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
    import hashlib
    import time as _clock

    def cli(cmd, envx, timeout):
        if "import" in cmd:
            return 0, json.dumps({"ok": True, "address": addr}), ""
        esc = int(float(cmd[cmd.index("--max-flop") + 1]) * RAW)
        n[0] += 1
        cid = "0x" + hashlib.sha256(f"channel-{n[0]}".encode()).hexdigest()
        paid = esc * 99 // 100
        chain["balance"] -= esc + 8 * 10 ** 15            # escrow + phí mở kênh
        chain["spent"] += paid
        chain["channels"][cid] = {"agent": addr, "miner": MINER, "status": "settled", "paymentStatus": "PAID",
                                  "observationHead": "finalized", "minerPaid": str(paid), "burned": "0",
                                  "escrow": str(esc), "aggregateGn": "30000", "capGflopsPerSec": 130,
                                  "cooperative": True, "openedAt": int(_clock.time()) * 1000}
        if scenario == "cli_timeout" and n[0] == 3:
            return None, "", ""                                # CLI bị ngắt: kênh đã có trên chain, agent không biết id
        return 0, json.dumps({"ok": True, "channel": cid, "completion_tokens": "900", "cooperative": True}), ""
    monkeypatch.setattr(fd, "_run", cli)

    import time as _t
    t0 = 1_800_000_000
    runs = 12 if scenario == "chain_reset" else 8
    old_spent = 0
    for i in range(runs):
        if scenario == "chain_reset" and i == 5:
            # testnet reset (không có genesis để so): chain mới, kênh cũ biến mất, tổng chi đếm lại từ 0. Agent cần
            # CHAIN_RESET_READINGS lần đọc thấp mới mở kỳ mới — các phiên ghi sổ trong lúc đó thuộc chain MỚI.
            old_spent = chain["spent"]
            chain.update(spent=0, channels={}, balance=1000 * RAW)
        monkeypatch.setattr(_t, "time", lambda i=i: t0 + i * 3600)
        ac.main()

    led = tm.load_ledger()
    pend_direct = [p for p in (state.get(fd.KEY) or {}).get("pending") or [] if p["channel"] in chain["channels"]]
    if scenario == "chain_reset":
        new_refs = [e for e in led.get("entries", []) if e.get("via") == "direct" and e.get("ref") in chain["channels"]]
        new_booked = sum(float(e["amount"]) for e in new_refs)
        unbooked_new = sum(int(chain["channels"][p["channel"]]["minerPaid"]) for p in pend_direct) / RAW
        assert old_spent > 0 and abs(new_booked - (chain["spent"] / RAW - unbooked_new)) < 1e-9
        assert float(led["direct_epoch_done"]["FLOP"]) == pytest.approx(new_booked)   # kỳ mới biết chi trực tiếp
        assert float((led.get("gateway_attributed") or {}).get("FLOP", "0")) == 0.0   # broker: không xác nhận nhầm
        return
    unbooked_paid = sum(int(chain["channels"][p["channel"]]["minerPaid"]) for p in pend_direct) / RAW
    unbooked_esc = sum(int(chain["channels"][p["channel"]]["escrow"]) for p in pend_direct) / RAW
    sess = float(led.get("session_cumulative", {}).get("FLOP", "0"))
    bal = float(led["balances"]["FLOP"])
    refs = [e.get("ref") for e in led.get("entries", []) if e.get("via") == "direct"]
    assert len(refs) == len(set(refs)) and len(refs) + len(pend_direct) == n[0]   # mọi kênh: ghi sổ đúng 1 lần / chờ
    assert n[0] == (3 if scenario == "cli_timeout" else runs)              # timeout: miner bị giữ 6h sau lượt 3
    assert not led.get("gateway_chain_low", {}).get("FLOP")                # không có lần "đọc thấp" nào
    if not gateway_own:
        assert abs(sess - (chain["spent"] / RAW - unbooked_paid)) < 1e-6   # thước đo airdrop = chain đã ghi sổ
    gw_spent = float(((state.get(gw.KEY) or {}).get("stats") or {}).get("spent") or 0)
    gw_conf = float((led.get("gateway_attributed") or {}).get("FLOP", "0"))
    gw_unconf = (gw_spent - gw_conf) if gateway_own else 0.0               # gateway đã chi, chain đã trừ, sổ chưa xác nhận
    assert abs((bal - unbooked_esc - gw_unconf) - (chain["balance"] / RAW) - 0.008 * n[0]) < 1e-6
    gw_att = float((led.get("gateway_attributed") or {}).get("FLOP", "0"))
    gw_pend = float((led.get("gateway_unattributed") or {}).get("FLOP", "0"))
    gw_spent_all = float(((state.get(gw.KEY) or {}).get("stats") or {}).get("spent") or 0)
    if gateway_own:                                                         # đứng tên ví mình: được xác nhận, cổng mở
        assert gw_att > 0 and gw_pend <= 6.5 + 1e-6 and not any("ĐÃ DỪNG mua" in m for m in tg)
        # chain = trực tiếp + gateway đã chi; sổ thiếu: kênh trực tiếp chưa ghi + gateway chưa có biên nhận/xác nhận
        assert abs(sess - (chain["spent"] / RAW - unbooked_paid - (gw_spent_all - gw_att))) < 1e-6
    else:                                                                   # broker: không xác nhận gì
        assert gw_att == 0.0
    assert not any("MUA TRỰC TIẾP" in m and "⚠️" in m for m in tg)                         # không báo động giả
    assert not any("chi TỤT LẠI" in m for m in tg)        # cổng đứng tên dừng mua đã có tin riêng, không báo trùng
