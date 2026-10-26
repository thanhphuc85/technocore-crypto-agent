"""E2E: main() THẬT ở chế độ testnet qua nhiều lượt, gateway + faucet giả ở tầng HTTP.

Test từng hàm không bắt được lỗi ở CHỖ NỐI (thứ tự faucet -> gateway -> ghi sổ -> KV, trạng thái
giữa các lượt, cảnh báo). Kịch bản: lượt 1 nhận faucet đúng ví DID + mua; lượt 2 biên nhận settle
-> ghi sổ; lượt 3 không ghi trùng."""
import json
import pytest
import agent_cron as ac
import flop_gateway as gw
import token_manager as tm
from test_flop_gateway import FakeGateway, Resp
from test_agent_cron import SEED_HEX

def test_three_testnet_runs_end_to_end(monkeypatch, tmp_path):
    env = {"TESTNET_ENABLED": "true", "FLOP_SESSION_ENABLED": "on", "FLOP_WALLET_USE_DID": "on",
           "FLOP_FAUCET_ENABLED": "on", "FLOP_FAUCET_URL": "https://faucet.flop.network",
           "FLOP_FAUCET_TIER": "quick", "FLOP_GATEWAY_PROMPTS": "Q one|Q two|Q three",
           "FLOP_GATEWAY_CALL_GAP_SEC": "0", "FLOP_GATEWAY_SKIP_PCT": "0",
           "TOKEN_LEDGER_FILE": str(tmp_path / "led.json"), "FLOP_FAUCET_STATE": str(tmp_path / "f.json"),
           "FLOP_PACER_FILE": str(tmp_path / "p.json"), "FLOP_FAUCET_JITTER_MIN": "0"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FLOP_SESSION_BACKEND", raising=False)
    monkeypatch.delenv("FLOP_FAUCET_ADDRESS", raising=False)
    monkeypatch.setattr(ac, "SEED_HEX", SEED_HEX)
    monkeypatch.setattr(ac, "RUNNER_ROLE", "primary")
    state = {}
    monkeypatch.setattr(ac, "load_state", lambda: json.loads(json.dumps(state)))
    def save(d):
        state.update(json.loads(json.dumps(d)))
    monkeypatch.setattr(ac, "save_state", save)
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
    # Gateway giả: mạng testnet, quota 300, biên nhận settle ngay lượt sau
    fake = FakeGateway(network="testnet", quota=300.0, expires_ms=0, cost=6.5)
    fake.receipts, fake.receipt_state = [], "settled"
    fake.override[("GET", "/v1/keys/me")] = None
    real = fake.__call__
    def gw_http(method, url, headers=None, json=None, timeout=20):
        if url.endswith("/keys/me") and method == "GET":
            fake.calls.append((method, "/v1/keys/me", headers or {}, json, url))
            return Resp(data={"credential": "owner", "remaining_quota": fake.quota, "expires_at": None})
        return real(method, url, headers=headers, json=json, timeout=timeout)
    fake.override = {}
    monkeypatch.setattr(ac, "_gateway_http", gw_http)
    # Faucet giả
    faucet_posts = []
    class FR:
        def __init__(self, data, status=200): self._d, self.status_code, self.headers, self.content = data, status, {}, b"x"
        def json(self): return self._d
        def raise_for_status(self): pass
    def fake_post(url, json=None, **k):
        faucet_posts.append((url, json))
        return FR({"success": True, "amount": 25, "tier": "anonymous", "txHash": "0x" + "ab" * 32})
    monkeypatch.setattr(ac.requests, "post", fake_post)
    monkeypatch.setattr(ac, "_faucet_cooldown_s", lambda url, linked: 86460)
    import time as _t
    t0 = 1_800_000_000
    for i in range(3):
        monkeypatch.setattr(_t, "time", lambda i=i: t0 + i * 300)
        ac.main()
    led = tm.load_ledger()
    g = state.get(gw.KEY) or {}
    assert len(faucet_posts) == 1 and faucet_posts[0][0].endswith("/fund")
    assert faucet_posts[0][1] == {"address": gw.wallet_address(ac.load_private_key())}
    assert g.get("network") == "testnet" and (g.get("stats") or {}).get("calls", 0) >= 1
    assert float((led.get("session_cumulative") or {}).get("FLOP", 0)) > 0
    assert "token_ledger_testnet" in kv
    assert led["balances"]["FLOP"] == "18.5" and led["session_cumulative"]["FLOP"] == "6.5"   # 25 - 6.5
    assert (g.get("stats") or {}).get("receipts") == 1                   # settle ở lượt 2, không ghi trùng ở lượt 3
    assert sum("QUICK" in m for m in tg) == 1                             # cảnh báo mức quick đúng 1 lần
