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
from decimal import Decimal, InvalidOperation

# Soft-import hạ tầng Technocore của agent_cron (như sonnet_voter). Thiếu -> vẫn build/ký message
# được để test; chỉ phần gửi/đọc mạng cần các hàm này.
try:
    from agent_cron import post_message, sign_message, fetch_messages
except Exception:  # pragma: no cover
    post_message = None
    sign_message = None
    fetch_messages = None


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


# ======================================================================================
# GIAO DỊCH (game) — GATED riêng, THEO Ý ĐỊNH TƯỜNG MINH, dry-run mặc định.
#
# SỰ THẬT KINH TẾ: điểm = POLF tại giá chốt S (04/10) − 10,000; zero-sum TRỪ phí. Clawback
# triệt tiêu mẹo giá lệch -> vào lệnh ≈ giá hiện tại, P&L = (S − entry) cho long / (entry − S)
# cho short, trừ phí 1%. KHÔNG có edge cơ học: không có "view" NVDA thì kỳ vọng ÂM. Vì vậy
# module này KHÔNG tự đoán hướng — chỉ thực thi lệnh do operator cấu hình tường minh.
#
# Convention trên close1 (quan sát thực tế; spec chỉ định nghĩa {"t":"trade"} đủ-2-chữ-ký):
#   maker chào : {"t":"offer","season":..,"terms":{...},"maker_sig":".."}
#   taker khớp : {"t":"trade","season":..,"terms":{...},"taker":"<did>","maker_sig":"..","taker_sig":".."}
# Chỉ {"t":"trade"} mới settle; {"t":"offer"} là quảng bá (referee bỏ qua).
# Ký: terms = JSON sorted-keys/compact; maker ký `<season>|terms|<terms>`; taker ký
#     `<season>|accept|<terms>|<taker did>`. Tái dùng sign_message (Ed25519 base64url-no-pad).
# ======================================================================================

DEFAULT_MAX_QTY = "5"            # trần an toàn qty mỗi lệnh (chống fat-finger)
LOCK_SWEEP = 2556                # sweep cuối (contest.json) — until không vượt quá
LIMIT_WINDOW = Decimal("0.05")   # ±5% quanh reference (luật §Limits)
MIN_QTY = Decimal("0.1")
PRICE_ROOM = "d-close1-price"    # referee-only: reference + limits + sweep n hiện tại
_TRUTHY = ("1", "true", "on", "yes")


def trade_enabled() -> bool:
    """Gate RIÊNG cho giao dịch (mặc định TẮT) — tách khỏi đăng ký (CLOSE_CALL_ENABLED)."""
    return os.environ.get("CLOSE_CALL_TRADE_ENABLED", "").strip().lower() in _TRUTHY


def trade_dry_run() -> bool:
    """Dry-run MẶC ĐỊNH BẬT: dựng+ký+log NHƯNG KHÔNG gửi. Đặt CLOSE_CALL_TRADE_DRY_RUN=off
    để phát THẬT. An toàn: quên cấu hình -> không bao giờ tự đặt lệnh thật."""
    return os.environ.get("CLOSE_CALL_TRADE_DRY_RUN", "on").strip().lower() not in ("0", "false", "off", "no")


def max_qty() -> Decimal:
    raw = os.environ.get("CLOSE_CALL_MAX_QTY", "").strip() or DEFAULT_MAX_QTY
    try:
        v = Decimal(raw)
        return v if v > 0 else Decimal(DEFAULT_MAX_QTY)
    except InvalidOperation:
        return Decimal(DEFAULT_MAX_QTY)


def order_spec() -> str:
    """Lệnh MAKER tường minh: 'side,qty,px,until' (px='market' hoặc số; until='+N' hoặc số).
    Rỗng -> không chào lệnh nào."""
    return os.environ.get("CLOSE_CALL_ORDER", "").strip()


def accept_id() -> str:
    """TAKER: id của offer muốn khớp (đọc từ close1). Rỗng -> không khớp gì (không cược bừa)."""
    return os.environ.get("CLOSE_CALL_ACCEPT", "").strip()


# --- Chuẩn hoá số/terms (thuần, test được) -----------------------------------------

def _dec2(x) -> str:
    """Chuỗi thập phân đúng bước 0.01 (2 chữ số). Ném ValueError nếu không parse được."""
    try:
        d = Decimal(str(x))
    except InvalidOperation:
        raise ValueError(f"số không hợp lệ: {x!r}")
    q = d.quantize(Decimal("0.01"))
    if q != d:
        raise ValueError(f"{x!r} không đúng bước 0.01")
    return str(q)


def canonical_terms(terms: dict) -> str:
    """terms -> JSON 1 dòng, SORTED keys, không khoảng trắng (đúng lane ký của luật)."""
    return json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_terms(maker: str, side: str, qty, px, until: int, taker: str = "any", tid: str = None) -> dict:
    """Dựng + VALIDATE terms. side∈{buy,sell}; qty≥0.1 bước 0.01; px 2dp; until int≤LOCK.
    tid: id lệnh (mặc định sinh ngẫu nhiên hex). KHÔNG ký ở đây."""
    if side not in ("buy", "sell"):
        raise ValueError("side phải là 'buy' hoặc 'sell'")
    if not (maker and str(maker).startswith("did:key:")):
        raise ValueError("maker did không hợp lệ")
    q = Decimal(_dec2(qty))
    if q < MIN_QTY:
        raise ValueError(f"qty tối thiểu {MIN_QTY}")
    p = _dec2(px)
    if type(until) is not int or until <= 0 or until > LOCK_SWEEP:
        raise ValueError(f"until phải là int trong (0, {LOCK_SWEEP}]")
    if not (taker == "any" or str(taker).startswith("did:key:")):
        raise ValueError("taker phải là 'any' hoặc did:key")
    tid = (tid or os.urandom(8).hex())
    return {"id": tid, "maker": maker, "px": p, "qty": str(q),
            "side": side, "taker": taker, "until": until}


def terms_sign_str(terms_json: str) -> str:
    return f"{season()}|terms|{terms_json}"


def accept_sign_str(terms_json: str, taker_did: str) -> str:
    return f"{season()}|accept|{terms_json}|{taker_did}"


def within_limits(px, low, high) -> bool:
    """px nằm trong [low, high] (biên ±5% referee post)."""
    try:
        p, lo, hi = Decimal(str(px)), Decimal(str(low)), Decimal(str(high))
    except InvalidOperation:
        return False
    return lo <= p <= hi


# --- Đọc referee price room --------------------------------------------------------

def read_reference() -> dict:
    """Đọc post mới nhất của d-close1-price -> {ok, ref_px, low, high, n_next}. Dùng cho
    giá 'market' + kiểm biên. Thiếu fetch_messages/không đọc được -> ok False."""
    if fetch_messages is None:
        return {"ok": False, "reason": "chưa có fetch_messages"}
    data = fetch_messages(since=0, room=PRICE_ROOM)
    if not data or not data.get("messages"):
        return {"ok": False, "reason": "không đọc được d-close1-price"}
    for m in reversed(data["messages"]):            # mới nhất trước
        try:
            o = json.loads(m.get("text", ""))
        except (ValueError, TypeError):
            continue
        ref = o.get("ref")
        ref_px = ref.get("px") if isinstance(ref, dict) else o.get("price")
        limits = o.get("limits")
        if ref_px is not None and isinstance(limits, list) and len(limits) == 2:
            return {"ok": True, "ref_px": ref_px, "low": limits[0], "high": limits[1],
                    "n_next": o.get("for") or (o.get("n", 0) + 1)}
    return {"ok": False, "reason": "post price không có ref/limits"}


# --- Dựng message (thuần, test được; KHÔNG gửi) ------------------------------------

def sign_terms(private_key, terms: dict) -> tuple:
    """(terms_json, maker_sig). Cần sign_message (agent_cron)."""
    if sign_message is None:
        raise RuntimeError("thiếu sign_message (agent_cron chưa import được)")
    tj = canonical_terms(terms)
    return tj, sign_message(private_key, terms_sign_str(tj))


def build_offer_msg(terms: dict, maker_sig: str) -> str:
    """{"t":"offer",...} — quảng bá của maker (đúng convention close1)."""
    return json.dumps({"t": "offer", "season": season(), "terms": terms, "maker_sig": maker_sig},
                      separators=(",", ":"), ensure_ascii=False)


def build_trade_msg(terms: dict, maker_sig: str, taker_did: str, taker_sig: str) -> str:
    """{"t":"trade",...} — bản ĐỦ 2 chữ ký, cái DUY NHẤT referee settle."""
    return json.dumps({"t": "trade", "season": season(), "terms": terms, "taker": taker_did,
                       "maker_sig": maker_sig, "taker_sig": taker_sig},
                      separators=(",", ":"), ensure_ascii=False)


def parse_offer(text: str) -> dict:
    """Rút offer hợp lệ từ 1 message text. None nếu không phải offer season này."""
    try:
        o = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not (isinstance(o, dict) and o.get("t") == "offer" and o.get("season") == season()):
        return None
    terms, sig = o.get("terms"), o.get("maker_sig")
    if not (isinstance(terms, dict) and isinstance(sig, str) and terms.get("maker")):
        return None
    return {"terms": terms, "maker_sig": sig}


# --- Phân tích lệnh MAKER tường minh -----------------------------------------------

def parse_order(spec: str, *, ref_px=None, n_next: int = None) -> dict:
    """'side,qty,px,until' -> dict tham số cho build_terms. px='market'->ref_px; until='+N'->
    n_next+N. Ném ValueError nếu thiếu ref khi cần. KHÔNG tự chọn hướng — side là bắt buộc."""
    parts = [p.strip() for p in (spec or "").split(",")]
    if len(parts) != 4:
        raise ValueError("CLOSE_CALL_ORDER phải dạng 'side,qty,px,until'")
    side, qty, px_raw, until_raw = parts
    if px_raw.lower() == "market":
        if ref_px is None:
            raise ValueError("px='market' nhưng chưa đọc được reference")
        px = ref_px
    else:
        px = px_raw
    if until_raw.startswith("+"):
        if n_next is None:
            raise ValueError("until='+N' nhưng chưa biết sweep hiện tại")
        until = int(n_next) + int(until_raw[1:])
    else:
        until = int(until_raw)
    return {"side": side, "qty": qty, "px": px, "until": min(until, LOCK_SWEEP)}


# --- Hành động gated (dry-run mặc định) --------------------------------------------

def post_offer(private_key=None, did: str = None, *, post_fn=None, state_mark=None, log=print) -> dict:
    """Chào 1 lệnh MAKER theo CLOSE_CALL_ORDER. Gated CLOSE_CALL_TRADE_ENABLED; dry-run mặc định.
    outcome: skipped_disabled|skipped_unconfigured|skipped_no_order|skipped_locked|
             invalid_order|out_of_limits|dry_run|posted|post_failed"""
    if not trade_enabled():
        return {"outcome": "skipped_disabled", "reason": "CLOSE_CALL_TRADE_ENABLED tắt"}
    sender = post_fn or post_message
    if sender is None or not (did and private_key):
        return {"outcome": "skipped_unconfigured", "reason": "thiếu post_fn/did/private_key"}
    if _past_lock():
        return {"outcome": "skipped_locked", "reason": f"đã qua khoá {lock_iso()}"}
    spec = order_spec()
    if not spec:
        return {"outcome": "skipped_no_order", "reason": "CLOSE_CALL_ORDER rỗng"}
    ref = read_reference()
    try:
        od = parse_order(spec, ref_px=ref.get("ref_px"), n_next=ref.get("n_next"))
        if Decimal(_dec2(od["qty"])) > max_qty():
            return {"outcome": "invalid_order", "reason": f"qty vượt trần {max_qty()}"}
        terms = build_terms(did, od["side"], od["qty"], od["px"], od["until"])
    except ValueError as e:
        return {"outcome": "invalid_order", "reason": str(e)}
    if ref.get("ok") and not within_limits(terms["px"], ref["low"], ref["high"]):
        return {"outcome": "out_of_limits",
                "reason": f"px {terms['px']} ngoài [{ref['low']},{ref['high']}]"}
    terms_json, maker_sig = sign_terms(private_key, terms)
    msg = build_offer_msg(terms, maker_sig)
    if trade_dry_run():
        log(f"[close-call][DRY] offer {od['side']} {terms['qty']}@{terms['px']} id={terms['id']} until={terms['until']}")
        return {"outcome": "dry_run", "message": msg, "id": terms["id"], "terms": terms}
    ok = sender(private_key, did, msg, room())
    if ok:
        if state_mark:
            state_mark(terms["id"])
        log(f"[close-call] offer {od['side']} {terms['qty']}@{terms['px']} id={terms['id']} -> r/{room()}")
        return {"outcome": "posted", "id": terms["id"], "terms": terms}
    return {"outcome": "post_failed", "reason": "post trả False"}


def take_offer(private_key=None, did: str = None, offer_id: str = None, *, post_fn=None,
               fetch_fn=None, log=print) -> dict:
    """Khớp 1 offer CỤ THỂ (theo id) trên close1 với vai TAKER. Gated + dry-run mặc định.
    KHÔNG tự chọn offer — phải chỉ định id (CLOSE_CALL_ACCEPT). Không khớp offer của CHÍNH mình.
    outcome: skipped_disabled|skipped_unconfigured|skipped_no_id|skipped_locked|not_found|
             self_offer|out_of_limits|dry_run|posted|post_failed"""
    if not trade_enabled():
        return {"outcome": "skipped_disabled", "reason": "CLOSE_CALL_TRADE_ENABLED tắt"}
    sender = post_fn or post_message
    fetcher = fetch_fn or fetch_messages
    if sender is None or fetcher is None or not (did and private_key):
        return {"outcome": "skipped_unconfigured", "reason": "thiếu post_fn/fetch/did/private_key"}
    if _past_lock():
        return {"outcome": "skipped_locked", "reason": f"đã qua khoá {lock_iso()}"}
    offer_id = (offer_id or accept_id()).strip()
    if not offer_id:
        return {"outcome": "skipped_no_id", "reason": "CLOSE_CALL_ACCEPT rỗng"}
    data = fetcher(since=0, room=room())
    found = None
    for m in reversed((data or {}).get("messages", [])):
        off = parse_offer(m.get("text", ""))
        if off and off["terms"].get("id") == offer_id:
            found = off
            break
    if not found:
        return {"outcome": "not_found", "reason": f"không thấy offer id={offer_id} trong r/{room()}"}
    terms = found["terms"]
    if terms.get("maker") == did:
        return {"outcome": "self_offer", "reason": "không khớp offer của chính mình"}
    if terms.get("taker") not in ("any", did):
        return {"outcome": "not_found", "reason": "offer chỉ định taker khác"}
    # kiểm biên nếu đọc được reference (referee vẫn void nếu sai -> đây chỉ chặn sớm)
    ref = read_reference()
    if ref.get("ok") and not within_limits(terms.get("px"), ref["low"], ref["high"]):
        return {"outcome": "out_of_limits", "reason": f"px {terms.get('px')} ngoài biên hiện tại"}
    terms_json = canonical_terms(terms)
    taker_sig = sign_message(private_key, accept_sign_str(terms_json, did))
    msg = build_trade_msg(terms, found["maker_sig"], did, taker_sig)
    if trade_dry_run():
        log(f"[close-call][DRY] take offer id={offer_id} ({terms.get('side')} {terms.get('qty')}@{terms.get('px')})")
        return {"outcome": "dry_run", "message": msg, "id": offer_id}
    ok = sender(private_key, did, msg, room())
    if ok:
        log(f"[close-call] took offer id={offer_id} -> r/{room()}")
        return {"outcome": "posted", "id": offer_id}
    return {"outcome": "post_failed", "reason": "post trả False"}


# --- Chạy thử offline (KHÔNG gửi) --------------------------------------------------

if __name__ == "__main__":                       # pragma: no cover
    demo_did = "did:key:z6MkSAMPLEownerDIDforOfflineDemoOnly0000000000"
    print("season       :", season())
    print("room         :", room())
    print("referee_did  :", referee_did())
    print("lock_iso     :", lock_iso(), "| past_lock:", _past_lock())
    print("enabled      :", enabled())
    print("owner-msg    :", build_owner(demo_did))
    print("trade_enabled:", trade_enabled(), "| dry_run:", trade_dry_run(), "| max_qty:", max_qty())
    t = build_terms(demo_did, "buy", "1", "224.50", 100)
    print("terms canon  :", canonical_terms(t))
    print("terms sign   :", terms_sign_str(canonical_terms(t)))
