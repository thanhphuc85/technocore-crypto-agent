"""flop_gateway: mua compute qua api.flop.network — gateway GIẢ, không mạng thật."""
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import flop_gateway as gw

SEED = "11" * 32
NOW = 1_800_000_000
BEARER = "sk-flop-SECRET-bearer-123"
SOON = (NOW + 2000) * 1000          # hạn gần -> phần đến hạn đủ cho 1 lần gọi ngay run này


def _key(byte="11"):
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(byte * 32))


class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._d, self.headers = status, data, headers or {}
        self.content = b"x" if data is not None else b""

    def json(self):
        return self._d


class FakeGateway:
    """Đủ hành vi của api.flop.network cho một nhịp: health, challenge, enroll, me, models,
    estimate, chat, receipts. `override[(method, path)] = Resp` để giả lỗi."""

    def __init__(self, network="devnet", quota=25.0, expires_ms=None, cost=0.9):
        self.network, self.quota, self.cost = network, quota, cost
        self.expires_ms = expires_ms if expires_ms is not None else (NOW + 20 * 3600) * 1000
        self.calls, self.override, self.challenge_addr, self.host = [], {}, None, "api.flop.network"
        self.report_cost = True                 # devnet thật (2026-10-24) KHÔNG trả usage.cost_flop
        self.receipts = [{"model": "m1", "prompt_hash": "p1", "tee_quote_hash": "t", "block_number": 7,
                          "tx_hash": "0xaa", "miner": "5MinerA"}]

    def __call__(self, method, url, headers=None, json=None, timeout=None):
        path = url.split("api.flop.network", 1)[-1].split("?")[0]
        self.calls.append((method, path, headers or {}, json, url))
        if (method, path) in self.override:
            return self.override[(method, path)]
        if path == "/health":
            return Resp(data={"status": "ok", "network": {"network": self.network}})
        if path == "/v1/enroll/wallet/challenge":
            addr = self.challenge_addr or url.split("address=")[1].split("&")[0]
            return Resp(data={"message": f"{self.host} wants you to enroll a FLOP devnet gateway key "
                                         f"with your account:\n{addr}\n\nAction: flop-devnet-enroll/v1",
                              "nonce": "ab" * 32, "expires_at": (NOW + 300) * 1000})
        if path == "/v1/enroll/wallet":
            return Resp(201, {"key": BEARER, "model": "m1", "remaining_quota": self.quota,
                              "expires_at": self.expires_ms})
        if path == "/v1/keys/me":
            return Resp(data={"credential": "owner", "remaining_quota": self.quota,
                              "expires_at": self.expires_ms})
        if path == "/v1/models":
            return Resp(data={"data": [{"id": "m1", "pricing": {"prompt": "0.006", "completion": "0.006"},
                                        "per_request_limits": {"completion_tokens": 8192}}]})
        if path == "/v1/models/estimate":
            return Resp(data={"estimated_input_tokens": 20, "estimated_cost_flop": 1.0})
        if path == "/v1/chat/completions":
            self.quota -= self.cost
            return Resp(data={"id": f"chatcmpl-{len(self.calls)}", "object": "chat.completion", "created": NOW,
                              "model": "m1", "choices": [{"index": 0, "finish_reason": "stop",
                                                          "message": {"role": "assistant", "content": "ok"}}],
                              "usage": {"prompt_tokens": 20, "completion_tokens": 100, "total_tokens": 120,
                                        **({"cost_flop": self.cost, "gflops_used": 3.2} if self.report_cost else {})}})
        if path == "/v1/keys/me/receipts":
            return Resp(data={"receipts": self.receipts})
        return Resp(404, {"error": "not_found", "message": path})

    def paths(self):
        return [p for _, p, *_ in self.calls]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("FLOP_WALLET_SEED_HEX", "FLOP_WALLET_USE_DID", "FLOP_GATEWAY_URL", "FLOP_GATEWAY_PURPOSE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FLOP_WALLET_SEED_HEX", SEED)
    monkeypatch.setenv("FLOP_GATEWAY_CALL_GAP_SEC", "0")


def _run(state, fake, booked=None, **kw):
    return gw.run_step(state, agent_key=_key("22"), http=fake, now=NOW, prompts=["q1", "q2", "q3"],
                       expect_network=kw.pop("expect", "devnet"),
                       book_fn=(lambda c, ref, m: booked.append((c, ref, m))) if booked is not None else None,
                       sleep=lambda s: None, log=lambda *a: None, **kw)


def test_ss58_of_the_main_did_key():
    did_pub = bytes.fromhex("0c3e2b0f2a48a7f6c47f4ff2d5d5b2c5bc1d43f86ec0c8d0f5a4b7ad6b4b1a3c")
    assert gw.ss58_encode(did_pub).startswith("5")
    # Địa chỉ của DID chính (đã đối chiếu ở faucet): khoá công khai -> SS58 prefix 42.
    import agent_cron as ac
    pub = ac.b58decode("z6Mkf13PqYooa8rr9oipQRbNSzzuVGkf3ZEWvPes5r87k9K4"[1:])[2:]
    assert gw.ss58_encode(pub) == "5CFP215CEbCtDduwG1Nzu5rmYXKfHc47wSATUi1oRvrkQnUP"
    assert ac.ss58_valid(gw.wallet_address(_key()))


def test_wallet_key_sources(monkeypatch):
    agent = _key("33")
    assert gw.wallet_address(gw.wallet_key(agent)) == gw.wallet_address(_key("11"))   # seed riêng thắng
    monkeypatch.setenv("FLOP_WALLET_SEED_HEX", "zz")
    assert gw.wallet_key(agent) is None                                  # seed hỏng: KHÔNG lùi về DID
    monkeypatch.delenv("FLOP_WALLET_SEED_HEX")
    assert gw.wallet_key(agent) is None                                  # chưa cấu hình
    monkeypatch.setenv("FLOP_WALLET_USE_DID", "on")
    assert gw.wallet_key(agent) is agent


def test_bearer_vault_only_opens_with_the_agent_key():
    blob = gw.seal_bearer(_key("22"), BEARER)
    assert BEARER not in json.dumps(blob)
    assert gw.open_bearer(_key("22"), blob) == BEARER
    assert gw.open_bearer(_key("44"), blob) is None                      # fork / khoá khác
    assert gw.open_bearer(_key("22"), {**blob, "c": blob["c"][:-4] + "AAAA"}) is None
    assert gw.open_bearer(_key("22"), None) is None


def test_pacing_math():
    due, win = gw.window_due(None, 24, NOW, NOW + 86400 + 1800, 300, 1800)
    assert due == pytest.approx(24 * 300 / 86400) and win["total"] == 24
    win["spent"] = 0.0                                                   # run này không chi được
    due2, _ = gw.window_due(win, 24, NOW + 300, NOW + 86400 + 1800, 300, 1800)
    assert due2 == pytest.approx(2 * 24 * 300 / 86400)                   # phần nhỏ được TÍCH LẠI
    assert gw.window_due(None, 5, NOW, NOW + 1900, 300, 1800)[0] == 5    # sát hạn -> chi hết
    assert gw.window_due(None, 0, NOW, NOW + 9999, 300, 1800)[0] == 0
    top, w2 = gw.window_due({**win, "spent": 20.0}, 30, NOW + 600, NOW + 86400 + 1800, 300, 1800)
    assert w2["total"] == 30 and w2["start"] == NOW + 600                # faucet nạp -> cửa sổ mới
    assert gw.horizon(NOW, None) == NOW - NOW % 86400 + 86400            # không hạn -> cuối ngày UTC
    assert gw.size_call(1.0, 20, 0.006, 0.006, 1024, 64) == 146
    assert gw.size_call(0.3, 20, 0.006, 0.006, 1024, 64) is None         # không đủ 64 token
    assert gw.size_call(100, 20, 0.006, 0.006, 1024, 64) == 1024         # kẹp trần


def test_happy_path_enrolls_signs_spends_and_never_stores_the_bearer(monkeypatch):
    monkeypatch.setenv("FLOP_GATEWAY_MAX_CALLS_PER_RUN", "2")
    fake, booked, state = FakeGateway(expires_ms=(NOW + 2000) * 1000), [], {}
    r = _run(state, fake, booked)
    assert r["outcome"] == "spent" and r["calls"] == 2 and r["spent"] == pytest.approx(1.8)
    # chữ ký challenge là ed25519 hợp lệ của VÍ, trên đúng câu được giao
    enroll = next(c for c in fake.calls if c[1] == "/v1/enroll/wallet")
    body = enroll[3]
    _key("11").public_key().verify(bytes.fromhex(body["signature"][2:]), body["message"].encode())
    assert body["address"] == gw.wallet_address(_key("11"))
    chats = [c for c in fake.calls if c[1] == "/v1/chat/completions"]
    assert all(c[2]["Authorization"] == f"Bearer {BEARER}" for c in chats)
    assert all(0 < c[3]["max_cost_flop"] <= r["due"] / 2 * 1.1 + 1e-9 for c in chats)   # trần do gateway giữ
    assert [b[0] for b in booked] == [0.9, 0.9] and booked[0][1].startswith("chatcmpl-")
    assert BEARER not in json.dumps(state)                               # chỉ bản mã
    assert state[gw.KEY]["stats"]["receipts"] == 1


def test_second_run_reuses_the_sealed_bearer_and_receipts_count_once(monkeypatch):
    fake, state = FakeGateway(expires_ms=(NOW + 2000) * 1000), {}
    _run(state, fake)
    fake.calls.clear()
    r = _run(state, fake)
    assert "/v1/enroll/wallet" not in fake.paths() and r["receipts_new"] == 0


def test_expiring_bearer_is_re_enrolled():
    fake, state = FakeGateway(expires_ms=(NOW + 60) * 1000), {}
    _run(state, fake)
    fake.calls.clear()
    _run(state, fake)
    assert "/v1/enroll/wallet" in fake.paths()


def test_wrong_network_spends_nothing():
    fake, booked = FakeGateway(network="devnet"), []
    r = _run({}, fake, booked, expect="testnet")
    assert r["outcome"] == "network_mismatch" and fake.paths() == ["/health"] and booked == []


def test_refuses_to_sign_a_challenge_for_another_address():
    fake = FakeGateway()
    fake.challenge_addr = "5SomeoneElse"
    r = _run({}, fake)
    assert r["outcome"] == "error" and "/v1/enroll/wallet" not in fake.paths()


def test_payment_required_is_recorded_never_paid():
    fake = FakeGateway(expires_ms=SOON)
    fake.override[("POST", "/v1/chat/completions")] = Resp(402, {
        "error": "payment_required", "price": 1.2, "asset": "FLOP", "hash_lock": "0xhl",
        "timeout_blocks": 50, "destination": "5Payee"})
    state = {}
    r = _run(state, fake, [])
    assert r["outcome"] == "payment_required" and r["payment"]["destination"] == "5Payee"
    assert state[gw.KEY]["wait_until"] > NOW
    assert _run(state, fake)["outcome"] == "backoff"                    # không gõ lại ngay


def test_401_drops_the_bearer_and_429_backs_off():
    fake, state = FakeGateway(), {}
    fake.override[("GET", "/v1/keys/me")] = Resp(401, {"error": "invalid_credential"})
    assert _run(state, fake)["outcome"] == "auth_reset" and "vault" not in state[gw.KEY]
    fake2, st2 = FakeGateway(expires_ms=SOON), {}
    fake2.override[("POST", "/v1/chat/completions")] = Resp(429, {"error": "overloaded"}, {"Retry-After": "120"})
    assert _run(st2, fake2)["outcome"] == "rate_limited" and st2[gw.KEY]["wait_until"] == NOW + 120


def test_missing_cost_is_not_booked_from_a_guess():
    fake, booked = FakeGateway(cost=0, expires_ms=SOON), []
    r = _run({}, fake, booked)
    assert booked == [] and r["last"]["usage_keys"]


def test_pace_wait_when_due_is_below_one_small_call():
    fake = FakeGateway(quota=0.5, expires_ms=(NOW + 30 * 86400) * 1000)
    r = _run({}, fake)
    assert r["outcome"] == "pace_wait" and "/v1/chat/completions" not in fake.paths()


def test_unconfigured_wallet_makes_no_requests(monkeypatch):
    monkeypatch.delenv("FLOP_WALLET_SEED_HEX")
    fake = FakeGateway()
    assert _run({}, fake)["outcome"] == "unconfigured" and fake.calls == []


def test_spreads_a_day_of_quota_without_dumping_at_the_end(monkeypatch):
    # 25 FLOP hạn 20h, run 5 phút: phải chi RẢI suốt cửa sổ (không dồn vào vài giờ cuối) và
    # hết quota trước hạn.
    fake, state, t = FakeGateway(quota=25.0, expires_ms=(NOW + 20 * 3600) * 1000, cost=0.55), {}, NOW
    spent_by_hour = {}
    while t < NOW + 20 * 3600 and fake.quota > 0.5:
        before = fake.quota
        gw.run_step(state, agent_key=_key("22"), http=fake, now=t, prompts=["q"], expect_network="devnet",
                    sleep=lambda s: None, log=lambda *a: None)
        spent_by_hour[(t - NOW) // 3600] = spent_by_hour.get((t - NOW) // 3600, 0) + before - fake.quota
        t += 300
    assert fake.quota <= 0.55 + 1e-9                                     # gần hết quota trước hạn
    first_half = sum(v for h, v in spent_by_hour.items() if h < 10)
    assert 9 <= first_half <= 16                                         # ~nửa quota ở nửa đầu


def test_refuses_to_sign_a_challenge_from_another_host():
    fake = FakeGateway()
    fake.host = "evil.example"
    assert _run({}, fake)["outcome"] == "error" and "/v1/enroll/wallet" not in fake.paths()


def test_no_expiry_keeps_the_bearer_instead_of_re_enrolling_every_run():
    fake, state = FakeGateway(expires_ms=0), {}
    fake.override[("GET", "/v1/keys/me")] = Resp(data={"credential": "owner", "remaining_quota": 25.0,
                                                       "expires_at": None})
    fake.override[("POST", "/v1/enroll/wallet")] = Resp(201, {"key": BEARER, "model": "m1",
                                                              "remaining_quota": 25.0, "expires_at": None})
    _run(state, fake)
    fake.calls.clear()
    _run(state, fake)
    assert "/v1/enroll/wallet" not in fake.paths()


def test_exhausted_allowance_is_not_mistaken_for_a_payment_request():
    fake, state = FakeGateway(), {}
    fake.override[("POST", "/v1/enroll/wallet")] = Resp(402, {"error": "devnet_allowance_exhausted"})
    r = _run(state, fake)
    assert r["outcome"] == "exhausted" and "payment" not in r and state[gw.KEY]["wait_until"] > NOW


def test_empty_prompts_make_no_requests():
    fake = FakeGateway()
    r = gw.run_step({}, agent_key=_key("22"), http=fake, now=NOW, prompts=[], expect_network="devnet",
                    sleep=lambda s: None, log=lambda *a: None)
    assert r["outcome"] == "unconfigured" and fake.calls == []


def test_health_failure_reason_is_reported():
    fake = FakeGateway()
    fake.override[("GET", "/health")] = Resp(403, {"error": "forbidden"})
    r = _run({}, fake)
    assert r["outcome"] == "error_network" and "HTTP 403 forbidden" in r["reason"]

    def boom(*a, **k):
        raise TimeoutError("read timed out")
    r2 = gw.run_step({}, agent_key=_key("22"), http=boom, now=NOW, prompts=["q"], expect_network="devnet",
                     sleep=lambda s: None, log=lambda *a: None)
    assert "TimeoutError" in r2["reason"]


def test_spend_is_taken_from_the_gateway_quota_when_usage_has_no_cost(monkeypatch):
    # Hình dạng THẬT của devnet: usage chỉ có token; quota /keys/me giảm đúng số đã chi.
    fake, booked, state = FakeGateway(expires_ms=SOON, cost=6.513), [], {}
    fake.report_cost = False
    r = _run(state, fake, booked)
    assert r["calls"] == 2 and [round(b[0], 3) for b in booked] == [6.513, 6.513]
    assert r["last"]["src"] == "quota" and r["spent"] == pytest.approx(13.026)
    assert state[gw.KEY]["window"]["spent"] == pytest.approx(13.026)      # nhịp chi tiến lên, không kẹt 0


def test_default_token_cap_is_the_model_limit(monkeypatch):
    monkeypatch.delenv("FLOP_GATEWAY_MAX_TOKENS", raising=False)
    fake = FakeGateway(quota=20000.0, expires_ms=SOON)
    _run({}, fake)
    sent = [c[3]["max_tokens"] for c in fake.calls if c[1] == "/v1/chat/completions"]
    assert sent and max(sent) == 8192                                     # không còn kẹt ở 1024


def test_oversized_request_halves_the_learned_cap_and_errors_parse_openai_shape():
    # Devnet thật 2026-10-24: 8192 token -> 503 {"error": {"code": "provider_error", "message":
    # "Error: no admitted miner for model ... and budget"}}; 1024 token thì được nhận.
    fake, state = FakeGateway(quota=20000.0, expires_ms=SOON), {}
    real = fake.__call__

    def picky(method, url, headers=None, json=None, timeout=None):
        if url.endswith("/chat/completions") and json["max_tokens"] > 1024:
            fake.calls.append((method, "/v1/chat/completions", headers or {}, json, url))
            return Resp(503, {"error": {"code": "provider_error", "type": "invalid_request_error",
                                        "message": "Error: no admitted miner for model 'm1' and budget"}},
                        {"Retry-After": "1"})
        return real(method, url, headers=headers, json=json, timeout=timeout)

    sizes = []
    for i in range(5):
        r = gw.run_step(state, agent_key=_key("22"), http=picky, now=NOW + i * 300, prompts=["q"],
                        expect_network="devnet", sleep=lambda s: None, log=lambda *a: None)
        sizes.append((r["outcome"], state[gw.KEY].get("tok_cap")))
    assert sizes[0] == ("no_miner", 4096) and "provider_error" in gw.GatewayError(503, "provider_error").code
    assert ("spent", 1024) in sizes                                      # hạ dần 8192 -> 4096 -> 2048 -> 1024
