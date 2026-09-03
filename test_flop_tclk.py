"""Tests cho flop_tclk.py — tclk/1 payee thuần + worker dry-run (fake deps, không mạng)."""

import hashlib
import flop_tclk as t


# --- Vector BYTE-EXACT với reference JS ------------------------------------------
# Offer THẬT lấy từ /r/tclk-offers (agent JS tạo id). Có nested `job` + context có "/" ->
# ép canonical_json đúng đệ quy + sort key. offer_id tính lại PHẢI khớp id trong frame,
# nếu không thì canonical/hash đã lệch reference và contract id sẽ sai -> counterparty reject.
REAL_OFFER = {
    "amount": "1000000", "asset": "PAPER",
    "claimByMs": 1788397677208, "expiresMs": 1788396477208,
    "from": "did:key:z6Mkn8jQeSc2SqmYrtcSjfZiP7nSkNb8QxYHQuSTzy6hfSkd",
    "id": "0x15c7ba46cdb2eb4599ad03255b2394628fb2c6b1a74e172126a35ac34f3c8b7b",
    "job": {"context": "/kv/tclk-job-46/deal-5873b346", "id": "deal-5873b346", "proto": "a2a"},
    "lock": "hash", "nonce": "4c8400cf54412bbc", "rails": ["paper"],
    "refundAfterMs": 1788399477208, "role": "payer", "type": "offer",
}


def test_offer_id_byte_exact_with_reference():
    assert t.offer_id(REAL_OFFER) == REAL_OFFER["id"]


def test_canonical_json_sorts_and_compacts():
    assert t.canonical_json({"b": 1, "a": "x"}) == '{"a":"x","b":1}'
    assert t.canonical_json({"z": None, "a": 1}) == '{"a":1}'           # None (=undefined) bị bỏ
    assert t.canonical_json([3, {"k": 2}]) == '[3,{"k":2}]'


def test_to_ascii_escapes_non_ascii():
    assert t.to_ascii("abc") == "abc"
    assert t.to_ascii("é") == "\\u00e9"
    assert t.to_ascii("你") == "\\u4f60"


# --- Hash lock ------------------------------------------------------------------
def test_hash_lock_roundtrip():
    preimage, statement = t.generate_hash_lock()
    assert statement.startswith("0x") and len(statement) == 66
    raw = bytes.fromhex(preimage[2:])
    assert statement == "0x" + hashlib.sha256(raw).hexdigest()
    assert t.verify_hash_preimage(statement, preimage) is True
    assert t.verify_hash_preimage(statement, "0x" + "00" * 32) is False


# --- make_accept ----------------------------------------------------------------
def test_make_accept_builds_valid_contract():
    frame, preimage, statement = t.make_accept(REAL_OFFER, "did:key:z6MkPayeeTestDidAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert frame["type"] == "accept"
    assert frame["ref"] == REAL_OFFER["id"]
    assert frame["statement"] == statement == "0x" + hashlib.sha256(bytes.fromhex(preimage[2:])).hexdigest()
    # contract id tính lại từ offer + accept-core khớp -> byte-exact
    core = {"from": frame["from"], "ref": frame["ref"], "statement": frame["statement"], "nonce": frame["nonce"]}
    assert frame["contract"] == t.contract_id(REAL_OFFER, core)
    # frame mã hoá được, có prefix, trong giới hạn
    line = t.encode_frame(frame)
    assert line.startswith("tclk1 ") and len(line) <= t.MAX_FRAME_CHARS


def test_make_accept_rejects_point_lock():
    import pytest
    o = {**REAL_OFFER, "lock": "point"}
    with pytest.raises(ValueError):
        t.make_accept(o, "did:key:z6Mkxxxx")


# --- select_offers --------------------------------------------------------------
def _msg(offer, seq=1):
    return {"seq": seq, "text": "tclk1 " + t.to_ascii(t.canonical_json(offer))}


def test_select_offers_picks_valid_payer_hash():
    now = REAL_OFFER["expiresMs"] - 5 * 60 * 1000        # trước hạn offer
    picks = t.select_offers([_msg(REAL_OFFER)], set(), "did:key:zSELF", now,
                            allow_rails=["paper"], min_claim_window_ms=60000,
                            min_refund_gap_ms=60000, max_n=2)
    assert len(picks) == 1 and picks[0][1]["id"] == REAL_OFFER["id"]


def test_select_offers_filters():
    now = REAL_OFFER["expiresMs"] - 5 * 60 * 1000
    A = dict(allow_rails=["paper"], min_claim_window_ms=60000, min_refund_gap_ms=60000, max_n=5)
    # bỏ offer của chính mình
    assert t.select_offers([_msg(REAL_OFFER)], set(), REAL_OFFER["from"], now, **A) == []
    # bỏ khi đã nhận
    assert t.select_offers([_msg(REAL_OFFER)], {REAL_OFFER["id"]}, "zSELF", now, **A) == []
    # bỏ role=payee (mình không đi trả tiền)
    assert t.select_offers([_msg({**REAL_OFFER, "role": "payee"})], set(), "zSELF", now, **A) == []
    # bỏ rail không khớp
    assert t.select_offers([_msg(REAL_OFFER)], set(), "zSELF", now,
                           allow_rails=["flop-htlc"], min_claim_window_ms=60000,
                           min_refund_gap_ms=60000, max_n=5) == []
    # bỏ khi hết hạn
    assert t.select_offers([_msg(REAL_OFFER)], set(), "zSELF", REAL_OFFER["expiresMs"] + 1, **A) == []
    # bỏ offer gắn id BỊA (id không khớp fields)
    assert t.select_offers([_msg({**REAL_OFFER, "id": "0x" + "de" * 32})], set(), "zSELF", now, **A) == []


# --- worker: DRY-RUN không post, LIVE post accept, KHÔNG BAO GIỜ reveal ----------
def _fetch(offer):
    return lambda since: {"messages": [_msg(offer, seq=5)], "last_seq": 5}


def test_run_payee_dry_run_does_not_post():
    now = REAL_OFFER["expiresMs"] - 5 * 60 * 1000
    posts = []
    state = {}
    res = t.run_tclk_payee(_fetch(REAL_OFFER), lambda x: posts.append(x) or True, state,
                           my_did="zSELF", allow_rails=["paper"],
                           min_claim_window_ms=60000, min_refund_gap_ms=60000,
                           dry_run=True, now_ms=now)
    assert res["dry_run"] is True
    assert res["accepted"] == [REAL_OFFER["id"]]
    assert posts == []                                   # DRY: không post gì
    assert state.get("tclk_accepted", []) == []          # DRY: không ghi done (live còn làm)
    assert "tclk_secrets" not in state                   # DRY: không lưu secret


def test_run_payee_live_posts_accept_only_never_reveal():
    now = REAL_OFFER["expiresMs"] - 5 * 60 * 1000
    posts = []
    state = {}
    res = t.run_tclk_payee(_fetch(REAL_OFFER), lambda x: posts.append(x) or True, state,
                           my_did="zSELF", allow_rails=["paper"],
                           min_claim_window_ms=60000, min_refund_gap_ms=60000,
                           dry_run=False, now_ms=now)
    assert res["accepted"] == [REAL_OFFER["id"]]
    assert len(posts) == 1
    # AN TOÀN: chỉ post ACCEPT, KHÔNG bao giờ reveal/lock/refund
    assert '"type":"accept"' in posts[0]
    assert '"type":"reveal"' not in posts[0] and '"type":"lock"' not in posts[0]
    # secret lưu nội bộ để reveal (do người) — có preimage
    secrets = state.get("tclk_secrets", {})
    assert len(secrets) == 1
    (meta,) = secrets.values()
    assert meta["offer_id"] == REAL_OFFER["id"] and meta["preimage"].startswith("0x")


def test_module_has_no_reveal_builder():
    # Bảo chứng thiết kế: module KHÔNG cung cấp hàm dựng reveal/lock -> worker không thể tự claim.
    assert not hasattr(t, "make_reveal")
    assert not hasattr(t, "make_lock")
