"""tools/claim_faucet.py: seed nhập ẩn, kiểm đúng ví trước khi ký, thử lại lỗi tạm thời với cùng nonce, dừng khi bị
từ chối. Không gọi mạng (get/post giả)."""
import importlib.util
from pathlib import Path

import flop_agent_claim as fac
import flop_gateway

_spec = importlib.util.spec_from_file_location("claim_faucet", Path(__file__).parent / "tools" / "claim_faucet.py")
cf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cf)

SEED = "11" * 32
LIMITS = {"faucet": {"status": "open", "agent": {"claimPath": "/v1/testnet/agent-claims", "grantFlop": 10}}}


def _addr():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return flop_gateway.wallet_address(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(SEED)))


def _get(url):
    if url.endswith("/chain"):
        return 200, {"network": "testnet", "genesisHash": fac.DEFAULT_GENESIS}
    return 200, LIMITS


def _post_seq(responses, sent):
    it = iter(responses)

    def post(url, body):
        sent.append(body)
        st, data = next(it)
        return st, data, {}
    return post


def test_wrong_seed_never_signs():
    sent = []
    code = cf.main(["--expect", "5CFP215CEbCtDduwG1Nzu5rmYXKfHc47wSATUi1oRvrkQnUP"], prompt=lambda p: SEED,
                   get=_get, post=_post_seq([], sent), sleep=lambda s: None)
    assert code == 1 and sent == []


def test_without_expect_asks_and_stops_unless_confirmed():
    sent = []
    code = cf.main([], prompt=lambda p: SEED, confirm=lambda p: "n", get=_get, post=_post_seq([], sent),
                   sleep=lambda s: None)
    assert code == 1 and sent == []


def test_bad_or_empty_seed_stops(capsys):
    for raw in ("", "zz" * 32, "ab" * 10):
        assert cf.main([], prompt=lambda p, r=raw: r, get=_get, post=_post_seq([], []), sleep=lambda s: None) == 1


def test_busy_faucet_is_retried_with_the_same_nonce_then_claims(capsys):
    sent, naps = [], []
    ok = (200, {"success": True, "amount": 10, "txHash": "0xabc", "status": "included"})
    code = cf.main(["--expect", _addr()], prompt=lambda p: "0x" + SEED.upper() + " ", get=_get,
                   post=_post_seq([(503, {"reason": "faucet_busy"}), (503, {"reason": "faucet_busy"}), ok], sent),
                   sleep=naps.append)
    assert code == 0 and len(sent) == 3 and len({b["nonce"] for b in sent}) == 1
    assert naps == [cf.PAUSE_SEC, cf.PAUSE_SEC] and "0xabc" in capsys.readouterr().out
    assert all(SEED not in b["signature"] for b in sent)


def test_final_refusal_stops_without_looping():
    sent = []
    code = cf.main(["--expect", _addr()], prompt=lambda p: SEED, get=_get,
                   post=_post_seq([(429, {"reason": "agent_spend_missing", "error": "spend first"})], sent),
                   sleep=lambda s: None)
    assert code == 2 and len(sent) == 1
