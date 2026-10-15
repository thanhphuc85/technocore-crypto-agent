"""
flop_engage.py — Tạo TƯƠNG TÁC HAI CHIỀU thật với peer ĐỘC LẬP (gated, dry-run mặc định).

Vì sao có module này: census độc lập chấm điểm agent theo REPLY-DISTANCE —
`credit = Σ (theo mỗi responder ĐÃ-KÝ khác nhau) của min(số lần họ trả lời bạn, 8)`.
Nghĩa là CHỈ khi peer đã-ký CHỦ ĐỘNG trả lời agent thì mới có credit; broadcast
(telemetry/oracle/kibble DELIVER) không sinh cạnh nào. `proactive_engage` hiện có chỉ
PHẢN ỨNG (chào khi peer chào, giúp khi peer hỏi) — nó cho đi chứ không GỢI peer trả lời.

Module này chọn một peer độc lập vừa nói điều CÓ CHẤT, rồi đặt MỘT câu hỏi thật, đúng chủ
đề, mời họ trả lời -> sinh một cạnh reply thật từ một key đã-ký khác.

Ràng buộc anti-sybil (khớp posture fleet: KHÔNG tạo self-talking cluster):
  • Loại fleet nhà (deny_dids) — không bao giờ ring giữa các agent của cùng operator.
  • BỀ RỘNG > bề sâu: 1 lần engage / peer mỗi `cooldown_h` (census cap 8/responder nên
    nhè 1 peer vừa vô ích vừa giống ring). Cơ chế ép chạm peer MỚI.
  • Chỉ nội dung THẬT: câu hỏi do LLM soạn bám đúng điều peer nói; soạn không ra -> SKIP
    (đúng tinh thần "thà bỏ còn hơn rải rác" của kibble). Logic LLM do caller tiêm vào.
  • Cap nhỏ/run + pacing (ở post_fn) + default OFF + dry-run.

THUẦN & kiểm thử được: is_boilerplate / is_substantive / engage_due / select_engage_targets
/ format_engage là hàm THUẦN (không mạng, không LLM). run_engage_pass nhận question_fn/
post_fn/nick_fn TIÊM vào -> test tất định, không chạm mạng.

Env (đọc ở agent_cron, truyền xuống):
  FLOP_ENGAGE_ENABLED        bật module (mặc định off).
  FLOP_ENGAGE_DRY_RUN        log "would ask …", KHÔNG đăng (mặc định on khi bật).
  FLOP_ENGAGE_MAX_PER_RUN    số câu engage tối đa/run (mặc định 1).
  FLOP_ENGAGE_COOLDOWN_H     giờ nghỉ tối thiểu / mỗi peer (mặc định 72).
  FLOP_ENGAGE_MIN_MSG_LEN    độ dài tối thiểu để coi tin peer là "có chất" (mặc định 40).
  FLOP_ENGAGE_MAX_DIDS       trần số DID giữ trong state 'engaged' (chống phình, mặc định 500).
"""
import re

# Tin FILLER điển hình trên board (không đáng đặt câu hỏi). Khớp thận trọng để KHÔNG
# loại nhầm câu hỏi/nhận định thật.
_FILLER_PATTERNS = (
    re.compile(r"^\s*(gm|gn|hi|hello|hey|yo|sup|wagmi|chao|ch[aà]o)\b", re.I),
    re.compile(r"\bcompleted?\s+work\b", re.I),
    re.compile(r"\bsigned\s+oracle\s+beacon\b", re.I),
    re.compile(r"\bsigned\s+Ed25519\b", re.I),
    re.compile(r"^\s*(JOB|CLAIM|DELIVER|ATTEST|WITNESS|tclk1?)\b", re.I),  # frame giao thức
    re.compile(r"^\s*(ok|err)\s+\w+.*\|\s*t=", re.I),                     # dòng A2A máy-đọc
)
# Dòng telemetry giá thuần: chủ yếu là "SYM $num (±x% 24h)" nối bằng · — không phải câu hỏi.
_PRICE_LINE = re.compile(r"^[\sA-Z0-9]*\$[\d,]+.*(24h|·|\bgwei\b)", re.I)
_CRYPTO_HINT = re.compile(
    r"\b(price|market|funding|tvl|stable|gas|rollup|l2|htlc|ptlc|nonce|sybil|"
    r"attest|oracle|stake|slashing|reorg|finality|liquidation|depeg|slippage|"
    r"airdrop|flop|did|ed25519|signature|escrow|preimage|mempool|validator)\b", re.I)


def _clean(text: str) -> str:
    """Gộp khoảng trắng / ký tự ẩn thành 1 space (nhẹ — isolate/guard thật ở caller)."""
    return " ".join((text or "").split())


def is_boilerplate(text: str) -> bool:
    """True nếu tin là filler điển hình (chào/telemetry/frame/dòng-máy) -> bỏ qua."""
    t = _clean(text)
    if not t:
        return True
    if _PRICE_LINE.match(t):
        return True
    return any(p.search(t) for p in _FILLER_PATTERNS)


def is_substantive(text: str, min_len: int) -> bool:
    """True nếu tin ĐÁNG đặt câu hỏi: đủ dài, không filler, và hoặc là câu hỏi hoặc
    chạm chủ đề crypto/giao thức (để câu hỏi ngược lại có chỗ bám)."""
    t = _clean(text)
    if len(t) < min_len or is_boilerplate(t):
        return False
    return ("?" in t) or bool(_CRYPTO_HINT.search(t))


def engage_due(engaged: dict, did: str, now: int, cooldown_h: float) -> bool:
    """True nếu peer `did` CHƯA được engage trong `cooldown_h` giờ gần nhất (ép bề rộng)."""
    last = (engaged or {}).get(did)
    if last is None:
        return True
    try:
        return (now - int(last)) >= cooldown_h * 3600
    except (TypeError, ValueError):
        return True


def select_engage_targets(messages, *, my_did, deny_dids, engaged, now,
                          cooldown_h, max_n, min_len):
    """Chọn POOL peer để engage (nhiều hơn max_n để còn thử khi 1 ứng viên SKIP).
    THUẦN. Loại: chính mình, key KHÔNG ký (nickname tự xưng — census không tính),
    fleet nhà (deny_dids), peer còn trong cooldown, tin filler/không-có-chất. Mỗi DID
    lấy TIN ĐẦU đủ-chất (theo seq tăng dần). Trả list dict {did,text,seq}."""
    deny = set(deny_dids or ())
    pool_cap = max(max_n * 5, 5)
    seen = set()
    out = []
    for m in sorted(messages or [], key=lambda x: x.get("seq", 0)):
        frm = m.get("from", "")
        if (not frm.startswith("did:key:")) or frm == my_did or frm in deny or frm in seen:
            continue
        if not engage_due(engaged, frm, now, cooldown_h):
            continue
        text = _clean(m.get("text", ""))
        if not is_substantive(text, min_len):
            continue
        seen.add(frm)
        out.append({"did": frm, "text": text, "seq": m.get("seq", 0)})
        if len(out) >= pool_cap:
            break
    return out


def format_engage(nick: str, question: str, agent_name: str) -> str:
    """Dòng engage chuẩn: '[Agent] @nick <câu hỏi>' (cùng style reply hiện có)."""
    return f"[{agent_name}] @{nick} {_clean(question)}"


def _record_engaged(engaged: dict, did: str, now: int, max_dids: int) -> None:
    """Ghi dấu đã-engage peer (prune để state không phình)."""
    engaged[did] = now
    if len(engaged) > max_dids:
        # bỏ các mục CŨ nhất
        for d in sorted(engaged, key=lambda k: engaged[k])[: len(engaged) - max_dids]:
            engaged.pop(d, None)


def run_engage_pass(messages, *, my_did, deny_dids, state, now, cooldown_h, max_n,
                    min_len, max_dids, agent_name, nick_fn, question_fn, post_fn,
                    dry_run=True, is_dup=None, log=print):
    """Chạy 1 lượt engage. Chọn pool -> với mỗi ứng viên gọi question_fn(text) (trả câu
    hỏi thật hoặc None=SKIP) -> đăng @peer qua post_fn cho tới khi đủ max_n lần ĐĂNG.
    Dry-run: log 'would ask …', KHÔNG đăng và KHÔNG ghi engaged (để lúc live còn engage
    được). Chỉ ghi 'engaged' khi ĐÃ đăng thật -> cooldown chỉ tính sau khi tạo cạnh thật.
    `is_dup(text, did)` (tùy chọn) chặn gửi trùng. Trả số câu đã đăng."""
    if max_n <= 0:
        return 0
    engaged = state.setdefault("engaged", {})
    pool = select_engage_targets(
        messages, my_did=my_did, deny_dids=deny_dids, engaged=engaged, now=now,
        cooldown_h=cooldown_h, max_n=max_n, min_len=min_len)
    posted = 0
    for cand in pool:
        if posted >= max_n:
            break
        did, text = cand["did"], cand["text"]
        q = question_fn(text)
        if not q:                                   # model SKIP / không soạn được -> bỏ ứng viên
            log(f"[engage] skip {nick_fn(did)} — không soạn được câu hỏi thật")
            continue
        msg = format_engage(nick_fn(did), q, agent_name)
        if is_dup and is_dup(msg, did):
            log(f"[engage] bỏ trùng -> {nick_fn(did)}")
            continue
        if dry_run:
            log(f"[engage] DRY-RUN would ask {nick_fn(did)}: {q[:120]}")
            posted += 1                             # đếm để log; KHÔNG ghi engaged khi dry-run
            continue
        if post_fn(msg):
            _record_engaged(engaged, did, now, max_dids)
            posted += 1
            log(f"[engage] asked {nick_fn(did)} (seq {cand['seq']})")
    return posted
