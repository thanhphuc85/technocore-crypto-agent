"""Tests cho flop_kibble.py — protocol thuần + orchestrator (fake deps, không mạng/LLM)."""

import flop_kibble as k


# --- parse_kibble_msg -------------------------------------------------------------

def test_parse_job_basic():
    j = k.parse_kibble_msg("JOB v1 | kedb15291bc | explain | Tides | Explain ocean tides")
    assert j == {"verb": "JOB", "jobid": "kedb15291bc", "type": "explain",
                 "title": "Tides", "body": "Explain ocean tides"}


def test_parse_job_body_keeps_pipes():
    # Mô tả job có thể chứa " | " -> body giữ nguyên phần còn lại.
    j = k.parse_kibble_msg("JOB v1 | k05307df383 | explain | T | a | b | c")
    assert j["body"] == "a | b | c"
    assert j["type"] == "explain"


def test_parse_claim_deliver_attest_witness():
    assert k.parse_kibble_msg("CLAIM v1 | k7a3ab1f067 | worker") == {
        "verb": "CLAIM", "jobid": "k7a3ab1f067", "role": "worker"}
    d = k.parse_kibble_msg("DELIVER v1 | k9cda50964f | the answer text")
    assert d == {"verb": "DELIVER", "jobid": "k9cda50964f", "text": "the answer text"}
    a = k.parse_kibble_msg("ATTEST v1 | k81af0720d4 | useful | rh:e844 | ok")
    assert a["verb"] == "ATTEST" and a["jobid"] == "k81af0720d4"
    w = k.parse_kibble_msg("WITNESS v1 | k14ecaf9096 | 7b22d55ca363d953")
    assert w == {"verb": "WITNESS", "jobid": "k14ecaf9096", "hash": "7b22d55ca363d953"}


def test_parse_rejects_junk():
    assert k.parse_kibble_msg("Agent node reporting in. Ed25519 verified.") is None
    assert k.parse_kibble_msg("JOB v2 | kedb15291bc | explain | t | b") is None   # sai version
    assert k.parse_kibble_msg("JOB v1 | BADID | explain | t | b") is None         # jobid sai
    assert k.parse_kibble_msg("JOB v1 | kedb15291bc | explain") is None           # thiếu field
    assert k.parse_kibble_msg(None) is None
    assert k.parse_kibble_msg("") is None


# --- format helpers ---------------------------------------------------------------

def test_format_claim_and_deliver_single_line():
    assert k.format_claim("kedb15291bc") == "CLAIM v1 | kedb15291bc | worker"
    out = k.format_deliver("kedb15291bc", "line one\nline two\t  spaced")
    assert out == "DELIVER v1 | kedb15291bc | line one line two spaced"   # newline -> 1 dòng
    assert "\n" not in out


# --- select_jobs ------------------------------------------------------------------

def _msg(seq, text, frm="did:key:zStranger"):
    return {"seq": seq, "from": frm, "text": text}


def test_select_filters_type_done_and_dedup():
    msgs = [
        _msg(10, "JOB v1 | k0000000001 | explain | A | body A"),
        _msg(11, "JOB v1 | k0000000002 | translate | B | body B"),   # type ngoài allow
        _msg(12, "JOB v1 | k0000000003 | explain | C | body C"),     # đã done -> loại
        _msg(13, "JOB v1 | k0000000001 | explain | A2 | body A2"),   # trùng id -> giữ seq mới
        _msg(14, "CLAIM v1 | k0000000009 | worker"),                 # không phải JOB
    ]
    jobs = k.select_jobs(msgs, done_set={"k0000000003"},
                         allow_types=["explain"], max_n=5)
    ids = [j["jobid"] for j in jobs]
    assert ids == ["k0000000001"]                     # 2=type, 3=done, dedup -> chỉ 1
    assert jobs[0]["title"] == "A2" and jobs[0]["seq"] == 13   # bản seq mới nhất


def test_select_empty_allow_types_returns_nothing():
    # Whitelist RỖNG (vd env FLOP_KIBBLE_TYPES="") KHÔNG được biến thành "nhận mọi type".
    msgs = [
        _msg(30, "JOB v1 | k000000000d | research | R | x"),
        _msg(31, "JOB v1 | k000000000e | explain | E | x"),
    ]
    assert k.select_jobs(msgs, done_set=set(), allow_types=[], max_n=5) == []
    assert k.select_jobs(msgs, done_set=set(), allow_types=None, max_n=5) == []


def test_select_skips_own_did_and_orders_and_caps():
    msgs = [
        _msg(20, "JOB v1 | k000000000a | explain | own | x", frm="did:key:zME"),  # của mình
        _msg(21, "JOB v1 | k000000000b | explain | p | x"),
        _msg(22, "JOB v1 | k000000000c | explain | q | x"),
    ]
    jobs = k.select_jobs(msgs, done_set=set(), allow_types=["explain"],
                         max_n=1, my_did="did:key:zME")
    assert [j["jobid"] for j in jobs] == ["k000000000c"]   # bỏ của mình, mới nhất trước, cap 1


# --- run_kibble_worker (orchestrator, fake deps) ----------------------------------

def _fetch(messages, last_seq):
    return lambda since: {"messages": messages, "last_seq": last_seq}


def test_run_dry_run_posts_nothing_but_advances_cursor():
    msgs = [_msg(30, "JOB v1 | k000000000d | explain | T | body")]
    posts = []
    state = {}
    out = k.run_kibble_worker(
        fetch_fn=_fetch(msgs, 30),
        answer_fn=lambda job: "a real answer",
        post_fn=lambda text: posts.append(text) or True,
        state=state, allow_types=["explain"], dry_run=True, log=lambda *_: None,
    )
    assert posts == []                                  # DRY: không đăng gì
    assert out["delivered"] == ["k000000000d"]
    assert state["kibble_cursor"] == 30                 # cursor vẫn tiến
    assert state.get("kibble_done", []) == []           # DRY: không ghi done


def test_run_live_claims_delivers_and_records_done():
    msgs = [_msg(40, "JOB v1 | k000000000e | explain | T | body")]
    posts = []
    state = {}
    out = k.run_kibble_worker(
        fetch_fn=_fetch(msgs, 40),
        answer_fn=lambda job: "the answer",
        post_fn=lambda text: posts.append(text) or True,
        state=state, allow_types=["explain"], do_claim=True, dry_run=False,
        log=lambda *_: None,
    )
    assert posts == ["CLAIM v1 | k000000000e | worker",
                     "DELIVER v1 | k000000000e | the answer"]
    assert out["delivered"] == ["k000000000e"]
    assert state["kibble_done"] == ["k000000000e"]      # LIVE: vào sổ done
    assert state["kibble_cursor"] == 40


def test_run_skips_when_answer_none_never_posts_filler():
    msgs = [_msg(50, "JOB v1 | k000000000f | explain | T | body")]
    posts = []
    state = {}
    out = k.run_kibble_worker(
        fetch_fn=_fetch(msgs, 50),
        answer_fn=lambda job: None,                     # model từ chối -> KHÔNG đăng rác
        post_fn=lambda text: posts.append(text) or True,
        state=state, allow_types=["explain"], dry_run=False, log=lambda *_: None,
    )
    assert posts == []
    assert out["skipped"] == 1 and out["delivered"] == []
    assert state.get("kibble_done", []) == []


def test_run_does_not_redo_done_job():
    msgs = [_msg(60, "JOB v1 | k000000001a | explain | T | body")]
    posts = []
    state = {"kibble_done": ["k000000001a"]}
    k.run_kibble_worker(
        fetch_fn=_fetch(msgs, 60),
        answer_fn=lambda job: "answer",
        post_fn=lambda text: posts.append(text) or True,
        state=state, allow_types=["explain"], dry_run=False, log=lambda *_: None,
    )
    assert posts == []                                  # đã done -> không làm lại


# --- REQUESTER (vai đăng JOB) -----------------------------------------------------

def test_format_job_roundtrips():
    txt = k.format_job("kaaaaaaaaaa", "Explain", "  Tides  ", "Explain\nocean   tides")
    assert txt == "JOB v1 | kaaaaaaaaaa | explain | Tides | Explain ocean tides"
    # đăng ra phải parse LẠI được đúng như một JOB hợp lệ
    p = k.parse_kibble_msg(txt)
    assert p == {"verb": "JOB", "jobid": "kaaaaaaaaaa", "type": "explain",
                 "title": "Tides", "body": "Explain ocean tides"}


def test_gen_jobid_is_valid():
    jid = k.gen_jobid(rand=lambda n: bytes(range(n)))   # tất định
    assert k._looks_like_jobid(jid)                     # 'k'+10hex
    assert jid == "k0001020304"


def test_pick_request_rotates():
    qs = [{"title": "A", "body": "a"}, {"title": "B", "body": "b"}]
    st = {}
    q, nidx = k.pick_request(qs, st); assert q["title"] == "A" and nidx == 1
    st["kibble_req_idx"] = nidx
    q, nidx = k.pick_request(qs, st); assert q["title"] == "B" and nidx == 0
    # str và tuple cũng chuẩn hoá được
    q, _ = k.pick_request(["just a string"], {}); assert q == {"title": "just a string", "body": "just a string"}
    q, _ = k.pick_request([("T", "B", "summarize")], {}); assert q["type"] == "summarize"
    assert k.pick_request([], {}) == (None, 0)          # pool rỗng -> None


def test_requester_dry_run_posts_nothing_but_rotates():
    posts = []
    st = {}
    qs = [{"title": "Q1", "body": "b1"}, {"title": "Q2", "body": "b2"}]
    rs = k.run_kibble_requester(post_fn=lambda t: posts.append(t) or True,
                                state=st, questions=qs, max_per_run=1,
                                dry_run=True, log=lambda *_: None,
                                gen=lambda: "kaaaaaaaaaa")
    assert posts == []                                  # DRY: không đăng thật
    assert rs["would_post"] == ["kaaaaaaaaaa"] and rs["posted"] == []
    assert st["kibble_req_idx"] == 1                    # con trỏ vẫn xoay
    assert "kibble_requested" not in st                 # dry KHÔNG ghi requested


def test_requester_live_posts_and_records():
    posts = []
    st = {}
    qs = [{"title": "Q1", "body": "b1"}]
    rs = k.run_kibble_requester(post_fn=lambda t: posts.append(t) or True,
                                state=st, questions=qs, jtype_default="explain",
                                max_per_run=1, dry_run=False, log=lambda *_: None,
                                gen=lambda: "kbbbbbbbbbb")
    assert len(posts) == 1
    p = k.parse_kibble_msg(posts[0])
    assert p and p["verb"] == "JOB" and p["jobid"] == "kbbbbbbbbbb" and p["title"] == "Q1"
    assert rs["posted"] == ["kbbbbbbbbbb"]
    assert st["kibble_requested"] == ["kbbbbbbbbbb"]    # live ghi vào sổ requested


def test_requester_live_post_fail_not_recorded():
    st = {}
    rs = k.run_kibble_requester(post_fn=lambda t: False,     # server từ chối
                                state=st, questions=[{"title": "Q", "body": "b"}],
                                dry_run=False, log=lambda *_: None, gen=lambda: "kcccccccccc")
    assert rs["posted"] == []
    assert st.get("kibble_requested", []) == []              # post fail -> KHÔNG ghi
