"""Làn agent của faucet testnet (flop_agent_claim): kiểm mạng trước khi ký, đúng chuỗi ký, giữ nonce khi chưa có
kết quả cuối, thử bản <Bytes> đúng một lần, không gõ lặp khi bị từ chối."""
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

import flop_agent_claim as fac

KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
ADDR = "5CFP215CEbCtDduwG1Nzu5rmYXKfHc47wSATUi1oRvrkQnUP"
GEN = fac.DEFAULT_GENESIS
NOW = 1_795_600_000.0
CHAIN = {"network": "testnet", "genesisHash": GEN}
LIMITS = {"faucet": {"status": "open", "agent": {"claimPath": "/v1/testnet/agent-claims", "grantFlop": 10}}}


def _get(chain=CHAIN, limits=LIMITS, calls=None):
    def g(url):
        if calls is not None:
            calls.append(url)
        return (200, chain) if url.endswith("/chain") else (200, limits)
    return g


def _post(responses, sent):
    it = iter(responses)

    def p(url, body):
        sent.append((url, body))
        st, data, hdr = next(it)
        return st, data, hdr or {}
    return p


def _verify(sig_hex, msg):
    Ed25519PublicKey.from_public_bytes(KEY.public_key().public_bytes_raw()).verify(bytes.fromhex(sig_hex[2:]), msg)


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("FLOP_FAUCET_AGENT_CLAIM", raising=False)
    assert fac.enabled() is False


def test_claim_signs_exact_official_message_and_schedules_next_day():
    sent = []
    ok = (200, {"success": True, "amount": 10, "tier": "anon_agent", "txHash": "0xabc", "status": "included"}, None)
    rec, out, alert = fac.run_step({}, KEY, ADDR, NOW, _get(), _post([ok], sent))
    assert out == "claimed" and "10 FLOP" in alert
    url, body = sent[0]
    assert url == "https://faucet.flop.network/v1/testnet/agent-claims"
    assert set(body) == {"address", "nonce", "signature"} and body["address"] == ADDR
    assert body["nonce"] == str(int(NOW * 1000))
    _verify(body["signature"], f"flop-faucet-agent/v1|{GEN}|{ADDR}|{body['nonce']}".encode())
    assert rec["nonce"] is None and rec["next_ts"] >= NOW + 86400 and rec["claims"][-1]["tx"] == "0xabc"
    assert rec["at"] == int(NOW)
    # Lượt kế tiếp trong ngày: không gọi mạng.
    calls = []
    rec2, out2, _ = fac.run_step(rec, KEY, ADDR, NOW + 3600, _get(calls=calls), _post([], []))
    assert out2 == "wait" and calls == []


def test_wrong_network_or_genesis_never_signs():
    for chain in ({"network": "devnet", "genesisHash": GEN}, {"network": "testnet", "genesisHash": "0x99c1"}):
        sent = []
        rec, out, _ = fac.run_step({}, KEY, ADDR, NOW, _get(chain=chain), _post([], sent))
        assert out.startswith("chain-mismatch") and sent == [] and rec["next_ts"] == int(NOW) + 3600


def test_closed_or_low_balance_faucet_waits_without_posting():
    sent = []
    lim = {"faucet": {"status": "low_balance", "agent": {"claimPath": "/v1/testnet/agent-claims"}}}
    rec, out, _ = fac.run_step({}, KEY, ADDR, NOW, _get(limits=lim), _post([], sent))
    assert out == "faucet-closed:low_balance" and sent == []


def test_retry_later_keeps_the_same_nonce():
    sent = []
    busy = (503, {"success": False, "reason": "indexer_unavailable", "retryAfterSeconds": 60}, None)
    rec, out, _ = fac.run_step({}, KEY, ADDR, NOW, _get(), _post([busy], sent))
    assert out.startswith("retry:503") and rec["nonce"] == sent[0][1]["nonce"] and rec["next_ts"] == int(NOW) + 60
    ok = (200, {"success": True, "amount": 10, "txHash": "0x1"}, None)
    rec, out, _ = fac.run_step(rec, KEY, ADDR, NOW + 400, _get(), _post([ok], sent))
    assert out == "claimed" and sent[1][1]["nonce"] == sent[0][1]["nonce"]


def test_network_error_keeps_nonce():
    def boom(url, body):
        raise TimeoutError()
    rec, out, _ = fac.run_step({}, KEY, ADDR, NOW, _get(), boom)
    assert out == "error-post:TimeoutError" and rec["nonce"] and rec["next_ts"] == int(NOW) + fac.NETWORK_RETRY


def test_bad_signature_tries_bytes_wrapper_once_then_pauses():
    sent = []
    bad = (401, {"success": False, "reason": "claim_signature_invalid", "error": "Sign the claim"}, None)
    rec, out, alert = fac.run_step({}, KEY, ADDR, NOW, _get(), _post([bad, bad], sent))
    assert out == "sig-unsupported" and len(sent) == 2 and alert
    n0, n1 = sent[0][1]["nonce"], sent[1][1]["nonce"]
    assert int(n1) > int(n0)                                     # nonce mới sau kết quả cuối
    _verify(sent[0][1]["signature"], f"flop-faucet-agent/v1|{GEN}|{ADDR}|{n0}".encode())
    _verify(sent[1][1]["signature"], f"<Bytes>flop-faucet-agent/v1|{GEN}|{ADDR}|{n1}</Bytes>".encode())
    assert rec["next_ts"] == int(NOW) + fac.SIG_FAIL_PAUSE


def test_wrapped_variant_is_remembered_after_it_works():
    sent = []
    bad = (401, {"success": False, "reason": "claim_signature_invalid"}, None)
    ok = (200, {"success": True, "amount": 10, "txHash": "0x2"}, None)
    rec, out, _ = fac.run_step({}, KEY, ADDR, NOW, _get(), _post([bad, ok], sent))
    assert out == "claimed" and rec["variant"] == "wrapped"
    sent2 = []
    rec, out, _ = fac.run_step(rec, KEY, ADDR, NOW + 2 * 86400, _get(), _post([ok], sent2))
    n = sent2[0][1]["nonce"]
    _verify(sent2[0][1]["signature"], f"<Bytes>flop-faucet-agent/v1|{GEN}|{ADDR}|{n}</Bytes>".encode())


def test_refusal_backs_off_and_alerts_only_once_per_reason():
    sent = []
    no = (403, {"success": False, "reason": "rollover_requires_spend", "error": "Spend your last grant first"}, None)
    rec, out, alert = fac.run_step({}, KEY, ADDR, NOW, _get(), _post([no], sent))
    assert out == "refused:403:rollover_requires_spend" and alert and rec["nonce"] is None
    assert rec["next_ts"] == int(NOW) + fac.REFUSE_MIN
    rec, out, alert = fac.run_step(rec, KEY, ADDR, NOW + fac.REFUSE_MIN, _get(), _post([no], sent))
    assert alert is None


def test_429_without_wait_backs_off_an_hour_and_respects_retry_after():
    sent = []
    rec, out, _ = fac.run_step({}, KEY, ADDR, NOW, _get(), _post([(429, {"reason": "ip_limit"}, None)], sent))
    assert rec["next_ts"] == int(NOW) + 3600
    rec, out, _ = fac.run_step({}, KEY, ADDR, NOW, _get(),
                               _post([(429, {"reason": "ip_limit"}, {"Retry-After": "7200"})], sent))
    assert rec["next_ts"] == int(NOW) + 7200


def test_absurd_amount_is_not_recorded_as_a_number():
    sent = []
    ok = (200, {"success": True, "amount": 10 * 10**18, "txHash": "0x3"}, None)
    rec, out, alert = fac.run_step({}, KEY, ADDR, NOW, _get(), _post([ok], sent))
    assert out == "claimed" and rec["claims"][-1]["amount"] is None and "nhận ? FLOP" in alert


def test_step_is_wired_and_state_is_durable(monkeypatch):
    import agent_cron as ac
    assert "testnet_claim" in ac.DURABLE_STATE_KEYS
    monkeypatch.delenv("FLOP_FAUCET_AGENT_CLAIM", raising=False)
    assert ac.testnet_claim_step({}, KEY) == "off"
    monkeypatch.setenv("FLOP_FAUCET_AGENT_CLAIM", "on")
    monkeypatch.setenv("FLOP_FAUCET_ENABLED", "on")
    assert ac.faucet_step({}, KEY, "did:key:x") == "agent-claim-lane"      # không gửi đường /fund cũ
