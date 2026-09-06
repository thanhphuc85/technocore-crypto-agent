"""Tests cho flop_rail_x402.py — rail HTLC x402 thuần + tích hợp qua run_tclk_complete (fake seams)."""

import hashlib
import flop_rail_x402 as x
import flop_tclk as t


def _mk_lock(pre_hex):
    """(preimage, statement=sha256(preimage)) — cùng dạng flop_tclk.generate_hash_lock."""
    b = bytes.fromhex(pre_hex[2:])
    return pre_hex, "0x" + hashlib.sha256(b).hexdigest()


PRE = "0x" + "ab" * 32
_, STMT = _mk_lock(PRE)
ADDR_ME = "0x" + "11" * 20
ADDR_OTHER = "0x" + "22" * 20


# --- Chuẩn hoá & guard thuần ----------------------------------------------------
def test_normalize_statement():
    assert x.normalize_statement(STMT.upper()) == STMT           # ép lowercase
    assert x.normalize_statement("0x" + "ab" * 32) == "0x" + "ab" * 32
    assert x.normalize_statement("nope") is None
    assert x.normalize_statement("0x" + "ab" * 31) is None       # sai độ dài
    assert x.normalize_statement(None) is None


def test_preimage_matches():
    assert x.preimage_matches(STMT, PRE) is True
    assert x.preimage_matches(STMT, "0x" + "cd" * 32) is False   # preimage sai
    assert x.preimage_matches(STMT, "0xzz") is False             # không phải hex
    assert x.preimage_matches("bad-stmt", PRE) is False


def test_claimable_window():
    refund = 1788399477208
    assert x.claimable(refund - 10 * 60 * 1000, refund, 3 * 60 * 1000) is True   # còn 10' > margin 3'
    assert x.claimable(refund - 60 * 1000, refund, 3 * 60 * 1000) is False       # chỉ còn 1' < margin
    assert x.claimable("bad", refund, 1000) is False


# --- Cổng verify_lock_state (thuần) --------------------------------------------
def _state(**over):
    s = {"funded": True, "withdrawn": False, "refunded": False,
         "hashlock": STMT, "timelock_ms": 1788399477208, "payee": ADDR_ME}
    s.update(over)
    return s


def test_verify_lock_state_happy():
    assert x.verify_lock_state(_state(), STMT, 1788399477208, ADDR_ME) is True


def test_verify_lock_state_rejects():
    assert x.verify_lock_state(_state(funded=False), STMT, 1788399477208, ADDR_ME) is False
    assert x.verify_lock_state(_state(withdrawn=True), STMT, 1788399477208, ADDR_ME) is False
    assert x.verify_lock_state(_state(refunded=True), STMT, 1788399477208, ADDR_ME) is False
    assert x.verify_lock_state(_state(hashlock="0x" + "cd" * 32), STMT, 1788399477208, ADDR_ME) is False
    assert x.verify_lock_state(_state(timelock_ms=1), STMT, 1788399477208, ADDR_ME) is False
    # payee cố định KHÁC mình -> reject (chống front-run rút preimage công khai)
    assert x.verify_lock_state(_state(payee=ADDR_OTHER), STMT, 1788399477208, ADDR_ME) is False
    assert x.verify_lock_state(None, STMT, 1788399477208, ADDR_ME) is False


# --- Rail object: gating khi CHƯA cấu hình -------------------------------------
def test_rail_unconfigured_is_safe():
    r = x.X402HtlcRail(config={"rpc_url": "", "htlc_addr": "", "payee_addr": "",
                              "claim_margin_ms": 1000, "max_amount": 0},
                       submit_fn=None, read_fn=None)
    assert r.configured() is False
    assert r.verify_lock("0x" + "00" * 32, STMT, 1) is False          # coi như CHƯA lock
    assert r.claim("0x" + "00" * 32, PRE, now_ms=0)["outcome"] == "skipped_unconfigured"


def _cfg():
    return {"rpc_url": "http://rpc", "htlc_addr": ADDR_ME, "payee_addr": ADDR_ME,
            "claim_margin_ms": 3 * 60 * 1000, "max_amount": 0}


def test_rail_verify_lock_reads_chain():
    calls = {}
    def read_fn(addr, fn, cid):
        calls["args"] = (addr, fn, cid)
        return _state()
    r = x.X402HtlcRail(config=_cfg(), submit_fn=lambda tx: "0xtx", read_fn=read_fn)
    assert r.configured() is True
    assert r.verify_lock("0x" + "0a" * 32, STMT, 1788399477208) is True
    assert calls["args"] == (ADDR_ME, "getContract", "0x" + "0a" * 32)


def test_rail_claim_dry_run_and_live():
    r = x.X402HtlcRail(config=_cfg(), submit_fn=lambda tx: "0xdeadbeef",
                       read_fn=lambda a, f, c: _state())
    cid = "0x" + "0a" * 32
    refund = 1788399477208
    # dry-run -> would_claim (không gọi submit)
    assert r.claim(cid, PRE, now_ms=refund - 10 * 60 * 1000, refund_after_ms=refund,
                   dry_run=True)["outcome"] == "would_claim"
    # live trong hạn -> claimed + tx
    res = r.claim(cid, PRE, now_ms=refund - 10 * 60 * 1000, refund_after_ms=refund, dry_run=False)
    assert res["outcome"] == "claimed" and res["tx"] == "0xdeadbeef"
    # quá cửa sổ -> skipped_window (KHÔNG lộ preimage vô ích)
    assert r.claim(cid, PRE, now_ms=refund - 60 * 1000, refund_after_ms=refund,
                   dry_run=False)["outcome"] == "skipped_window"


def test_rail_claim_submit_error():
    def boom(tx):
        raise RuntimeError("rpc down")
    r = x.X402HtlcRail(config=_cfg(), submit_fn=boom, read_fn=lambda a, f, c: _state())
    res = r.claim("0x" + "0a" * 32, PRE, now_ms=0, dry_run=False)
    assert res["outcome"] == "error_submit" and "rpc down" in res["reason"]


# --- Tích hợp: run_tclk_complete với value_rail giả -----------------------------
class FakeRail:
    """Rail giá-trị giả cho test tích hợp: verify_lock luôn OK, ghi lại claim."""
    name = "x402"
    def __init__(self, ok=True):
        self.ok = ok
        self.claims = []
    def verify_lock(self, contract, statement, refund_after_ms):
        return self.ok
    def claim(self, contract, preimage_hex, *, now_ms, refund_after_ms=None, dry_run=True):
        self.claims.append((contract, preimage_hex, dry_run))
        return {"outcome": "claimed", "rail": "x402", "tx": "0xfeed"}


def _meta(contract):
    return {"preimage": PRE, "statement": STMT, "refundAfterMs": 9_999_999_999_999,
            "claimByMs": 9_999_999_999_999, "amount": "1000000", "asset": "USDC",
            "rails": ["x402"], "payer_did": "did:key:zPayer", "accepted_ms": 0}


def test_complete_value_rail_claims_before_reveal():
    contract = "0x" + "0a" * 32
    posts = []
    rail = FakeRail(ok=True)
    state = {"tclk_secrets": {contract: _meta(contract)}, "tclk_completed": []}
    res = t.run_tclk_complete(
        read_room_fn=lambda room: {"messages": []},
        kv_get_fn=lambda ns, key: None,
        post_fn=lambda room, text: posts.append(text) or True,
        do_work_fn=lambda meta: "đáp án thật",
        state=state, my_did="did:key:zMe", now_ms=1, offers_room="tclk-offers",
        value_rail=rail, dry_run=False,
    )
    assert res["revealed"] == [contract]
    # đã CLAIM on-chain (live) trước khi post reveal
    assert rail.claims and rail.claims[0][2] is False
    # có 2 post: deliverable + frame reveal; reveal đứng SAU
    assert any("deliver" in p for p in posts)
    assert any(p.startswith("tclk1 ") and "reveal" in p for p in posts)


def test_complete_value_rail_dry_run_no_claim():
    contract = "0x" + "0b" * 32
    posts = []
    rail = FakeRail(ok=True)
    state = {"tclk_secrets": {contract: _meta(contract)}, "tclk_completed": []}
    res = t.run_tclk_complete(
        read_room_fn=lambda room: {"messages": []},
        kv_get_fn=lambda ns, key: None,
        post_fn=lambda room, text: posts.append(text) or True,
        do_work_fn=lambda meta: "đáp án thật",
        state=state, my_did="did:key:zMe", now_ms=1, offers_room="tclk-offers",
        value_rail=rail, dry_run=True,
    )
    assert res["revealed"] == [contract]
    assert rail.claims == []          # dry-run KHÔNG claim thật
    assert posts == []                # dry-run KHÔNG post gì


def test_complete_value_rail_not_locked_waits():
    contract = "0x" + "0c" * 32
    rail = FakeRail(ok=False)         # escrow chưa verify -> chờ, KHÔNG reveal
    state = {"tclk_secrets": {contract: _meta(contract)}, "tclk_completed": []}
    res = t.run_tclk_complete(
        read_room_fn=lambda room: {"messages": []},
        kv_get_fn=lambda ns, key: None,
        post_fn=lambda room, text: True,
        do_work_fn=lambda meta: "đáp án thật",
        state=state, my_did="did:key:zMe", now_ms=1, offers_room="tclk-offers",
        value_rail=rail, dry_run=False,
    )
    assert res["revealed"] == [] and res["waiting"] == 1
    assert rail.claims == []
