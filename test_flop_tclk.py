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


def test_payee_never_locks():
    # Payee KHÔNG BAO GIỜ escrow tiền -> module không có hàm dựng `lock`. (reveal có, nhưng bị
    # guard chặt trong run_tclk_complete: chỉ reveal sau lock+rail-verify+deadline+làm-được-việc.)
    assert not hasattr(t, "make_lock")
    assert hasattr(t, "make_reveal")


# ═══ Vòng HOÀN TẤT: derivation + guards + worker ═══════════════════════════════
# Vector THẬT (deal đã chạy trên venue): contract -> deal room / paper note / state note.
REAL_CONTRACT = "0x312dd45894e96f35abc4b99b2ab0e4a080fdae7d5728a05f19a11f6904893cfa"
REAL_PAPER_LINE = ("tclkpaper1 locked hash "
                   "0xed99072602abb7a2f5fc991faaa237ce2b8a285ffaeb03e87920bbca147de508 1788401042781")


def test_deal_room_and_notes_derivation_byte_exact():
    assert t.deal_room(REAL_CONTRACT) == "mb-p-tclk-312dd45894e96f35"
    assert t.paper_note(REAL_CONTRACT) == {"ns": "tclk-paper-31", "key": "2dd45894e96f35"}
    assert t.state_note(REAL_CONTRACT) == {"ns": "tclk-31", "key": "2dd45894e96f35"}


def test_decode_paper_record_real_and_malformed():
    rec = t.decode_paper_record(REAL_PAPER_LINE)
    assert rec["status"] == "locked" and rec["lock"] == "hash"
    assert rec["refundAfterMs"] == 1788401042781 and "secret" not in rec
    # claimed phải có secret; locked không được có -> các dòng sai => None
    assert t.decode_paper_record("tclkpaper1 claimed hash 0x" + "a" * 64 + " 123") is None
    assert t.decode_paper_record("tclkpaper1 locked hash 0x" + "a" * 64 + " 123 0x" + "b" * 64) is None
    assert t.decode_paper_record("wrongprefix locked hash 0x" + "a" * 64 + " 123") is None
    assert t.decode_paper_record("garbage") is None


def test_verify_paper_lock_gate():
    stmt = "0x" + "a" * 64
    ok = {"status": "locked", "lock": "hash", "statement": stmt, "refundAfterMs": 999}
    assert t.verify_paper_lock(ok, "hash", stmt, 999) is True
    assert t.verify_paper_lock({**ok, "status": "claimed"}, "hash", stmt, 999) is False
    assert t.verify_paper_lock(ok, "hash", "0x" + "b" * 64, 999) is False   # statement lệch
    assert t.verify_paper_lock(ok, "hash", stmt, 1000) is False             # refundAfterMs lệch
    assert t.verify_paper_lock(None, "hash", stmt, 999) is False


def test_find_payer_lock():
    lock = {"type": "lock", "from": "zPAYER", "contract": REAL_CONTRACT, "rail": "paper", "ref": REAL_CONTRACT}
    msgs = [{"text": "tclk1 " + t.to_ascii(t.canonical_json(lock))}]
    assert t.find_payer_lock(msgs, REAL_CONTRACT, "zPAYER")["rail"] == "paper"
    assert t.find_payer_lock(msgs, REAL_CONTRACT, "zOTHER") is None         # sai payer
    assert t.find_payer_lock(msgs, "0x" + "0" * 64, "zPAYER") is None       # sai contract


# --- worker completion: các nhánh an toàn -----------------------------------------
def _complete_env(lock_present=True, paper_ok=True, past_claim=False):
    stmt = "0x" + "c" * 64
    contract = REAL_CONTRACT
    now = 1000
    meta = {"preimage": "0x" + "e" * 64, "statement": stmt, "payer_did": "zPAYER",
            "amount": "10", "asset": "PAPER", "claimByMs": 500 if past_claim else 5000,
            "refundAfterMs": 9999, "job": {"proto": "a2a", "id": "task-1"}}
    state = {"tclk_secrets": {contract: meta}}
    lock = {"type": "lock", "from": "zPAYER", "contract": contract, "rail": "paper", "ref": contract}
    room_msgs = [{"text": "tclk1 " + t.to_ascii(t.canonical_json(lock))}] if lock_present else []
    paper = ("tclkpaper1 locked hash " + stmt + " 9999") if paper_ok else None
    read_room = lambda room: {"messages": room_msgs}
    kv_get = lambda ns, key: paper
    return contract, state, read_room, kv_get, now


def test_complete_dry_run_reveals_nothing_but_reports():
    contract, state, read_room, kv_get, now = _complete_env()
    posts = []
    res = t.run_tclk_complete(read_room, kv_get, lambda r, x: posts.append(x) or True,
                              do_work_fn=lambda meta: "the deliverable", state=state,
                              my_did="zSELF", now_ms=now, dry_run=True)
    assert res["revealed"] == [contract]
    assert posts == []                                   # DRY: không post
    assert state.get("tclk_completed", []) == []


def test_complete_live_posts_deliver_then_reveal():
    contract, state, read_room, kv_get, now = _complete_env()
    posts = []
    res = t.run_tclk_complete(read_room, kv_get, lambda r, x: posts.append(x) or True,
                              do_work_fn=lambda meta: "the deliverable", state=state,
                              my_did="zSELF", now_ms=now, dry_run=False)
    assert res["revealed"] == [contract]
    assert len(posts) == 2                               # deliverable THEN reveal
    assert "deliver]" in posts[0]
    assert '"type":"reveal"' in posts[1] and '"secret":"0x' in posts[1]
    assert contract in state["tclk_completed"]


def test_complete_uses_offers_room_not_deal_room():
    """Fix: lock/reveal ở lại room offers (deal room mới không tạo được vì cap đầy). Đọc & post
    phải vào offers_room, KHÔNG phải deal_room(contract) — nếu đọc deal room sẽ chờ vô hạn."""
    contract, state, _read, kv_get, now = _complete_env()
    seen_read, seen_post = [], []
    lock = {"type": "lock", "from": "zPAYER", "contract": contract, "rail": "paper", "ref": contract}

    def read_room(room):
        seen_read.append(room)
        return {"messages": [{"text": "tclk1 " + t.to_ascii(t.canonical_json(lock))}]}

    def post(room, x):
        seen_post.append(room)
        return True

    res = t.run_tclk_complete(read_room, kv_get, post, do_work_fn=lambda meta: "d",
                              state=state, my_did="zSELF", now_ms=now, dry_run=False,
                              offers_room="tclk-offers")
    assert res["revealed"] == [contract]
    assert seen_read == ["tclk-offers"]                       # đọc room offers
    assert seen_post == ["tclk-offers", "tclk-offers"]        # deliver + reveal, cùng room offers
    assert t.deal_room(contract) not in seen_read + seen_post  # TUYỆT ĐỐI không dùng deal room


def test_complete_waits_when_kv_not_locked():
    # CỔNG = paper KV record: rail CHƯA ghi locked -> chờ (dù có/không có frame)
    contract, state, read_room, kv_get, now = _complete_env(paper_ok=False)
    posts = []
    res = t.run_tclk_complete(read_room, kv_get, lambda r, x: posts.append(x) or True,
                              do_work_fn=lambda meta: "x", state=state, my_did="zSELF",
                              now_ms=now, dry_run=False)
    assert res["revealed"] == [] and res["waiting"] == 1 and posts == []


def test_complete_reveals_via_kv_without_frame():
    # PR B: lock FRAME đã cuộn khỏi cửa sổ (room KHÔNG có frame) nhưng paper KV record XÁC NHẬN
    # locked -> VẪN reveal. KV là cổng, không phụ thuộc frame -> bắt được lock dù nhịp chạy thưa.
    contract, state, read_room, kv_get, now = _complete_env(lock_present=False)
    posts = []
    res = t.run_tclk_complete(read_room, kv_get, lambda r, x: posts.append(x) or True,
                              do_work_fn=lambda meta: "d", state=state, my_did="zSELF",
                              now_ms=now, dry_run=False)
    assert res["revealed"] == [contract] and res["waiting"] == 0
    assert len(posts) == 2                               # deliver + reveal vẫn xảy ra


def test_complete_refuses_when_rail_not_confirmed():
    # lock frame CÓ nhưng rail chưa xác nhận -> KHÔNG reveal (cổng an toàn)
    contract, state, read_room, kv_get, now = _complete_env(paper_ok=False)
    posts = []
    res = t.run_tclk_complete(read_room, kv_get, lambda r, x: posts.append(x) or True,
                              do_work_fn=lambda meta: "x", state=state, my_did="zSELF",
                              now_ms=now, dry_run=False)
    assert res["revealed"] == [] and res["waiting"] == 1 and posts == []


def test_complete_refuses_past_claim_window():
    contract, state, read_room, kv_get, now = _complete_env(past_claim=True)
    res = t.run_tclk_complete(read_room, kv_get, lambda r, x: True,
                              do_work_fn=lambda meta: "x", state=state, my_did="zSELF",
                              now_ms=now, dry_run=False)
    assert res["revealed"] == [] and res["expired"] == 1


def test_complete_refuses_when_work_fails():
    # do_work_fn trả None (không làm được) -> KHÔNG reveal (giữ thiện chí)
    contract, state, read_room, kv_get, now = _complete_env()
    posts = []
    res = t.run_tclk_complete(read_room, kv_get, lambda r, x: posts.append(x) or True,
                              do_work_fn=lambda meta: None, state=state, my_did="zSELF",
                              now_ms=now, dry_run=False)
    assert res["revealed"] == [] and posts == []


def test_complete_prunes_secrets_on_expire_and_reveal():
    """Chống phình: deal quá cửa sổ claim HOẶC đã reveal bị pop khỏi tclk_secrets. Deal đang
    CHỜ (chưa lock) thì giữ lại. Nhờ vậy kho không tích rác + hết log lặp mỗi run."""
    # hết hạn -> pop
    contract, state, read_room, kv_get, now = _complete_env(past_claim=True)
    t.run_tclk_complete(read_room, kv_get, lambda r, x: True, do_work_fn=lambda m: "x",
                        state=state, my_did="zSELF", now_ms=now, dry_run=False)
    assert contract not in state["tclk_secrets"]          # dead deal đã dọn

    # đã reveal -> pop, nhưng vẫn nhớ ở tclk_completed để dedup
    contract, state, read_room, kv_get, now = _complete_env()
    t.run_tclk_complete(read_room, kv_get, lambda r, x: True, do_work_fn=lambda m: "d",
                        state=state, my_did="zSELF", now_ms=now, dry_run=False)
    assert contract not in state["tclk_secrets"]
    assert contract in state["tclk_completed"]

    # đang CHỜ (KV chưa xác nhận lock) -> KHÔNG pop (còn phải theo dõi)
    contract, state, read_room, kv_get, now = _complete_env(paper_ok=False)
    t.run_tclk_complete(read_room, kv_get, lambda r, x: True, do_work_fn=lambda m: "d",
                        state=state, my_did="zSELF", now_ms=now, dry_run=False)
    assert contract in state["tclk_secrets"]


# ═══ Bộ lọc chỉ-nhận-job-text ══════════════════════════════════════════════════
def test_is_media_job():
    assert t.is_media_job("tiktok short video | duration <=90s; 9:16; h264/aac mp4") is True
    assert t.is_media_job("ig post or short video | image 1080x1350") is True
    assert t.is_media_job("x post or article | <=25000 chars") is False   # text -> không chặn
    assert t.is_media_job("explain database isolation levels") is False
    assert t.is_media_job("") is False and t.is_media_job(None) is False


def test_run_payee_skips_media_job_at_accept():
    now = REAL_OFFER["expiresMs"] - 5 * 60 * 1000
    posts = []
    state = {}
    # job.context của REAL_OFFER trỏ tới spec; giả spec là VIDEO -> phải bỏ, không accept
    res = t.run_tclk_payee(_fetch(REAL_OFFER), lambda x: posts.append(x) or True, state,
                           my_did="zSELF", allow_rails=["paper"],
                           min_claim_window_ms=60000, min_refund_gap_ms=60000,
                           dry_run=False, now_ms=now,
                           job_spec_fn=lambda ctx: "tiktok short video 9:16 mp4")
    assert res["accepted"] == [] and res["skipped"] == 1
    assert posts == []                                   # không post accept cho job media


def test_run_payee_accepts_text_job_with_filter_on():
    now = REAL_OFFER["expiresMs"] - 5 * 60 * 1000
    posts = []
    state = {}
    res = t.run_tclk_payee(_fetch(REAL_OFFER), lambda x: posts.append(x) or True, state,
                           my_did="zSELF", allow_rails=["paper"],
                           min_claim_window_ms=60000, min_refund_gap_ms=60000,
                           dry_run=False, now_ms=now,
                           job_spec_fn=lambda ctx: "x post or article <=25000 chars")
    assert res["accepted"] == [REAL_OFFER["id"]] and len(posts) == 1   # job text -> vẫn nhận
