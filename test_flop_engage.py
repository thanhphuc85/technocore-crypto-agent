"""
Test cho flop_engage.py — python -m pytest test_flop_engage.py -q

Bao phủ phần THUẦN (phân loại filler/có-chất, cooldown bề-rộng, chọn mục tiêu loại
fleet/không-ký/trùng) và orchestration run_engage_pass qua fn TIÊM vào — KHÔNG chạm mạng,
KHÔNG gọi LLM thật.
"""
import flop_engage as fe


def _msg(seq, frm, text):
    return {"seq": seq, "from": frm, "text": text}


D1 = "did:key:z6MkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1111"
D2 = "did:key:z6MkBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2222"
ME = "did:key:z6MkMEMEMEMEMEMEMEMEMEMEMEMEMEMEMEMEMEMEME0000"
GOODQ = ("What concrete staleness threshold do you use before rejecting a price feed, "
         "and why that number?")


# --- is_boilerplate / is_substantive -------------------------------------------------

def test_is_boilerplate_filler():
    assert fe.is_boilerplate("gm everyone 👋")
    assert fe.is_boilerplate("Completed work successfully")
    assert fe.is_boilerplate("[NodeX] Signed oracle beacon | BTC $81660 (-2.2% 24h)")
    assert fe.is_boilerplate("JOB v1 | kabc123 | explain | title | body")
    assert fe.is_boilerplate("ok price ETH 2522 | src=coingecko | t=2026-10-15T00:00:00Z")
    assert fe.is_boilerplate("BTC $81660 (-2.2% 24h) · ETH $2424 (-3.3% 24h)")
    assert fe.is_boilerplate("")


def test_is_boilerplate_real_message_is_not():
    assert not fe.is_boilerplate(GOODQ)
    assert not fe.is_boilerplate("I think single-source price oracles fail on exchange outages.")


def test_is_substantive():
    assert fe.is_substantive(GOODQ, 40)
    assert fe.is_substantive("How do you bound TVL staleness across chains reliably?", 40)
    assert not fe.is_substantive("gm", 40)                       # quá ngắn + filler
    assert not fe.is_substantive("nice weather today, hope you are well friend", 40)  # không crypto, không '?'
    assert not fe.is_substantive("Completed work successfully for the assigned task now", 40)  # filler


# --- engage_due ----------------------------------------------------------------------

def test_engage_due_cooldown():
    now = 1_000_000
    assert fe.engage_due({}, D1, now, 72) is True               # chưa engage bao giờ
    assert fe.engage_due({D1: now - 3600}, D1, now, 72) is False  # mới 1h < 72h
    assert fe.engage_due({D1: now - 73 * 3600}, D1, now, 72) is True  # quá 72h
    assert fe.engage_due({D1: "bad"}, D1, now, 72) is True      # giá trị hỏng -> cho phép


# --- select_engage_targets -----------------------------------------------------------

def test_select_skips_self_unsigned_and_fleet():
    now = 1_000_000
    msgs = [
        _msg(1, ME, GOODQ),                                     # chính mình
        _msg(2, "~nickonly", GOODQ),                            # không ký (nickname) -> census không tính
        _msg(3, D2, GOODQ),                                     # fleet (deny) -> loại
        _msg(4, D1, GOODQ),                                     # hợp lệ
    ]
    out = fe.select_engage_targets(msgs, my_did=ME, deny_dids={D2}, engaged={}, now=now,
                                   cooldown_h=72, max_n=1, min_len=40)
    assert [c["did"] for c in out] == [D1]


def test_select_skips_boilerplate_and_short():
    now = 1_000_000
    msgs = [_msg(1, D1, "gm"), _msg(2, D1, "Completed work successfully done ok"),
            _msg(3, D2, GOODQ)]
    out = fe.select_engage_targets(msgs, my_did=ME, deny_dids=set(), engaged={}, now=now,
                                   cooldown_h=72, max_n=1, min_len=40)
    assert [c["did"] for c in out] == [D2]


def test_select_one_per_did_first_substantive():
    now = 1_000_000
    msgs = [_msg(5, D1, GOODQ), _msg(2, D1, "How do you size jitter to break timing correlation?")]
    out = fe.select_engage_targets(msgs, my_did=ME, deny_dids=set(), engaged={}, now=now,
                                   cooldown_h=72, max_n=1, min_len=40)
    assert len(out) == 1 and out[0]["seq"] == 2                 # theo seq tăng dần -> tin seq 2


def test_select_respects_cooldown():
    now = 1_000_000
    msgs = [_msg(1, D1, GOODQ)]
    out = fe.select_engage_targets(msgs, my_did=ME, deny_dids=set(),
                                   engaged={D1: now - 3600}, now=now,
                                   cooldown_h=72, max_n=1, min_len=40)
    assert out == []                                            # D1 còn trong cooldown


def test_select_pool_bigger_than_max_n():
    now = 1_000_000
    dids = [f"did:key:z6Mk{i:048d}" for i in range(10)]
    msgs = [_msg(i, d, GOODQ) for i, d in enumerate(dids)]
    out = fe.select_engage_targets(msgs, my_did=ME, deny_dids=set(), engaged={}, now=now,
                                   cooldown_h=72, max_n=1, min_len=40)
    assert len(out) == 5                                        # pool = max(max_n*5, 5)


# --- format_engage -------------------------------------------------------------------

def test_format_engage():
    assert fe.format_engage("z6Mk…1111", "What threshold?", "MyAgent") == \
        "[MyAgent] @z6Mk…1111 What threshold?"


# --- run_engage_pass -----------------------------------------------------------------

def _nick(did):
    return did[-4:]


def test_run_engage_live_posts_and_records():
    now = 1_000_000
    state = {}
    posted = []
    n = fe.run_engage_pass(
        [_msg(1, D1, GOODQ)], my_did=ME, deny_dids=set(), state=state, now=now,
        cooldown_h=72, max_n=1, min_len=40, max_dids=500, agent_name="A",
        nick_fn=_nick, question_fn=lambda t: "What exact threshold do you use?",
        post_fn=lambda t: (posted.append(t), True)[1], dry_run=False, log=lambda *a: None)
    assert n == 1
    assert posted and posted[0].startswith("[A] @1111 ")
    assert D1 in state["engaged"]                               # đã ghi cooldown


def test_run_engage_dry_run_no_post_no_record():
    now = 1_000_000
    state = {}
    posted = []
    n = fe.run_engage_pass(
        [_msg(1, D1, GOODQ)], my_did=ME, deny_dids=set(), state=state, now=now,
        cooldown_h=72, max_n=1, min_len=40, max_dids=500, agent_name="A",
        nick_fn=_nick, question_fn=lambda t: "Q?",
        post_fn=lambda t: (posted.append(t), True)[1], dry_run=True, log=lambda *a: None)
    assert n == 1                                               # đếm để log
    assert posted == []                                        # KHÔNG đăng khi dry-run
    assert state.get("engaged", {}) == {}                      # KHÔNG ghi -> live còn engage được


def test_run_engage_skip_tries_next_candidate():
    now = 1_000_000
    state = {}
    posted = []
    # question_fn SKIP (None) cho D1, ra câu hỏi cho D2
    def qf(text):
        return None if "FIRST" in text else "Real question for second?"
    msgs = [_msg(1, D1, "FIRST substantive-ish message about funding rate basis risk?"),
            _msg(2, D2, GOODQ)]
    n = fe.run_engage_pass(
        msgs, my_did=ME, deny_dids=set(), state=state, now=now, cooldown_h=72, max_n=1,
        min_len=40, max_dids=500, agent_name="A", nick_fn=_nick, question_fn=qf,
        post_fn=lambda t: (posted.append(t), True)[1], dry_run=False, log=lambda *a: None)
    assert n == 1 and len(posted) == 1
    assert D2 in state["engaged"] and D1 not in state["engaged"]  # D1 bị SKIP, không ghi


def test_run_engage_respects_max_n():
    now = 1_000_000
    state = {}
    posted = []
    msgs = [_msg(1, D1, GOODQ), _msg(2, D2, GOODQ)]
    n = fe.run_engage_pass(
        msgs, my_did=ME, deny_dids=set(), state=state, now=now, cooldown_h=72, max_n=1,
        min_len=40, max_dids=500, agent_name="A", nick_fn=_nick,
        question_fn=lambda t: "Q real question here?",
        post_fn=lambda t: (posted.append(t), True)[1], dry_run=False, log=lambda *a: None)
    assert n == 1 and len(posted) == 1                         # dừng sau max_n lần ĐĂNG


def test_run_engage_is_dup_skips():
    now = 1_000_000
    state = {}
    posted = []
    n = fe.run_engage_pass(
        [_msg(1, D1, GOODQ)], my_did=ME, deny_dids=set(), state=state, now=now,
        cooldown_h=72, max_n=1, min_len=40, max_dids=500, agent_name="A", nick_fn=_nick,
        question_fn=lambda t: "Q?", post_fn=lambda t: (posted.append(t), True)[1],
        dry_run=False, is_dup=lambda t, who: True, log=lambda *a: None)
    assert n == 0 and posted == []


def test_run_engage_max_n_zero():
    assert fe.run_engage_pass([_msg(1, D1, GOODQ)], my_did=ME, deny_dids=set(), state={},
                              now=1, cooldown_h=72, max_n=0, min_len=40, max_dids=500,
                              agent_name="A", nick_fn=_nick, question_fn=lambda t: "Q?",
                              post_fn=lambda t: True, dry_run=False, log=lambda *a: None) == 0


def test_record_engaged_prunes():
    engaged = {}
    for i in range(12):
        fe._record_engaged(engaged, f"did:key:{i}", now=1000 + i, max_dids=5)
    assert len(engaged) == 5
    assert "did:key:11" in engaged and "did:key:0" not in engaged  # giữ mới, bỏ cũ
