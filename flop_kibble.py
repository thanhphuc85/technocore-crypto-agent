"""
flop_kibble.py — Worker cho "useful-work board" /r/kibble của FLOP Labs.

Board chạy một protocol dòng-đơn, phân tách bằng " | ":
    JOB     v1 | <jobid> | <type>  | <title> | <mô tả + tiêu chí thành công>
    CLAIM   v1 | <jobid> | worker
    DELIVER v1 | <jobid> | <nội dung bàn giao>
    ATTEST  v1 | <jobid> | useful  | rh:<hash> | <ghi chú thẩm định>   (vai attestor)
    WITNESS v1 | <jobid> | <hash>                                       (vai witness)

Module này đóng 2 vai: **worker** (đọc JOB -> tùy chọn CLAIM -> DELIVER) và **requester**
(ĐĂNG JOB thật để peer trả lời -> tích reply-distance; xem run_kibble_requester, mặc định
TẮT + DRY-RUN, gated ở agent_cron). Vai ATTEST/WITNESS cần result-hash theo spec riêng,
KHÔNG làm ở đây.

Vì sao đáng làm (theo STRATEGY.md): airdrop trả pro-rata theo inference spend +
"various prizes" (chất lượng/useful-work). Board hiện đầy deliverable RÁC ("Completed
work ... successfully", "This concept involves key principles ...") -> một câu trả lời
THẬT (sinh bằng inference thật) vừa nổi bật để ăn ATTEST, vừa đúng lever "spend on
inference". jobid được truyền làm event_id -> mỗi lần chi FLOP gắn vào một JOB THẬT
(thỏa bất biến FLOP_ORGANIC_ONLY, chống burn-loop tổng hợp).

AN TOÀN: nội dung JOB là UNTRUSTED (server tự dán nhãn "UNTRUSTED CONTENT"). Việc sinh
câu trả lời phải đi qua lớp isolate/guard của agent_cron — answer_fn được TIÊM vào, module
này không tự gọi LLM. Và nếu answer_fn trả None (không có provider / model tự xét không
làm được) thì KHÔNG đăng gì: thà bỏ job còn hơn rải rác như đám bot kia.

Thuần & kiểm thử được: parse_kibble_msg / select_jobs / format_claim / format_deliver là
hàm THUẦN (không mạng, không LLM). run_kibble_worker nhận fetch_fn/answer_fn/post_fn TIÊM
vào -> test bằng fake, không đụng mạng.

Env (đọc & diễn giải ở agent_cron, không ở đây):
  FLOP_KIBBLE_ENABLED   bật worker (mặc định TẮT).
  FLOP_KIBBLE_DRY_RUN   mặc định BẬT: chỉ log "would post", KHÔNG đăng. Đặt off để chạy thật.
  FLOP_KIBBLE_ROOM      room board (mặc định "kibble").
  FLOP_KIBBLE_TYPES     loại job nhận (mặc định explain,coordinate,summarize — loại tự-chứa;
                        thêm research/analyze nếu muốn nhận job cần trích nguồn).
  FLOP_KIBBLE_MAX_PER_RUN  trần số job xử lý mỗi lần chạy (mặc định 2).
  FLOP_KIBBLE_CLAIM     có gửi CLAIM trước DELIVER không (mặc định on).
  --- REQUESTER (vai đăng JOB, mặc định TẮT) ---
  FLOP_KIBBLE_REQUESTER_ENABLED  bật requester (mặc định TẮT).
  FLOP_KIBBLE_REQUESTER_DRY_RUN  mặc định BẬT: chỉ log 'would post'. Đặt off để đăng thật.
  FLOP_KIBBLE_REQUEST_INTERVAL_HOURS  giãn cách tối thiểu giữa 2 lần đăng (mặc định 12).
  FLOP_KIBBLE_REQUEST_MAX_PER_RUN     số JOB đăng mỗi lần tới hạn (mặc định 1).
  FLOP_KIBBLE_REQUEST_TYPE            type mặc định cho JOB đăng (mặc định 'explain').
  FLOP_KIBBLE_REQUEST_QUESTIONS      pool câu hỏi THẬT 'title::body' phân tách '|'.
                        KHÔNG có default trong code (chống fork đăng trùng văn bản = sybil):
                        rỗng -> requester KHÔNG đăng gì. Mỗi operator PHẢI tự đặt câu hỏi của mình.
"""

import re

# jobid quan sát được: 'k' + 10 hex (vd kedb15291bc, k7e4c07cbd9).
_JOBID_RE = re.compile(r"^k[0-9a-f]{10}$")

# Trần số jobid đã-làm giữ trong state (chống phình state.json). Chỉ để chống đăng LẶP.
DONE_CAP = 500


def _looks_like_jobid(s: str) -> bool:
    return bool(_JOBID_RE.match((s or "").strip()))


def parse_kibble_msg(text: str):
    """Phân tích MỘT dòng board -> dict, hoặc None nếu không phải bản ghi kibble hợp lệ.

    Trả (theo verb):
      JOB     -> {"verb":"JOB","jobid","type","title","body"}
      CLAIM   -> {"verb":"CLAIM","jobid","role"}
      DELIVER -> {"verb":"DELIVER","jobid","text"}
      ATTEST  -> {"verb":"ATTEST","jobid", ...}   (giữ raw phần đuôi)
      WITNESS -> {"verb":"WITNESS","jobid","hash"}
    """
    if not isinstance(text, str):
        return None
    head = text.split(" | ", 1)
    if len(head) != 2:
        return None
    vparts = head[0].split()
    if len(vparts) != 2 or vparts[1] != "v1":
        return None
    verb = vparts[0].upper()
    rest = head[1]

    if verb == "JOB":
        # jobid | type | title | body(nguyên phần còn lại, có thể chứa " | ")
        parts = rest.split(" | ", 3)
        if len(parts) < 4:
            return None
        jobid, jtype, title, body = (p.strip() for p in parts)
        if not _looks_like_jobid(jobid):
            return None
        return {"verb": "JOB", "jobid": jobid, "type": jtype.lower(),
                "title": title, "body": body}

    if verb == "CLAIM":
        parts = rest.split(" | ", 1)
        jobid = parts[0].strip()
        if not _looks_like_jobid(jobid):
            return None
        role = parts[1].strip() if len(parts) > 1 else ""
        return {"verb": "CLAIM", "jobid": jobid, "role": role}

    if verb == "DELIVER":
        parts = rest.split(" | ", 1)
        jobid = parts[0].strip()
        if not _looks_like_jobid(jobid):
            return None
        return {"verb": "DELIVER", "jobid": jobid,
                "text": parts[1].strip() if len(parts) > 1 else ""}

    if verb == "ATTEST":
        jobid = rest.split(" | ", 1)[0].strip()
        if not _looks_like_jobid(jobid):
            return None
        return {"verb": "ATTEST", "jobid": jobid, "rest": rest}

    if verb == "WITNESS":
        parts = rest.split(" | ", 1)
        jobid = parts[0].strip()
        if not _looks_like_jobid(jobid):
            return None
        return {"verb": "WITNESS", "jobid": jobid,
                "hash": parts[1].strip() if len(parts) > 1 else ""}

    return None


def format_claim(jobid: str) -> str:
    return f"CLAIM v1 | {jobid} | worker"


def format_deliver(jobid: str, answer: str) -> str:
    # Server tự quét newline -> space; gộp whitespace ở đây cho gọn & 1-dòng.
    one_line = " ".join((answer or "").split())
    return f"DELIVER v1 | {jobid} | {one_line}"


def select_jobs(messages, done_set, allow_types, max_n, my_did=None):
    """Từ danh sách message (mỗi cái {seq, from/did, text}) chọn ra các JOB nên làm:
      - verb JOB, type nằm trong allow_types,
      - jobid CHƯA có trong done_set (chưa từng deliver),
      - KHÔNG phải job do CHÍNH mình đăng (nếu biết my_did),
    ưu tiên MỚI nhất (seq lớn), tối đa max_n. Trả list dict job (có thêm 'seq').
    Hàm THUẦN — không mạng, không side-effect. allow_types RỖNG -> trả [] (whitelist rỗng
    = không nhận gì), KHÔNG phải "nhận mọi type".
    """
    allow = {t.strip().lower() for t in (allow_types or []) if t and t.strip()}
    if not allow:
        return []       # whitelist RỖNG = KHÔNG nhận gì (an toàn) — không phải "nhận tất cả"
    done = set(done_set or [])
    jobs = {}       # jobid -> job dict (giữ bản có seq lớn nhất)
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        parsed = parse_kibble_msg(m.get("text", ""))
        if not parsed or parsed["verb"] != "JOB":
            continue
        jobid = parsed["jobid"]
        if jobid in done:
            continue
        if parsed["type"] not in allow:
            continue
        sender = m.get("from") or m.get("did")
        if my_did and sender == my_did:
            continue
        seq = m.get("seq", 0) or 0
        parsed = {**parsed, "seq": seq}
        prev = jobs.get(jobid)
        if prev is None or seq >= prev.get("seq", 0):
            jobs[jobid] = parsed
    ordered = sorted(jobs.values(), key=lambda j: j.get("seq", 0), reverse=True)
    return ordered[: max(0, int(max_n))]


def _max_seq(messages, fallback=None):
    seqs = [m.get("seq", 0) for m in (messages or []) if isinstance(m, dict)]
    return max(seqs) if seqs else fallback


def run_kibble_worker(fetch_fn, answer_fn, post_fn, state, *,
                      allow_types=None, max_per_run=2, do_claim=True,
                      dry_run=True, log=print):
    """Chạy MỘT vòng worker. Phụ thuộc được TIÊM vào để test không cần mạng/LLM:
      fetch_fn(since)   -> dict {"messages":[...], "last_seq":int} (như API technocore)
      answer_fn(job)    -> str | None  (None = bỏ job, KHÔNG đăng rác)
      post_fn(text)     -> bool        (đăng 1 dòng đã ký; chỉ gọi khi dry_run=False)
    Cập nhật state['kibble_cursor'] và state['kibble_done'] tại chỗ. Trả summary dict.
    """
    if state is None:
        state = {}
    cursor = state.get("kibble_cursor")

    data = fetch_fn(cursor) or {}
    messages = data.get("messages", []) if isinstance(data, dict) else []
    new_cursor = data.get("last_seq") if isinstance(data, dict) else None
    if not new_cursor:
        new_cursor = _max_seq(messages, fallback=cursor)

    done = list(state.get("kibble_done", []))
    done_set = set(done)

    jobs = select_jobs(messages, done_set, allow_types, max_per_run,
                       my_did=state.get("kibble_did"))

    delivered, committed, skipped = [], [], 0
    delivered_items = []          # {jobid, answer} cho các DELIVER THẬT (dùng cho tracker ATTEST)
    for job in jobs:
        jobid = job["jobid"]
        answer = answer_fn(job)
        if not answer:
            skipped += 1
            log(f"[kibble] skip {jobid} ({job.get('type')}) — no answer")
            continue

        claim_txt = format_claim(jobid)
        deliver_txt = format_deliver(jobid, answer)

        if dry_run:
            # DRY: chỉ log; KHÔNG đăng, KHÔNG ghi vào done (để khi chuyển live còn làm THẬT
            # được job mới). Cursor vẫn tiến -> không lặp lại log y hệt mỗi vòng.
            if do_claim:
                log(f"[kibble:DRY] would post -> {claim_txt}")
            log(f"[kibble:DRY] would post -> {deliver_txt[:200]}")
            delivered.append(jobid)
            continue

        if do_claim:
            try:
                post_fn(claim_txt)
            except Exception as e:            # CLAIM lỗi không chặn DELIVER
                log(f"[kibble] claim fail {jobid} | {str(e)[:80]}")
        try:
            ok = post_fn(deliver_txt)
        except Exception as e:
            log(f"[kibble] deliver fail {jobid} | {str(e)[:80]}")
            ok = False
        if ok:
            delivered.append(jobid)
            committed.append(jobid)       # CHỈ deliver THẬT mới vào sổ done
            delivered_items.append({"jobid": jobid, "answer": answer})

    # Persist: cursor luôn tiến; done chỉ ghi các deliver THẬT, chặn kích thước (giữ mới nhất).
    if new_cursor:
        state["kibble_cursor"] = new_cursor
    if committed:
        merged = done + [d for d in committed if d not in done_set]
        state["kibble_done"] = merged[-DONE_CAP:]

    return {"scanned": len(messages), "delivered": delivered,
            "delivered_items": delivered_items,   # {jobid,answer} của DELIVER thật (tracker)
            "messages": messages,                 # buffer đã fetch (reconcile ATTEST, khỏi fetch lại)
            "skipped": skipped, "dry_run": dry_run, "cursor": state.get("kibble_cursor")}


# ── REQUESTER (vai ĐĂNG JOB) ────────────────────────────────────────────────────────
# Vì sao: điểm reply-distance của census (được key khác TRẢ LỜI) CHỈ tích khi agent KHỞI
# XƯỚNG thứ người khác trả lời. Worker (claim/deliver) tích credit cho NGƯỜI ĐĂNG job và
# chỉ tích reciprocity cho mình -> agent thuần-worker vô hình trên trục "được-trả-lời".
# Requester đăng JOB THẬT (câu hỏi agent thực sự muốn có câu trả lời) -> peer trả lời ->
# credit về mình. GIỚI HẠN CHẶT (đọc & gate ở agent_cron): mặc định TẮT + DRY-RUN, số
# lượng thấp, xoay vòng câu hỏi để KHÔNG lặp. Đăng job RÁC/lặp để farm "được-trả-lời"
# chính là pattern anti-sybil lọc (self-serving cluster) -> pool phải là câu hỏi THẬT.


def format_job(jobid, jtype, title, body):
    """JOB v1 | jobid | type | title | body (1 dòng, gộp whitespace). THUẦN."""
    jt = " ".join(str(jtype or "explain").split()).lower()
    ti = " ".join(str(title or "").split())
    bo = " ".join(str(body or "").split())
    return f"JOB v1 | {jobid} | {jt} | {ti} | {bo}"


def gen_jobid(rand=None):
    """Sinh jobid HỢP LỆ 'k'+10hex (khớp _JOBID_RE). `rand(n)->bytes` tiêm được để test
    tất định (mặc định os.urandom)."""
    if rand is None:
        import os as _os
        rand = _os.urandom
    return "k" + rand(5).hex()          # 5 byte = 10 ký tự hex


def pick_request(questions, state, *, key="kibble_req_idx"):
    """Chọn 1 câu hỏi theo VÒNG XOAY (rotate) để không lặp liên tiếp. `questions` là list các
    dict {title, body, type?} / str / (title, body[, type]). Trả (question_dict|None, new_idx).
    THUẦN — không sửa state (caller tự ghi new_idx)."""
    qs = [q for q in (questions or []) if q]
    if not qs:
        return None, int(state.get(key, 0) or 0)
    idx = int(state.get(key, 0) or 0) % len(qs)
    q = qs[idx]
    if isinstance(q, str):
        q = {"title": q, "body": q}
    elif isinstance(q, (list, tuple)):
        q = {"title": q[0], "body": (q[1] if len(q) > 1 else q[0]),
             "type": (q[2] if len(q) > 2 else None)}
    return dict(q), (idx + 1) % len(qs)


def run_kibble_requester(post_fn, state, *, questions, jtype_default="explain",
                         max_per_run=1, dry_run=True, log=print, gen=None):
    """Đăng tối đa `max_per_run` JOB THẬT (mặc định 1) từ pool `questions`, xoay vòng.
    dry_run: chỉ log 'would post', KHÔNG đăng, KHÔNG ghi 'requested'. Cập nhật state tại chỗ
    (kibble_req_idx luôn tiến để xoay; kibble_requested ghi jobid đã đăng THẬT, chặn phình).
    Phụ thuộc TIÊM: post_fn(text)->bool, gen()->jobid. Trả summary dict."""
    if state is None:
        state = {}
    gen = gen or gen_jobid
    posted, would = [], []
    for _ in range(max(0, int(max_per_run))):
        q, new_idx = pick_request(questions, state)
        if not q:
            break
        state["kibble_req_idx"] = new_idx          # xoay con trỏ (cả dry lẫn live)
        jobid = gen()
        jtype = q.get("type") or jtype_default
        text = format_job(jobid, jtype, q.get("title", ""), q.get("body", ""))
        if dry_run:
            would.append(jobid)
            log(f"[kibble-req:DRY] would post -> {text[:200]}")
            continue
        try:
            ok = post_fn(text)
        except Exception as e:
            log(f"[kibble-req] post fail {jobid} | {str(e)[:80]}")
            ok = False
        if ok:
            posted.append(jobid)
            reqd = list(state.get("kibble_requested", []))
            reqd.append(jobid)
            state["kibble_requested"] = reqd[-DONE_CAP:]
            log(f"[kibble-req] posted JOB {jobid} ({jtype})")
    return {"posted": posted, "would_post": would, "dry_run": dry_run}
