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


def test_requester_empty_pool_posts_nothing():
    # Pool rỗng (fork chưa đặt câu hỏi riêng) -> KHÔNG đăng gì (chống trùng văn bản = sybil).
    posts = []
    st = {}
    rs = k.run_kibble_requester(post_fn=lambda t: posts.append(t) or True,
                                state=st, questions=[], dry_run=False,
                                log=lambda *_: None, gen=lambda: "kdddddddddd")
    assert posts == [] and rs["posted"] == [] and rs["would_post"] == []
    assert "kibble_requested" not in st


# --- vân tay / chống job gần trùng (thuần) --------------------------------------------

_TPL = ("Review whether posting many ATTEST lines with distinct job_id but identical reason "
        "templates would hit technocore dupe_filter (5 copies per 60s). Tape lines sampled=200.")


def test_job_fingerprint_is_normalized_word_set():
    fp = k.job_fingerprint("Ed25519  Signature Sizes!", "How LARGE is a signature?")
    assert fp == sorted(set(fp))                      # sorted + không trùng
    assert "ed25519" in fp and "large" in fp          # hạ chữ thường, bỏ dấu câu
    # thứ tự từ không đổi vân tay
    assert k.job_fingerprint("a b", "c") == k.job_fingerprint("c b", "a")


def test_similarity_bounds():
    a = k.job_fingerprint("one time pad", "perfect secrecy")
    assert k.similarity(a, a) == 1.0
    assert k.similarity(a, k.job_fingerprint("gas fees", "rollup finality")) == 0.0
    assert k.similarity(a, []) == 0.0 and k.similarity([], []) == 0.0


def test_template_family_is_near_duplicate_but_distinct_jobs_are_not():
    # 4 job cùng template, chỉ khác hậu tố id (đúng thứ quan sát được trên board)
    a = k.job_fingerprint("Review dupe-filter risk for validator attests (mvbmaxua-2)", _TPL)
    b = k.job_fingerprint("Review dupe-filter risk for validator attests (mvbmc37c-2)", _TPL)
    assert k.similarity(a, b) >= 0.85
    c = k.job_fingerprint("Explain the one-time pad",
                           "Why is perfect secrecy impractical to deploy at scale?")
    assert k.similarity(a, c) < 0.85
    recent = k.remember_fingerprint([], a, now_ms=1_000)
    assert k.is_near_duplicate(b, recent, 0.85, now_ms=1_000) is True
    assert k.is_near_duplicate(c, recent, 0.85, now_ms=1_000) is False


def test_is_near_duplicate_disabled_at_zero_threshold():
    a = k.job_fingerprint("same", "same body")
    assert k.is_near_duplicate(a, [a], 0.0) is False     # 0 = tắt hẳn
    assert k.is_near_duplicate([], [a], 0.85) is False   # vân tay rỗng -> không chặn


def test_remember_fingerprint_caps_and_dedupes():
    recent = []
    for i in range(k.DUP_KEEP + 10):
        recent = k.remember_fingerprint(recent, k.job_fingerprint(f"job {i}", "body"))
    assert len(recent) == k.DUP_KEEP
    fp = k.job_fingerprint("job 0", "body")
    once = k.remember_fingerprint(k.remember_fingerprint(recent, fp), fp)
    assert sum(1 for r in once if r["fp"] == fp) == 1           # không nhân bản
    assert once[0]["fp"] == fp                                  # mới nhất lên đầu


def test_remember_fingerprint_is_pure():
    recent = [k.job_fingerprint("a", "b")]
    snapshot = [list(x) for x in recent]
    k.remember_fingerprint(recent, k.job_fingerprint("c", "d"))
    assert recent == snapshot                                   # không sửa đầu vào


# --- HẠN SỐNG của vân tay (sửa lỗi chặn vĩnh viễn làm throughput về 0) ----------------

_H = 3600 * 1000


def test_fingerprint_expires_after_ttl():
    """Chặn trả lời CẢ LOẠT trong một đợt, KHÔNG cấm vĩnh viễn một template."""
    a = k.job_fingerprint("Review dupe-filter risk (mvbmaxua-2)", _TPL)
    b = k.job_fingerprint("Review dupe-filter risk (mvbmc37c-2)", _TPL)
    recent = k.remember_fingerprint([], a, now_ms=0)
    ttl = 6 * _H
    assert k.is_near_duplicate(b, recent, 0.85, now_ms=1 * _H, ttl_ms=ttl) is True   # trong hạn
    assert k.is_near_duplicate(b, recent, 0.85, now_ms=7 * _H, ttl_ms=ttl) is False  # hết hạn


def test_zero_ttl_never_expires():
    a = k.job_fingerprint("x", _TPL)
    recent = k.remember_fingerprint([], a, now_ms=0, ttl_ms=0)
    assert k.is_near_duplicate(a, recent, 0.85, now_ms=999 * _H, ttl_ms=0) is True


def test_legacy_bare_list_entries_are_treated_as_expired():
    """State cũ (list trần, không mốc) chính là thứ đã khoá cứng throughput -> phải
    KHÔNG còn chặn nữa, và bị dọn ở lần ghi kế tiếp."""
    now = 1_790_000_000_000                        # mốc epoch ms thật (prod luôn >> ttl)
    a = k.job_fingerprint("x", _TPL)
    legacy = [a]                                   # định dạng trước khi có timestamp: ts=0
    assert k.is_near_duplicate(a, legacy, 0.85, now_ms=now) is False
    out = k.remember_fingerprint(legacy, k.job_fingerprint("y", "khác hẳn"), now_ms=now)
    assert all(isinstance(r, dict) for r in out) and len(out) == 1


def test_remember_prunes_expired_entries():
    old = k.remember_fingerprint([], k.job_fingerprint("cũ", _TPL), now_ms=0)
    out = k.remember_fingerprint(old, k.job_fingerprint("mới", "nội dung khác"),
                                 now_ms=7 * _H, ttl_ms=6 * _H)
    assert len(out) == 1 and out[0]["fp"] == k.job_fingerprint("mới", "nội dung khác")


def test_entries_keep_their_own_timestamp():
    r = k.remember_fingerprint([], k.job_fingerprint("a", "b"), now_ms=5_000)
    r = k.remember_fingerprint(r, k.job_fingerprint("c", "d"), now_ms=9_000)
    assert [x["ts"] for x in r] == [9_000, 5_000]


# --- lọc gần-trùng LÚC CHỌN (đợt job toàn một họ không được đốt hết quota) -------------

def _job_msg(seq, jobid, title, body, jtype="explain"):
    return {"seq": seq, "from": "did:key:zSomeoneElse",
            "text": f"JOB v1 | {jobid} | {jtype} | {title} | {body}"}


def _burst_then_distinct():
    """Mô phỏng board thật: 1 requester nhả 6 job CÙNG HỌ (mới nhất), rồi 3 job khác hẳn."""
    msgs = []
    for i in range(6):
        msgs.append(_job_msg(1000 + i, "k%010x" % (i + 1),
                             f"Review dupe-filter risk (mvbo1fuy-{i})", _TPL))
    for i, (t, b) in enumerate([
            ("One-time pad perfect secrecy", "Why is it impractical to deploy at scale?"),
            ("Solana SVM account locking", "How does parallel transaction scheduling work?"),
            ("PostgreSQL covering index", "When does an index-only scan actually trigger?")]):
        msgs.append(_job_msg(900 + i, "k%010x" % (100 + i), t, b))
    return msgs


def test_select_jobs_digs_past_a_single_family_burst():
    """Đây CHÍNH LÀ lỗi làm throughput về 0: 5 job mới nhất toàn một họ. Lọc lúc chọn phải
    lấy 1 đại diện của họ rồi ĐÀO TIẾP xuống các job khác, không trả về đợt toàn trùng."""
    picked = k.select_jobs(_burst_then_distinct(), set(), ["explain"], 5,
                           recent_fps=[], dup_sim=0.85, now_ms=1_000)
    assert len(picked) == 4                       # 1 đại diện họ + 3 job khác nhau
    titles = [p["title"] for p in picked]
    assert sum(1 for t in titles if "dupe-filter" in t) == 1
    assert len(set(titles)) == 4


def test_select_jobs_unchanged_when_dup_filter_off():
    picked = k.select_jobs(_burst_then_distinct(), set(), ["explain"], 5, dup_sim=0)
    assert len(picked) == 5                       # hành vi cũ: 5 job mới nhất, kể cả trùng
    assert all("dupe-filter" in p["title"] for p in picked)


def test_select_jobs_skips_family_already_answered_recently():
    recent = k.remember_fingerprint(
        [], k.job_fingerprint("Review dupe-filter risk (mvbmaxua-2)", _TPL), now_ms=1_000)
    picked = k.select_jobs(_burst_then_distinct(), set(), ["explain"], 5,
                           recent_fps=recent, dup_sim=0.85, now_ms=1_000)
    assert all("dupe-filter" not in p["title"] for p in picked)      # họ đã làm -> bỏ qua
    assert len(picked) == 3                                          # còn 3 job khác


def test_select_jobs_takes_family_again_after_ttl():
    recent = k.remember_fingerprint(
        [], k.job_fingerprint("Review dupe-filter risk (mvbmaxua-2)", _TPL), now_ms=0)
    late = 7 * 3600 * 1000
    picked = k.select_jobs(_burst_then_distinct(), set(), ["explain"], 5,
                           recent_fps=recent, dup_sim=0.85, now_ms=late,
                           ttl_ms=6 * 3600 * 1000)
    assert sum(1 for p in picked if "dupe-filter" in p["title"]) == 1   # hết hạn -> làm lại


# --- trim_to_sentence: không chặt ngang chữ (bài cụt "...a heap fetch f") -------------
def _long(n_sentences=40, tail=" Use a composite index to perform a heap fetch f"):
    return ("Composite indexes reduce random IO for range scans. " * n_sentences) + tail


def test_trim_short_text_unchanged():
    assert k.trim_to_sentence("  A short answer.  ", 1200) == "A short answer."


def test_trim_ends_on_complete_sentence_not_mid_word():
    s = _long()
    assert len(s) > 1200
    out = k.trim_to_sentence(s, 1200)
    assert len(out) <= 1200 and out.endswith("scans.")
    assert not s[:1200].endswith(".")                      # bản cắt cứng cũ thì cụt


def test_trim_drops_dangling_list_marker_after_sentence():
    s = ("Latency bounds hold for each region. " * 40) + "4. Next step continues here"
    cap = s.index("4.") + 2                                # cắt ngay sau "4."
    out = k.trim_to_sentence(s, cap)
    assert out.endswith("region.") and not out.endswith("4.")


def test_trim_keeps_a_real_number_ending_a_sentence():
    s = ("x " * 300) + "The answer is 4. " + ("word " * 300)
    cap = s.index("4.") + 2
    assert k.trim_to_sentence(s, cap).endswith("is 4.")


def test_trim_decimal_is_not_a_sentence_end():
    s = ("word " * 300) + "Version 3.5 adds support for" + (" more words" * 50)
    out = k.trim_to_sentence(s, len(s) - 20)
    assert not out.endswith("3.")


def test_trim_without_sentence_end_cuts_on_word_and_marks_it():
    s = "word " * 400
    out = k.trim_to_sentence(s, 1200)
    assert len(out) <= 1201 and out.endswith("…") and out[:-1].endswith("word")



def test_one_failing_job_does_not_abort_the_batch():
    msgs = [{"seq": 10 + i, "from": "did:key:zR", "text": f"JOB v1 | k000000000{i} | explain | t{i} | body {i} different words here"}
            for i in range(3)]
    st, posts = {}, []

    def answer(job):
        if job["jobid"].endswith("2"):
            raise RuntimeError("boom")
        return "a real answer."
    out = k.run_kibble_worker(fetch_fn=lambda since: {"messages": msgs, "last_seq": 12}, answer_fn=answer,
                               post_fn=lambda t: posts.append(t) or True, state=st,
                               allow_types=["explain"], max_per_run=3, do_claim=True, dry_run=False)
    assert len(out["delivered"]) == 2 and out["skipped"] == 1
