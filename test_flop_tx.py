"""
Test cho flop_tx.py (khung submit_tx) — python -m pytest test_flop_tx.py -q

Bao phủ: factory chọn adapter theo env, relay POST payload đã-ký + parse tx hash
(qua requests giả), stub EVM raise, và token_manager tự-nối submit_tx ở testnet.
"""

import pytest

import flop_tx
import token_manager as tm


class _Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("TESTNET_ENABLED", "FLOP_RPC_URL", "FLOP_SUBMIT_URL", "FLOP_TX_MODE",
              "FLOP_TX_UA", "TOKEN_LEDGER_FILE"):
        monkeypatch.delenv(k, raising=False)


SIGNED = {"did": "did:key:zTest", "token": "FLOP", "amount": "0.001",
          "nonce": "1", "memo": "x", "sig": "abc"}


# --- factory ---------------------------------------------------------------------

def test_build_submit_tx_modes(monkeypatch):
    assert flop_tx.build_submit_tx({}) is None                       # relay, chưa có endpoint
    assert flop_tx.build_submit_tx({"FLOP_RPC_URL": "https://r"}) is flop_tx.relay_submit_tx
    assert flop_tx.build_submit_tx({"FLOP_TX_MODE": "off"}) is None
    assert flop_tx.build_submit_tx({"FLOP_TX_MODE": "evm"}) is flop_tx.evm_submit_tx


def test_evm_stub_raises():
    with pytest.raises(NotImplementedError):
        flop_tx.evm_submit_tx({"signed": SIGNED, "rpc_url": "https://r"})


# --- relay -----------------------------------------------------------------------

def test_relay_posts_signed_and_parses_hash(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return _Resp({"tx_hash": "0xabc123"})

    monkeypatch.setattr(flop_tx.requests, "post", fake_post)
    monkeypatch.setenv("FLOP_SUBMIT_URL", "https://relay.test/tx")
    out = flop_tx.relay_submit_tx({"token": "FLOP", "amount": "0.001", "memo": "x",
                                   "rpc_url": "https://r", "signed": SIGNED})
    assert out == {"tx_hash": "0xabc123"}
    assert captured["url"] == "https://relay.test/tx"
    assert captured["body"]["sig"] == "abc"        # POST payload = signed


def test_relay_extracts_jsonrpc_result(monkeypatch):
    monkeypatch.setattr(flop_tx.requests, "post",
                        lambda *a, **k: _Resp({"result": "0xdeadbeef"}))
    monkeypatch.setenv("FLOP_SUBMIT_URL", "https://relay.test/tx")
    assert flop_tx.relay_submit_tx({"amount": "1", "token": "FLOP", "memo": "x",
                                    "rpc_url": "https://r", "signed": SIGNED})["tx_hash"] == "0xdeadbeef"


def test_relay_needs_signed_and_url(monkeypatch):
    with pytest.raises(RuntimeError, match="đã ký"):
        flop_tx.relay_submit_tx({"amount": "1", "rpc_url": "https://r"})
    with pytest.raises(RuntimeError, match="FLOP_SUBMIT_URL"):
        flop_tx.relay_submit_tx({"amount": "1", "signed": SIGNED})   # no url anywhere


def test_relay_raises_when_no_hash(monkeypatch):
    monkeypatch.setattr(flop_tx.requests, "post", lambda *a, **k: _Resp({"ok": True}))
    monkeypatch.setenv("FLOP_SUBMIT_URL", "https://relay.test/tx")
    with pytest.raises(RuntimeError, match="tx hash"):
        flop_tx.relay_submit_tx({"amount": "1", "token": "FLOP", "memo": "x",
                                 "rpc_url": "https://r", "signed": SIGNED})


# --- token_manager tự-nối submit_tx ở testnet ------------------------------------

def test_spend_autowires_relay(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import agent_cron
    pk = Ed25519PrivateKey.generate()
    did = agent_cron.did_of(pk)                          # khóa thật -> payload được ký

    ledger = str(tmp_path / "l.json")
    tm.credit("100", path=ledger)
    posted = {}
    monkeypatch.setattr(flop_tx.requests, "post",
                        lambda url, json=None, **k: (posted.update(json), _Resp({"tx_hash": "0xfeed"}))[1])
    monkeypatch.setenv("TESTNET_ENABLED", "true")
    monkeypatch.setenv("FLOP_SUBMIT_URL", "https://relay.test/tx")
    r = tm.spend("0.001", "Gemini Inference", path=ledger, private_key=pk, did=did)
    assert r["outcome"] == "spent_onchain"
    assert r["tx_hash"] == "0xfeed"
    assert r["balance_after"] == "99.999"
    assert posted["did"] == did and posted["sig"]        # đã POST payload có chữ ký


def test_spend_testnet_unconfigured_without_endpoint(tmp_path, monkeypatch):
    ledger = str(tmp_path / "l.json")
    tm.credit("100", path=ledger)
    monkeypatch.setenv("TESTNET_ENABLED", "true")        # cờ bật nhưng KHÔNG endpoint
    r = tm.spend("0.001", "x", path=ledger)
    assert r["outcome"] == "skipped_unconfigured"
    assert tm.check_balance(path=ledger) == "100"
