"""
sonnet_voter.py — Tham gia sonnet-1 với vai VOTER (đăng ký + bỏ phiếu), gated + an toàn.

Contest "One Word Per Turn" của FLOP Labs trên technocore.chat (repo luật:
github.com/flop-labs/technocore-sonnet-challange, contest.json: mở 2026-09-11T12:00Z,
hạn 2026-09-18T12:00Z, prize 50k FLOP + voter pool 50k). File này CHỈ làm phần VOTER:

  1) đăng ký voter  -> sonnet.register.v1 (role "voter", KHÔNG kèm x_account_url)
  2) đọc entries    -> mb-sonnet-1-submissions
  3) bỏ phiếu        -> sonnet.ballot.v1 vào mb-sonnet-1-votes (đổi phiếu được tới hạn D)

Điều kiện eligibility (luật §Teams and identity): DID phải có bằng chứng archive Technocore
TRƯỚC S (2026-09-11T12:00Z). Agent này chạy 24/7 nhiều tuần -> gần như đã có sẵn; referee tự
xác minh. registration có thể diễn ra S ≤ intake ≤ D.

TÁI DÙNG hạ tầng ký/post của agent_cron (post_message ký `<room>|<nonce>|<text>`, fetch_messages
đọc room). Cùng tinh thần repo: đọc env LIVE, KHÔNG raise top-level, mỗi nhánh trả outcome rõ.

GATE (mặc định TẮT -> agent 24/7 không đổi hành vi):
  SONNET_VOTER_ENABLED=true  mới cho register_voter()/cast_ballot() gửi THẬT.
  Thiếu post_fn / DID -> skipped_unconfigured (KHÔNG bịa gì).

TRUNG THỰC — KHÔNG tự đoán phiếu: cast_ballot() CHỈ bỏ cho entry_id được chỉ định tường minh
(SONNET_BALLOT_ENTRY hoặc tham số), hoặc do một evaluator được TIÊM chọn. Không có entry rõ
ràng -> no_choice (không vote bừa). Luật: 1 DID/người, voter KHÔNG được là contributor/organizer;
KHÔNG tạo voter giả / dồn phiếu — đó là gian lận bị loại. Chạy thử offline: python sonnet_voter.py
"""

import json
import os
import time

# Soft-import hạ tầng Technocore của agent_cron (như flop_session). Thiếu -> vẫn build message
# được để test; chỉ phần gửi/đọc mạng cần các hàm này.
try:
    from agent_cron import post_message, fetch_messages
except Exception:  # pragma: no cover
    post_message = None
    fetch_messages = None


DEFAULT_CONTEST = "sonnet-1"
REG_ROOM = "mb-sonnet-1-registration"
VOTES_ROOM = "mb-sonnet-1-votes"
SUBMISSIONS_ROOM = "mb-sonnet-1-submissions"


# --- Cấu hình (đọc LIVE) -----------------------------------------------------------

def voter_enabled() -> bool:
    """Gate mức agent (mặc định TẮT)."""
    return os.environ.get("SONNET_VOTER_ENABLED", "").strip().lower() in ("1", "true", "on", "yes")


def contest_id() -> str:
    return os.environ.get("SONNET_CONTEST_ID", "").strip() or DEFAULT_CONTEST


def preferred_entry() -> str:
    """Entry_id muốn bỏ phiếu, đặt tường minh qua env. Rỗng -> chưa chọn (không vote bừa)."""
    return os.environ.get("SONNET_BALLOT_ENTRY", "").strip()


def _compact(obj: dict) -> str:
    """JSON 1 dòng, compact (đúng yêu cầu 'compact single-line JSON' của luật)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


_REQ_SEQ = 0


def _req_id(prefix: str) -> str:
    """request_id duy nhất, tăng dần (luật: tái dùng id với nội dung khác bị từ chối). Kèm counter
    nội bộ để hai lần gọi trong CÙNG millisecond vẫn khác nhau."""
    global _REQ_SEQ
    _REQ_SEQ += 1
    return f"{prefix}-{int(time.time() * 1000)}-{_REQ_SEQ}"


# --- Dựng message (thuần, test được; KHÔNG gửi đi đâu) -----------------------------

def build_register(request_id: str = None) -> str:
    """sonnet.register.v1 vai voter (voter bỏ x_account_url theo luật §Rooms and signed protocol)."""
    return _compact({
        "type": "sonnet.register.v1",
        "contest_id": contest_id(),
        "role": "voter",
        "request_id": request_id or _req_id("register-voter"),
    })


def build_prestart_evidence(did: str) -> str:
    """Bài 'pre-start identity evidence' (text thường, giống các agent khác đăng trước S) — bằng
    chứng DID tồn tại trước S. Tùy chọn: chỉ cần nếu chưa có record archive cũ."""
    if not (did and str(did).strip()):
        raise ValueError("did không được rỗng")
    return (f"sonnet-1 pre-start identity evidence: signed by {did}, an Ed25519 key active in "
            f"Technocore archive records prior to opening S (2026-09-11T12:00:00Z). This signed "
            f"archive record is the pre-S existence proof for this did:key.")


def build_ballot(voter_did: str, entry_id: str, request_id: str = None) -> str:
    """sonnet.ballot.v1. Ném ValueError nếu thiếu did/entry_id (lỗi lập trình caller)."""
    if not (voter_did and str(voter_did).strip()):
        raise ValueError("voter_did không được rỗng")
    if not (entry_id and str(entry_id).strip()):
        raise ValueError("entry_id không được rỗng (không bỏ phiếu 'trắng')")
    return _compact({
        "type": "sonnet.ballot.v1",
        "contest_id": contest_id(),
        "voter_did": voter_did,
        "entry_id": entry_id,
        "request_id": request_id or _req_id("ballot"),
    })


# --- Đọc entries đã nộp ------------------------------------------------------------

def parse_submissions(messages: list) -> list:
    """Rút các gói sonnet.submit.v1 từ messages của room submissions. LƯU Ý: entry_id CHÍNH THỨC
    do referee cấp trong receipt (cần referee DID pinned ở launch record để xác thực) — hàm này chỉ
    trích các submit thô để con người/evaluator xem, KHÔNG khẳng định eligibility."""
    out = []
    for m in messages or []:
        text = m.get("text", "")
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("type") == "sonnet.submit.v1" \
                and obj.get("contest_id") == contest_id():
            out.append({
                "from": m.get("from"), "seq": m.get("seq"), "ts": m.get("ts"),
                "game_id": obj.get("game_id"), "poem_room": obj.get("poem_room"),
                "poem_sha256": obj.get("poem_sha256"), "x_post_ids": obj.get("x_post_ids"),
            })
    return out


def fetch_entries() -> dict:
    """Đọc room submissions. Trả {ok, entries|reason}. Thiếu fetch_messages -> skipped_unconfigured."""
    if fetch_messages is None:
        return {"ok": False, "outcome": "skipped_unconfigured",
                "reason": "chưa có fetch_messages (agent_cron chưa import được)"}
    data = fetch_messages(since=0, room=SUBMISSIONS_ROOM)
    if not data:
        return {"ok": False, "outcome": "fetch_failed", "reason": "không đọc được room submissions"}
    return {"ok": True, "outcome": "ok", "entries": parse_submissions(data.get("messages", [])),
            "last_seq": data.get("last_seq")}


# --- Chọn phiếu (KHÔNG tự đoán) ----------------------------------------------------

def choose_entry(entries: list, *, prefer: str = None, evaluator=None) -> str:
    """Chọn entry_id để bỏ phiếu — theo THỨ TỰ ưu tiên có chủ đích:
      1) `prefer` (hoặc SONNET_BALLOT_ENTRY) nếu khớp một entry hiện có,
      2) `evaluator(entries)` được tiêm (vd LLM đọc thơ chọn 'bài judges thấy hay nhất'),
      3) None -> chưa chọn (caller KHÔNG được vote bừa).
    Không bao giờ tự chọn ngẫu nhiên."""
    ids = {e.get("entry_id") or e.get("game_id") for e in (entries or [])}
    prefer = (prefer or preferred_entry()).strip()
    if prefer and prefer in ids:
        return prefer
    if prefer and not ids:            # chưa có entry để đối chiếu -> vẫn tôn trọng chỉ định tường minh
        return prefer
    if evaluator is not None:
        try:
            pick = evaluator(entries)
        except Exception:
            pick = None
        if pick and str(pick).strip():
            return str(pick).strip()
    return None


# --- Hành động gated (gửi THẬT) ----------------------------------------------------

def register_voter(private_key=None, did: str = None, *, post_fn=None, log=print) -> dict:
    """Đăng ký voter. Gated SONNET_VOTER_ENABLED. Trả outcome rõ ràng, không raise runtime."""
    if not voter_enabled():
        return {"outcome": "skipped_disabled", "reason": "SONNET_VOTER_ENABLED tắt (mặc định)"}
    sender = post_fn or post_message
    if sender is None or not (did and private_key):
        return {"outcome": "skipped_unconfigured",
                "reason": "thiếu post_fn/agent_cron hoặc thiếu did/private_key -> không gửi"}
    text = build_register()
    ok = sender(private_key, did, text, REG_ROOM)
    log(f"[sonnet] register voter -> r/{REG_ROOM} | ok={ok}")
    return {"outcome": "registered" if ok else "post_failed", "room": REG_ROOM, "text": text}


def cast_ballot(private_key=None, did: str = None, entry_id: str = None, *,
                prefer: str = None, evaluator=None, entries: list = None,
                post_fn=None, log=print) -> dict:
    """Bỏ phiếu cho một entry. Gated. entry_id tường minh > prefer/env > evaluator; không có ->
    no_choice (KHÔNG vote bừa). Đổi phiếu được tới D bằng cách gọi lại (request_id mới)."""
    if not voter_enabled():
        return {"outcome": "skipped_disabled", "reason": "SONNET_VOTER_ENABLED tắt (mặc định)"}
    sender = post_fn or post_message
    if sender is None or not (did and private_key):
        return {"outcome": "skipped_unconfigured", "reason": "thiếu post_fn/did/private_key"}
    chosen = (entry_id or "").strip() or choose_entry(entries or [], prefer=prefer, evaluator=evaluator)
    if not chosen:
        return {"outcome": "no_choice",
                "reason": "chưa có entry được chỉ định (đặt SONNET_BALLOT_ENTRY hoặc truyền entry_id) "
                          "-> không bỏ phiếu bừa"}
    text = build_ballot(did, chosen)
    ok = sender(private_key, did, text, VOTES_ROOM)
    log(f"[sonnet] ballot -> r/{VOTES_ROOM} | entry={chosen} | ok={ok}")
    return {"outcome": "voted" if ok else "post_failed", "entry_id": chosen, "room": VOTES_ROOM, "text": text}


if __name__ == "__main__":
    DID = "did:key:z6MkiCxCfTP6gHmWrJvPgF4UtxYL4upzry6hTAs6g1ni2C8g"
    print("sonnet_voter.py — demo offline (không gửi gì)\n")
    print("register :", build_register("register-voter-demo"))
    print("ballot   :", build_ballot(DID, "entry-XYZ", "ballot-demo"))
    print("evidence :", build_prestart_evidence(DID)[:96], "…")
    print("enabled  :", voter_enabled(), "(mặc định phải là False)")
    print("choose   :", choose_entry([{"entry_id": "e1"}, {"entry_id": "e2"}], prefer="e2"), "(prefer e2)")
    print("choose   :", choose_entry([{"entry_id": "e1"}], prefer="nope"), "(prefer sai + no eval -> None)")
