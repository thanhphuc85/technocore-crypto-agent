# SPDX-License-Identifier: MIT
"""tclk/1 (Technocore Lock Protocol) — PAYEE side, an toàn & DRY-RUN mặc định.

`tclk/1` (spec: github.com/flop-labs/tclk) cho 2 agent lạ làm deal HTLC/PTLC bằng tin nhắn KÝ
trong room công khai: offer → accept → lock → reveal (hoặc refund). Coordination nằm ở room;
tiền nằm trên "rail" mà offer nêu. Technocore KHÔNG giữ key, KHÔNG settle.

Module này CHỈ làm vai PAYEE ở mức AN TOÀN NHẤT:
  - PHÁT HIỆN offer hợp lệ trên `tclk-offers` (payer trả tiền, hash-lock, rail mình nhận, còn hạn),
  - DỰNG frame `accept` đúng chuẩn (mint preimage, statement=sha256(preimage), tính contract id),
  - DRY-RUN: chỉ LOG "would accept", KHÔNG post, KHÔNG lộ secret.
  - Live (khi tắt dry-run): CHỈ post `accept`. TUYỆT ĐỐI KHÔNG tự `lock`/`reveal` — reveal là hành
    động CLAIM tiền, luôn để con người quyết. (Alpha/testnet, chưa audit — xem cảnh báo của spec.)

Thuần & kiểm thử được: canonical_json / to_ascii / offer_id / contract_id / make_accept /
select_offers là hàm THUẦN (không mạng). `run_tclk_payee` nhận fetch_fn/post_fn tiêm vào — cùng
kiểu như flop_kibble — nên chữ ký/HTTP đi qua agent_cron.

BYTE-EXACT: canonical_json + to_ascii mô phỏng nguyên văn `src/frames.ts` của reference; đã kiểm
ngược với 50 offer THẬT trên tclk-offers (offer_id tính lại khớp id do agent JS tạo).
"""
from __future__ import annotations

import json
import hashlib
import os

TCLK_PREFIX = "tclk1 "
TCLK_DOMAIN = "FLOP::tclk::v1"
MAX_FRAME_CHARS = 4096


# ── Canonical encoding (khớp src/frames.ts) ──────────────────────────────────
def canonical_json(value) -> str:
    """JSON tất định: key sắp xếp, separators gọn `,`/`:`, bỏ key None (=undefined của JS).
    KHÔNG escape non-ASCII ở bước này (giống JSON.stringify) — việc escape do to_ascii lo."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return json.dumps(value)
    if isinstance(value, float):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(x) for x in value) + "]"
    if isinstance(value, dict):
        keys = [k for k in sorted(value.keys()) if value[k] is not None]
        return "{" + ",".join(
            json.dumps(k, ensure_ascii=False) + ":" + canonical_json(value[k]) for k in keys
        ) + "}"
    raise TypeError(f"tclk: unsupported value {type(value)!r}")


def to_ascii(s: str) -> str:
    """Escape mọi ký tự non-ASCII thành \\uXXXX (theo UTF-16 code unit, khớp charCodeAt của JS)
    -> bytes lưu = bytes đã ký = bytes đã băm id."""
    out = []
    for ch in s:
        o = ord(ch)
        if o < 0x80:
            out.append(ch)
        elif o <= 0xFFFF:
            out.append("\\u{:04x}".format(o))
        else:                                            # ngoài BMP -> cặp surrogate
            o -= 0x10000
            out.append("\\u{:04x}\\u{:04x}".format(0xD800 + (o >> 10), 0xDC00 + (o & 0x3FF)))
    return "".join(out)


def _domain_hash(tag: str, payload: str) -> str:
    """0x + sha256("FLOP::tclk::v1|<tag>|<payload đã to_ascii>")."""
    data = (TCLK_DOMAIN + "|" + tag + "|" + to_ascii(payload)).encode("utf-8")
    return "0x" + hashlib.sha256(data).hexdigest()


def offer_id(offer_fields: dict) -> str:
    """id của offer = domain-hash trên canonical offer (KHÔNG gồm 'id')."""
    fields = {k: v for k, v in offer_fields.items() if k != "id"}
    return _domain_hash("offer", canonical_json(fields))


def contract_id(offer_with_id: dict, accept_core: dict) -> str:
    """contract id = domain-hash trên canonical {offer, accept}. Buộc chặt offer đầy đủ (có id)
    + phần lõi của accept -> mỗi bên tính lại; lệch là reject."""
    return _domain_hash("contract", canonical_json({"offer": offer_with_id, "accept": accept_core}))


# ── Hash lock ────────────────────────────────────────────────────────────────
def _sha256_hex(b: bytes) -> str:
    return "0x" + hashlib.sha256(b).hexdigest()


def generate_hash_lock():
    """Mint preimage 32 byte ngẫu nhiên -> (preimage_hex, statement=sha256(preimage))."""
    preimage = os.urandom(32)
    return "0x" + preimage.hex(), _sha256_hex(preimage)


def verify_hash_preimage(statement: str, preimage_hex: str) -> bool:
    """True nếu sha256(preimage) == statement (fail-closed)."""
    try:
        p = bytes.fromhex(preimage_hex[2:] if preimage_hex.startswith("0x") else preimage_hex)
        if len(p) != 32:
            return False
        return _sha256_hex(p) == statement.lower()
    except Exception:
        return False


# ── Frame parse / build ──────────────────────────────────────────────────────
_OFFER_REQUIRED = ("type", "from", "role", "amount", "asset", "lock", "rails",
                   "claimByMs", "refundAfterMs", "expiresMs", "nonce", "id")


def parse_frame(text: str):
    """'tclk1 {json}' -> dict; None nếu không phải frame tclk hợp lệ (fail-closed)."""
    if not isinstance(text, str) or not text.startswith(TCLK_PREFIX):
        return None
    try:
        obj = json.loads(text[len(TCLK_PREFIX):])
        return obj if isinstance(obj, dict) and obj.get("type") else None
    except Exception:
        return None


def _valid_offer_shape(o: dict) -> bool:
    if o.get("type") != "offer":
        return False
    if any(o.get(k) is None for k in _OFFER_REQUIRED):
        return False
    return isinstance(o.get("rails"), list) and o["lock"] in ("hash", "point")


def validate_deadlines(offer: dict, now_ms: int, min_claim_window_ms: int, min_refund_gap_ms: int) -> bool:
    """Cửa sổ claim (now→claimByMs) đủ để làm việc + reveal, và gap claim→refund đủ để rail thấy
    reveal trước khi payer được refund. Margin là khẩu vị rủi ro của caller (khớp validateDeadlines)."""
    if min_claim_window_ms < 1 or min_refund_gap_ms < 1:
        return False
    return (offer["claimByMs"] - now_ms >= min_claim_window_ms
            and offer["refundAfterMs"] - offer["claimByMs"] >= min_refund_gap_ms)


def make_accept(offer: dict, my_did: str, preimage_hex: str = None):
    """Dựng frame `accept` cho 1 offer HASH-LOCK. Trả (accept_frame, preimage_hex, statement).
    Nếu preimage_hex None -> mint mới. KHÔNG hỗ trợ point-lock (cần adaptor sig chưa audit)."""
    if offer.get("lock") != "hash":
        raise ValueError("tclk: chỉ hỗ trợ hash-lock (point-lock cần adaptor sig, không dùng)")
    if preimage_hex is None:
        preimage_hex, statement = generate_hash_lock()
    else:
        statement = _sha256_hex(bytes.fromhex(preimage_hex[2:]))
    accept_core = {
        "from": my_did,
        "ref": offer["id"],
        "statement": statement,
        "nonce": os.urandom(8).hex(),
    }
    cid = contract_id(offer, accept_core)
    frame = {**accept_core, "contract": cid, "type": "accept"}
    return frame, preimage_hex, statement


def encode_frame(frame: dict) -> str:
    """dict -> dòng dây 'tclk1 ' + canonical + to_ascii (bytes = bytes sẽ ký)."""
    line = TCLK_PREFIX + to_ascii(canonical_json(frame))
    if len(line) > MAX_FRAME_CHARS:
        raise ValueError(f"tclk: frame quá dài {len(line)} > {MAX_FRAME_CHARS}")
    return line


# ── Chọn offer đáng nhận (vai PAYEE) ─────────────────────────────────────────
def select_offers(messages, accepted_set, my_did, now_ms, allow_rails,
                  min_claim_window_ms, min_refund_gap_ms, max_n):
    """Lọc offer để NHẬN (payee). Điều kiện, tất cả phải đúng:
      - frame offer hợp lệ, KHÔNG do mình đăng, chưa nhận (id chưa trong accepted_set),
      - role='payer' (bên gửi TRẢ TIỀN -> mình làm việc), lock='hash',
      - có rail giao nhau với allow_rails, còn hạn (expiresMs>now), deadline đủ margin,
      - offer_id tính lại KHỚP id trong frame (chống offer bịa id).
    Hàm THUẦN — không mạng, không side-effect. Trả list (tối đa max_n) kèm seq để tiến cursor."""
    allow = {r.strip().lower() for r in (allow_rails or []) if r and r.strip()}
    picked = []
    for m in sorted(messages, key=lambda x: x.get("seq", 0)):
        if len(picked) >= max_n:
            break
        o = parse_frame(m.get("text", ""))
        if not o or not _valid_offer_shape(o):
            continue
        if o.get("from") == my_did or o.get("role") != "payer" or o.get("lock") != "hash":
            continue
        if o["id"] in accepted_set:
            continue
        if not (allow & {str(r).lower() for r in o["rails"]}):
            continue
        if o["expiresMs"] <= now_ms:
            continue
        if not validate_deadlines(o, now_ms, min_claim_window_ms, min_refund_gap_ms):
            continue
        try:                                             # chống offer gắn id bịa
            if offer_id(o) != o["id"]:
                continue
        except Exception:
            continue
        picked.append((m.get("seq", 0), o))
    return picked


# Dấu hiệu job đòi MEDIA (video/ảnh) — agent chỉ-text KHÔNG làm được. Từ khoá mạnh để tránh
# chặn nhầm job text (vd "post or article" không dính; "short video"/"9:16"/"image" thì dính).
_MEDIA_HINTS = (
    "video", "mp4", "mov", "webm", "reel", "9:16", "4:5", "16:9", "h264", "aac",
    "1080", "720", "image", "photo", "picture", "png", "jpg", "jpeg", "gif",
    "illustration", "thumbnail", "logo", "banner", "poster", "animation", "render",
    "tiktok", "youtube", "instagram",
)


def is_media_job(spec: str) -> bool:
    """True nếu spec job đòi media (video/ảnh) -> bỏ NGAY ở bước accept (không đợi SKIP lúc làm).
    None/rỗng -> False (không rõ thì không chặn; SKIP lúc hoàn tất vẫn là lưới an toàn cuối)."""
    if not spec:
        return False
    low = spec.lower()
    return any(h in low for h in _MEDIA_HINTS)


# ── Worker (DRY-RUN mặc định) ────────────────────────────────────────────────
def run_tclk_payee(fetch_fn, post_fn, state, *, my_did, allow_rails,
                   min_claim_window_ms, min_refund_gap_ms, max_per_run=2,
                   dry_run=True, now_ms=None, job_spec_fn=None, log=print):
    """Quét tclk-offers, chọn offer đáng nhận, dựng `accept`.
      fetch_fn(since)   -> data JSON như /r/tclk-offers?format=json
      post_fn(text)     -> bool (chỉ gọi khi dry_run=False, và CHỈ để post `accept`)
      job_spec_fn(ctx)  -> str|None đọc job-spec ở job.context; job đòi MEDIA -> bỏ (chỉ nhận text)
      state             -> dùng 'tclk_cursor' (con trỏ) + 'tclk_accepted' (id đã nhận)
    AN TOÀN: KHÔNG bao giờ tự `lock`/`reveal`. reveal = CLAIM tiền -> luôn để người quyết.
    Trả {scanned, accepted:[ids], skipped, dry_run, cursor}."""
    import time
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    last = state.get("tclk_cursor")
    data = fetch_fn(last)
    if not data or "messages" not in data:
        log("[tclk] không lấy được offer, bỏ qua vòng này.")
        return {"scanned": 0, "accepted": [], "skipped": 0, "dry_run": dry_run,
                "cursor": last}
    messages = data.get("messages", [])
    accepted_set = set(state.get("tclk_accepted", []))
    picks = select_offers(messages, accepted_set, my_did, now_ms, allow_rails,
                          min_claim_window_ms, min_refund_gap_ms, max_per_run)

    accepted, skipped = [], 0
    cursor = last or 0
    for seq, offer in picks:
        # BỘ LỌC CHỈ-NHẬN-TEXT: nếu job có spec đòi media (video/ảnh) -> bỏ ngay, không accept.
        job = offer.get("job") or {}
        ctx = job.get("context")
        if job_spec_fn and ctx:
            try:
                spec = job_spec_fn(ctx)
            except Exception:
                spec = None
            if is_media_job(spec):
                skipped += 1
                cursor = max(cursor, seq)
                log(f"[tclk] skip {offer.get('id','?')[:14]} — job cần media (không phải text), bỏ")
                continue
        try:
            frame, preimage, statement = make_accept(offer, my_did)
            line = encode_frame(frame)
        except Exception as e:
            skipped += 1
            log(f"[tclk] skip offer {offer.get('id','?')[:14]} — dựng accept lỗi: {str(e)[:80]}")
            continue
        oid = offer["id"]
        amt = f"{offer.get('amount')} {offer.get('asset')}"
        rails = ",".join(offer.get("rails", []))
        if dry_run:
            # DRY: chỉ LOG. KHÔNG post, KHÔNG lộ preimage, KHÔNG ghi vào accepted (để live còn làm).
            log(f"[tclk:DRY] would ACCEPT {oid[:14]} | {amt} | rail={rails} | "
                f"contract={frame['contract'][:14]} | statement={statement[:14]}… (secret giữ nội bộ)")
            accepted.append(oid)
            cursor = max(cursor, seq)
            continue
        # LIVE: chỉ POST `accept`. KHÔNG lock/reveal. Lưu secret NỘI BỘ để bước reveal (do người) dùng.
        if post_fn(line):
            accepted.append(oid)
            accepted_set.add(oid)
            state.setdefault("tclk_secrets", {})[frame["contract"]] = {
                "offer_id": oid, "preimage": preimage, "statement": statement,
                "payer_did": offer.get("from"), "job": offer.get("job"),
                "amount": offer.get("amount"), "asset": offer.get("asset"),
                "rails": offer.get("rails"), "claimByMs": offer.get("claimByMs"),
                "refundAfterMs": offer.get("refundAfterMs"), "accepted_ms": now_ms,
            }
            log(f"[tclk] ACCEPT posted {oid[:14]} | contract={frame['contract'][:14]} "
                "(đã lưu secret nội bộ; reveal/claim để NGƯỜI quyết)")
            cursor = max(cursor, seq)
        else:
            log(f"[tclk] accept post fail {oid[:14]} — dừng (đường ghi lỗi?)")
            break                                        # ghi lỗi -> dừng, không phí thêm

    # Cursor tiến tới offer đã xét gần nhất; nếu chưa xét gì thì bám last_seq của batch.
    new_last = data.get("last_seq", last)
    if not picks:
        cursor = max(cursor, new_last or 0)
    if not dry_run:
        state["tclk_accepted"] = list(accepted_set)[-200:]   # chống phình
    state["tclk_cursor"] = cursor
    return {"scanned": len(messages), "accepted": accepted, "skipped": skipped,
            "dry_run": dry_run, "cursor": cursor}


# ═══════════════════════════════════════════════════════════════════════════════
# VÒNG HOÀN TẤT (completion) — sau ACCEPT: chờ payer LOCK -> verify rail -> làm việc -> REVEAL.
# Chỉ chạy trên deal ĐÃ accept live (có secret nội bộ). AN TOÀN: chỉ reveal khi (1) có lock
# frame của payer, (2) rail XÁC NHẬN lock khớp statement/refundAfterMs, (3) còn cửa sổ claim,
# (4) đã LÀM ĐƯỢC việc (có deliverable). Reveal công khai + không đảo được -> guard bắt buộc.
# Derivation khớp src/technocore.ts + src/paper-rail.ts.
# ═══════════════════════════════════════════════════════════════════════════════
import re as _re

_CONTRACT_RE = _re.compile(r"^0x[0-9a-f]{64}$")
_STMT_RE = _re.compile(r"^0x[0-9a-f]{64,66}$")
_SECRET_RE = _re.compile(r"^0x[0-9a-f]{64}$")
PAPER_RECORD_PREFIX = "tclkpaper1"


def _require_contract(contract: str) -> str:
    if not _CONTRACT_RE.match(contract or ""):
        raise ValueError("tclk: contract id sai định dạng: " + str(contract))
    return contract


def deal_room(contract: str) -> str:
    """Room deal suy ra từ contract: mb-p-tclk-<16 hex đầu>. 2 bên tự tính cùng tên (không riêng tư)."""
    return "mb-p-tclk-" + _require_contract(contract)[2:18]


def state_note(contract: str) -> dict:
    """Con trỏ trạng thái CAS: ns tclk-<2 hex>, key <14 hex>. Chỉ là hint, không phải thẩm quyền."""
    c = _require_contract(contract)
    return {"ns": "tclk-" + c[2:4], "key": c[4:18]}


def paper_note(contract: str) -> dict:
    """Nơi rail 'paper' ghi escrow: ns tclk-paper-<2 hex>, key <14 hex>."""
    c = _require_contract(contract)
    return {"ns": "tclk-paper-" + c[2:4], "key": c[4:18]}


def decode_paper_record(value: str):
    """Parse 'tclkpaper1 <status> <lock> <statement> <refundAfterMs>[ secret]'. None nếu sai
    (namespace world-writable -> mọi read là input lạ, fail-closed, không throw)."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(" ")
    if len(parts) < 5 or len(parts) > 6:
        return None
    prefix, status, lock, statement, refund = parts[:5]
    secret = parts[5] if len(parts) == 6 else None
    if prefix != PAPER_RECORD_PREFIX or status not in ("locked", "claimed", "refunded"):
        return None
    if lock not in ("hash", "point") or not _STMT_RE.match(statement):
        return None
    try:
        refund_after_ms = int(refund)
    except (ValueError, TypeError):
        return None
    if refund_after_ms <= 0:
        return None
    if secret is not None and not _SECRET_RE.match(secret):
        return None
    if (status == "claimed") != (secret is not None):
        return None
    rec = {"status": status, "lock": lock, "statement": statement, "refundAfterMs": refund_after_ms}
    if secret is not None:
        rec["secret"] = secret
    return rec


def verify_paper_lock(record, lock: str, statement: str, refund_after_ms: int) -> bool:
    """CỔNG AN TOÀN: True nếu rail xác nhận đang LOCKED đúng lock/statement/refundAfterMs của mình
    (khớp PaperRail.verifyLock). Trước khi làm việc + reveal phải qua cổng này."""
    return (isinstance(record, dict)
            and record.get("status") == "locked"
            and record.get("lock") == lock
            and record.get("statement") == statement
            and record.get("refundAfterMs") == refund_after_ms)


def make_reveal(contract: str, secret_hex: str, my_did: str) -> dict:
    """Frame `reveal` — công bố secret = CLAIM. Chỉ dựng sau khi qua mọi guard."""
    return {"type": "reveal", "from": my_did, "contract": _require_contract(contract),
            "secret": secret_hex}


def find_payer_lock(messages, contract: str, payer_did: str):
    """Tìm frame `lock` của ĐÚNG payer cho ĐÚNG contract trong deal room. None nếu chưa có."""
    for m in messages or []:
        f = parse_frame(m.get("text", ""))
        if (f and f.get("type") == "lock" and f.get("contract") == contract
                and f.get("from") == payer_did and f.get("rail")):
            return f
    return None


def run_tclk_complete(read_room_fn, kv_get_fn, post_fn, do_work_fn, state, *, my_did,
                      now_ms=None, dry_run=True, max_per_run=2, log=print):
    """Hoàn tất deal đã accept: chờ payer lock -> verify rail -> làm việc -> reveal.
      read_room_fn(room)   -> data JSON /r/<room>
      kv_get_fn(ns, key)   -> str|None (đã strip banner)
      post_fn(room, text)  -> bool (deliverable + reveal; chỉ khi dry_run=False)
      do_work_fn(meta)     -> str|None (None = không làm được -> KHÔNG reveal)
    state: 'tclk_secrets' (đã accept) + 'tclk_completed' (đã reveal). Trả
    {revealed, waiting, expired, dry_run}."""
    import time
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    secrets = state.get("tclk_secrets", {})
    done = set(state.get("tclk_completed", []))
    revealed, waiting, expired = [], 0, 0
    for contract, meta in list(secrets.items()):
        if contract in done or len(revealed) >= max_per_run:
            continue
        if now_ms >= meta.get("claimByMs", 0):           # (1) còn cửa sổ claim an toàn
            expired += 1
            log("[tclk] " + contract[:14] + " quá cửa sổ claim -> bỏ (không reveal muộn)")
            continue
        try:
            room = deal_room(contract)
            data = read_room_fn(room)
            msgs = data.get("messages", []) if data else []
            lockf = find_payer_lock(msgs, contract, meta.get("payer_did"))
            if not lockf:                                # payer chưa lock -> chờ
                waiting += 1
                continue
            pn = paper_note(contract)                    # (2) rail phải XÁC NHẬN (không tin frame)
            rec = decode_paper_record(kv_get_fn(pn["ns"], pn["key"]) or "")
            if not verify_paper_lock(rec, "hash", meta.get("statement"), meta.get("refundAfterMs")):
                waiting += 1
                log("[tclk] " + contract[:14] + " có lock frame nhưng rail chưa xác nhận -> chờ")
                continue
            deliverable = do_work_fn(meta) if do_work_fn else None   # (3) làm THẬT; không được -> bỏ
            if not deliverable:
                log("[tclk] " + contract[:14] + " không làm được job -> bỏ (KHÔNG reveal)")
                continue
            reveal = make_reveal(contract, meta["preimage"], my_did)
            amt = str(meta.get("amount")) + " " + str(meta.get("asset"))
            if dry_run:
                log("[tclk:DRY] would DELIVER + REVEAL " + contract[:14] + " | " + amt
                    + " | deliver=" + repr(deliverable[:60]) + "… (secret giữ nội bộ)")
                revealed.append(contract)
                continue
            post_fn(room, "[" + my_did[:12] + " deliver] " + deliverable)   # LIVE
            if post_fn(room, encode_frame(reveal)):
                revealed.append(contract)
                done.add(contract)
                log("[tclk] REVEAL posted " + contract[:14] + " — deal HOÀN TẤT (claim " + amt + ")")
        except Exception as e:
            log("[tclk] complete lỗi " + contract[:14] + ": " + str(e)[:80])
            continue
    if not dry_run:
        state["tclk_completed"] = list(done)[-200:]
    return {"revealed": revealed, "waiting": waiting, "expired": expired, "dry_run": dry_run}
