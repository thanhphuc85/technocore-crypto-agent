"""
flop_close_call.py — Tham gia contest "Close Call" của FLOP Labs với vai OWNER (chỉ ĐĂNG KÝ),
gated + an toàn. KHÔNG giao dịch (trading là chiến lược riêng, ngoài phạm vi file này).

Contest "one bet on NVIDIA" trên technocore.chat (repo luật:
github.com/flop-labs/technocore-close-call-challenge, contest.json: mở
2026-09-25T12:00Z, khoá 2026-10-04T09:00Z, prize 1,000,000 FLOP chia top-3 sau mainnet).

Đăng ký (luật §Agent prompt bước 1): post 1 tin ĐÃ KÝ
    {"t":"owner","season":"close-1","key":"<did:key của bạn>"}
vào room trading `close1` (đã đăng ký sẵn từ đầu). Sweep KẾ TIẾP cấp 10,000 POLF.
MINT MỘT LẦN/khoá, đăng ký lúc nào cũng được TRƯỚC khi khoá.

TÁI DÙNG hạ tầng ký/post của agent_cron: post_message ký `<room>|<nonce>|<text>` —
đúng lane mà luật §Messages and signing yêu cầu. Cùng tinh thần repo: đọc env LIVE,
KHÔNG raise top-level, mỗi nhánh trả outcome rõ ràng.

GATE (mặc định TẮT -> agent 24/7 không đổi hành vi):
  CLOSE_CALL_ENABLED=true  mới cho register_owner() gửi THẬT.
  Thiếu post_fn / DID / private_key -> skipped_unconfigured (KHÔNG bịa gì).

Referee DID (trust anchor, pin trong seed/launch record) chỉ để THAM CHIẾU/xác minh về sau;
đăng ký owner KHÔNG cần verify referee. Chạy thử offline: python flop_close_call.py
"""

import json
import os
import time

# Soft-import hạ tầng Technocore của agent_cron (như sonnet_voter). Thiếu -> vẫn build message
# được để test; chỉ phần gửi mạng cần hàm này.
try:
    from agent_cron import post_message
except Exception:  # pragma: no cover
    post_message = None


DEFAULT_SEASON = "close-1"
DEFAULT_ROOM = "close1"            # room trading đăng ký sẵn từ đầu (luật §Rooms)
# Referee DID pin trong seed message của contest — TRÙNG referee sonnet-2 của FLOP Labs.
# Chỉ để tham chiếu/verify về sau; override khi FLOP đổi qua CLOSE_CALL_REFEREE_DID.
DEFAULT_REFEREE_DID = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
# Khoá (lock) — sau mốc này đăng ký vô nghĩa (luật §The lock). Chặn post thừa.
DEFAULT_LOCK_ISO = "2026-10-04T09:00:00Z"


# --- Cấu hình (đọc LIVE) -----------------------------------------------------------

def enabled() -> bool:
    """Gate mức agent (mặc định TẮT)."""
    return os.environ.get("CLOSE_CALL_ENABLED", "").strip().lower() in ("1", "true", "on", "yes")


def season() -> str:
    return os.environ.get("CLOSE_CALL_SEASON", "").strip() or DEFAULT_SEASON


def room() -> str:
    return os.environ.get("CLOSE_CALL_ROOM", "").strip() or DEFAULT_ROOM


def referee_did() -> str:
    return os.environ.get("CLOSE_CALL_REFEREE_DID", "").strip() or DEFAULT_REFEREE_DID


def lock_iso() -> str:
    return os.environ.get("CLOSE_CALL_LOCK", "").strip() or DEFAULT_LOCK_ISO


def _past_lock(now: float = None) -> bool:
    """True nếu đã qua mốc khoá (đăng ký không còn ý nghĩa). Sai định dạng -> coi như CHƯA khoá
    (an toàn: thà thử đăng ký còn hơn tự chặn nhầm)."""
    iso = lock_iso().replace("Z", "+00:00")
    try:
        from datetime import datetime, timezone
        lock = datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return False
    return (now if now is not None else time.time()) >= lock


# --- Dựng message (thuần, test được; KHÔNG gửi đi đâu) -----------------------------

def build_owner(did: str) -> str:
    """owner-message theo ĐÚNG shape luật §Messages and signing:
    {"t":"owner","season":"<season>","key":"<did>"} — JSON 1 dòng, compact.
    Giữ thứ tự khoá t/season/key như ví dụ trong luật."""
    if not (did and str(did).strip()):
        raise ValueError("did không được rỗng")
    return json.dumps(
        {"t": "owner", "season": season(), "key": did},
        separators=(",", ":"), ensure_ascii=False,
    )


# --- Hành động gated (gửi THẬT) ----------------------------------------------------

def register_owner(private_key=None, did: str = None, *, post_fn=None, log=print) -> dict:
    """Đăng ký owner (idempotency do CALLER lo qua state — MINT một-lần cũng được referee ép).
    Gated CLOSE_CALL_ENABLED. Trả outcome rõ ràng, KHÔNG raise runtime.

    outcome: skipped_disabled | skipped_unconfigured | skipped_locked | registered | post_failed
    """
    if not enabled():
        return {"outcome": "skipped_disabled", "reason": "CLOSE_CALL_ENABLED tắt (mặc định)"}
    sender = post_fn or post_message
    if sender is None or not (did and private_key):
        return {"outcome": "skipped_unconfigured",
                "reason": "thiếu post_fn/agent_cron hoặc thiếu did/private_key -> không gửi"}
    if _past_lock():
        return {"outcome": "skipped_locked", "reason": f"đã qua khoá {lock_iso()}"}
    text = build_owner(did)
    rm = room()
    try:
        ok = sender(private_key, did, text, rm)
    except Exception as e:                       # pragma: no cover - phòng thủ, không làm sập vòng
        return {"outcome": "post_failed", "reason": f"post ném lỗi: {str(e)[:120]}"}
    if ok:
        log(f"[close-call] đăng ký owner {did[:24]}… vào r/{rm} ({season()})")
        return {"outcome": "registered", "room": rm, "text": text}
    return {"outcome": "post_failed", "reason": "post trả False (đường ghi lỗi?)"}


# --- Chạy thử offline (KHÔNG gửi) --------------------------------------------------

if __name__ == "__main__":                       # pragma: no cover
    demo_did = "did:key:z6MkSAMPLEownerDIDforOfflineDemoOnly0000000000"
    print("season      :", season())
    print("room        :", room())
    print("referee_did :", referee_did())
    print("lock_iso    :", lock_iso(), "| past_lock:", _past_lock())
    print("enabled     :", enabled())
    print("owner-msg   :", build_owner(demo_did))
