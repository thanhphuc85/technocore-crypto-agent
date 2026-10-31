# SPDX-License-Identifier: MIT
"""flop_gateway — MUA COMPUTE qua gateway CHÍNH THỨC api.flop.network.

Vì sao có module này: FLOP công bố (flop.finance/official/, 2026-10-23) rằng compute được bán
qua "FLOP REST Gateway" tương thích OpenAI, KHÔNG phải mempool/PoUI/settle mà scaffold
`flop_session.py` đoán. Luồng thật (đọc từ api.flop.network/openapi.json):

  1. Ví FLOP = tài khoản Substrate, địa chỉ SS58 (prefix 42), khoá ed25519.
  2. GET  /enroll/wallet/challenge?address=<SS58>  -> {message, nonce, expires_at}
     POST /enroll/wallet {address, message, signature: 0x<hex ed25519>}
          -> {key (bearer, CHỈ hiện 1 lần), model, remaining_quota, expires_at}
  3. POST /chat/completions (Bearer) {model, messages, max_tokens, max_cost_flop}
          -> {choices, usage{prompt_tokens, completion_tokens, gflops_used, cost_flop}}
  4. GET  /keys/me (quota còn lại) · GET /keys/me/receipts (biên nhận của gateway:
     block_number, tx_hash, miner — không chứa prompt/output).

An toàn:
  · Bearer là bí mật: KHÔNG in log, KHÔNG ghi KV. Cất trong state.json đã MÃ HOÁ AES-GCM bằng
    khoá dẫn xuất (HKDF) từ khoá agent — cache Actions đọc được từ workflow PR thì vẫn chỉ
    thấy bản mã; không có seed (secret) thì không mở được.
  · Mỗi yêu cầu gửi `max_cost_flop` = trần chi do GATEWAY cưỡng chế, không chỉ code mình.
  · 402 Payment Required (x402: price/asset/hash_lock/destination) -> CHỈ ghi lại các trường
    và DỪNG. Agent không bao giờ tự trả on-chain theo hướng dẫn trong phản hồi.
  · Kiểm `network` ở /health: chi devnet KHÔNG bao giờ được ghi vào sổ testnet.

Thuần & test được: mọi HTTP qua `http(method, url, headers=, json=, timeout=)` tiêm vào.
"""
from __future__ import annotations

import base64
import hashlib
import math
import os
import random
import re
import time

KEY = "flop_gateway"
DEFAULT_URL = "https://api.flop.network/v1"
SS58_PREFIX = 42
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_VAULT_INFO = b"flop-gateway-bearer/v1"
SESSION_MEMO = "inference-session gateway"     # tiền tố khớp token_manager.SESSION_MEMO_PREFIX


class GatewayError(Exception):
    """Lỗi HTTP có ý nghĩa: status, mã lỗi máy-đọc (`error`), Retry-After (giây)."""

    def __init__(self, status: int, code: str = "", message: str = "", retry_after=None,
                 data: dict = None):
        super().__init__(f"{status} {code} {message}".strip())
        self.status, self.code, self.message = status, code, message
        self.retry_after, self.data = retry_after, data or {}


# --- cấu hình -------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_num(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return float(default)


def base_url() -> str:
    return (_env("FLOP_GATEWAY_URL") or DEFAULT_URL).rstrip("/")


def origin(url: str) -> str:
    m = re.match(r"(?i)^(https?://[^/]+)", url or "")
    return m.group(1) if m else ""


# --- ví: khoá ed25519 -> địa chỉ SS58 ---------------------------------------------------

def _b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def ss58_encode(pub32: bytes, prefix: int = SS58_PREFIX) -> str:
    if len(pub32) != 32 or not 0 <= prefix < 64:
        raise ValueError("ss58_encode: cần khoá công khai 32 byte và prefix 0..63")
    body = bytes([prefix]) + pub32
    return _b58encode(body + hashlib.blake2b(b"SS58PRE" + body, digest_size=64).digest()[:2])


def _raw_pub(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _raw_priv(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())


def wallet_key(agent_key):
    """Khoá ví FLOP. FLOP_WALLET_SEED_HEX (secret riêng, khuyên dùng) thắng; không có mà
    FLOP_WALLET_USE_DID=on thì dùng chính khoá DID của agent; không thì None (chưa cấu hình).
    Seed hỏng -> None (KHÔNG lặng lẽ lùi về khoá DID)."""
    raw = _env("FLOP_WALLET_SEED_HEX")
    if raw:
        try:
            seed = bytes.fromhex(raw)
        except ValueError:
            return None
        if len(seed) != 32:
            return None
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey.from_private_bytes(seed)
    if _env("FLOP_WALLET_USE_DID").lower() in ("1", "true", "on", "yes"):
        return agent_key
    return None


def wallet_address(key) -> str:
    return ss58_encode(_raw_pub(key))


# --- két bearer: AES-GCM, khoá HKDF từ khoá agent ---------------------------------------

def _vault_key(agent_key) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=_VAULT_INFO).derive(_raw_priv(agent_key))


def seal_bearer(agent_key, bearer: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(_vault_key(agent_key)).encrypt(nonce, bearer.encode("utf-8"), _VAULT_INFO)
    return {"n": base64.b64encode(nonce).decode(), "c": base64.b64encode(ct).decode()}


def open_bearer(agent_key, blob):
    """Bearer đã giải mã, hoặc None (thiếu/hỏng/khoá khác — vd bản fork, bản giả)."""
    if not isinstance(blob, dict) or agent_key is None:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        pt = AESGCM(_vault_key(agent_key)).decrypt(
            base64.b64decode(blob["n"]), base64.b64decode(blob["c"]), _VAULT_INFO)
        return pt.decode("utf-8")
    except Exception:
        return None


# --- HTTP -------------------------------------------------------------------------------

def _retry_after(headers) -> int:
    try:
        return max(0, int(float(str((headers or {}).get("Retry-After", "")).strip())))
    except (TypeError, ValueError):
        return 0


def _call(http, method: str, url: str, *, bearer: str = None, body: dict = None,
          timeout: float = 20, ok=(200, 201), extra_headers: dict = None) -> dict:
    headers = {"Accept": "application/json", **(extra_headers or {})}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    r = http(method, url, headers=headers, json=body, timeout=timeout)
    try:
        data = r.json() if getattr(r, "content", b"") else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {"data": data}
    if r.status_code not in ok:
        err = data.get("error")
        if isinstance(err, dict):          # dạng OpenAI: {"error": {"code", "message", "type"}}
            code, msg = err.get("code") or err.get("type") or "", err.get("message") or ""
        else:
            code, msg = err or "", data.get("message") or ""
        raise GatewayError(r.status_code, str(code)[:80], str(msg)[:200],
                           _retry_after(getattr(r, "headers", {})), data)
    return data


def network_detail(http, base: str):
    """(network, lỗi): `network.network` ở <origin>/health ("devnet"/"testnet"/...) và mô tả
    lỗi khi không đọc được (mã HTTP / loại ngoại lệ) — để log nói ĐƯỢC vì sao, không đoán."""
    try:
        d = _call(http, "GET", origin(base) + "/health", timeout=20)
    except GatewayError as e:
        return None, f"HTTP {e.status} {e.code}".strip()
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"
    net = d.get("network")
    if isinstance(net, dict):
        net = net.get("network")
    if isinstance(net, str) and net.strip():
        return net.strip().lower(), ""
    return None, f"/health không có trường network (keys: {sorted(d)[:8]})"


def network_matches(net: str, expect: str) -> bool:
    """`/health` có thể ghi "flop-testnet-1" thay vì đúng "testnet": khớp khi từ khoá nằm trong
    tên như một TỪ riêng (tách theo ký tự không phải chữ/số). "devnet" không bao giờ khớp
    "testnet" và ngược lại -> chi devnet không bao giờ bị ghi vào sổ testnet."""
    net, expect = str(net or "").strip().lower(), str(expect or "").strip().lower()
    if not net or not expect:
        return False
    return net == expect or expect in re.split(r"[^a-z0-9]+", net)


def network(http, base: str):
    """`network.network` ở <origin>/health hoặc None."""
    return network_detail(http, base)[0]


def catalog(http, base: str) -> dict:
    d = _call(http, "GET", base + "/models")
    return {m["id"]: m for m in d.get("data") or [] if isinstance(m, dict) and m.get("id")}


def enroll(http, base: str, wkey, purpose: str = None) -> dict:
    """Ký challenge bằng khoá ví -> {key, model, remaining_quota, expires_at}."""
    from urllib.parse import quote
    addr = wallet_address(wkey)
    url = f"{base}/enroll/wallet/challenge?address={quote(addr)}"
    if purpose:
        url += f"&purpose={quote(purpose)}"
    ch = _call(http, "GET", url)
    msg = ch.get("message")
    host = re.sub(r"(?i)^https?://", "", origin(base)).lower()
    first = msg.splitlines()[0].lower() if isinstance(msg, str) and msg else ""
    if not isinstance(msg, str) or addr not in msg or len(msg) > 2000 or host not in first:
        # Chỉ ký câu enroll của CHÍNH gateway này, nêu đúng địa chỉ ví mình — không ký câu lạ.
        raise GatewayError(0, "challenge_mismatch", "challenge không nêu gateway/địa chỉ ví này")
    sig = "0x" + wkey.sign(msg.encode("utf-8")).hex()
    d = _call(http, "POST", base + "/enroll/wallet",
              body={"address": addr, "message": msg, "signature": sig})
    if not d.get("key") or not d.get("model"):
        raise GatewayError(0, "bad_enrollment", f"thiếu key/model (keys: {sorted(d)[:8]})")
    return d


def me(http, base: str, bearer: str) -> dict:
    return _call(http, "GET", base + "/keys/me", bearer=bearer)


def receipts_detail(http, base: str, bearer: str):
    """(biên nhận, hình dạng). Hình dạng = các KHOÁ + số mục, không có nội dung — để log biết
    endpoint thật trả gì khi không tìm thấy biên nhận (devnet 2026-10-24: 6 lần chi, 0 biên nhận)."""
    d = _call(http, "GET", base + "/keys/me/receipts", bearer=bearer)
    for k in ("receipts", "data", "items"):
        if isinstance(d.get(k), list):
            items = [x for x in d[k] if isinstance(x, dict)]
            first = sorted(items[0])[:12] if items else []
            return items, f"{k}[{len(items)}] item_keys={first}"
    return [], f"keys={sorted(d)[:12]}"


def receipts(http, base: str, bearer: str) -> list:
    return receipts_detail(http, base, bearer)[0]


def estimate(http, base: str, model: str, prompt: str, max_tokens: int) -> dict:
    return _call(http, "POST", base + "/models/estimate",
                 body={"model": model, "prompt": prompt, "max_tokens": int(max_tokens)})


def chat(http, base: str, bearer: str, model: str, prompt: str, max_tokens: int,
         max_cost: float, max_wait_ms: int = 60000, idempotency_key: str = None) -> dict:
    """`idempotency_key` -> header `Idempotency-Key` (1–128 ký tự ASCII in được). Trang Connect dùng
    nó làm `request_id` của biên nhận; README @flop-labs/client 0.2.1: gateway LƯU phản hồi JSON đã
    hoàn tất (stream:false) để phát lại theo khoá. Chưa kiểm chứng trên gateway thật: phát lại khi
    request gốc còn chạy / chưa tới nơi, và có miễn phí không -> vẫn đối soát bằng biên nhận."""
    return _call(http, "POST", base + "/chat/completions", bearer=bearer, timeout=max_wait_ms / 1000 + 15,
                 extra_headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
                 body={"model": model, "max_tokens": int(max_tokens),
                       "messages": [{"role": "user", "content": prompt}],
                       "max_cost_flop": round(float(max_cost), 6), "max_wait_ms": int(max_wait_ms)})


# --- nhịp chi: rải quota còn lại tới lúc hết hạn ----------------------------------------

def answer_text(resp) -> str:
    """Nội dung câu trả lời từ phản hồi chat/completions (dạng OpenAI); '' nếu không có."""
    try:
        c = (resp.get("choices") or [])[0]
        m = c.get("message") if isinstance(c.get("message"), dict) else {}
        t = m.get("content") if m else c.get("text")
        return t.strip() if isinstance(t, str) else ""
    except (AttributeError, IndexError, TypeError):
        return ""


def epoch_s(v):
    """Mốc thời gian -> giây. OpenAPI ghi `expires_at` là mili-giây; nếu mạng khác trả GIÂY
    mà vẫn chia 1000 thì hạn rơi về năm 1970 -> bearer luôn "sắp hết hạn" -> đăng ký lại ví
    MỌI lượt. Giá trị > 1e11 coi là mili-giây, ngược lại là giây. Rỗng/rác -> None."""
    try:
        x = int(float(v))
    except (TypeError, ValueError, OverflowError):    # số khổng lồ/vô cực từ phản hồi lạ
        return None
    if x <= 0 or x > 10**16:                           # quá năm ~2286 (ms) = rác, không phải hạn
        return None
    return x // 1000 if x > 10**11 else x


def horizon(now: int, expires_at) -> int:
    """Mốc phải chi hết: hạn của allowance nếu có (ms hoặc s), không thì cuối ngày UTC."""
    exp = epoch_s(expires_at)
    if exp and exp > now:
        return exp
    return now - now % 86400 + 86400


def window_due(win, remaining: float, now: int, horizon_ts: int, interval: float,
               margin: float, topup_min: float = 10.0):
    """-> (due, win). Rải quota ĐỀU tới hạn theo MỤC TIÊU TUYẾN TÍNH có cộng dồn (như
    flop_pacer): cửa sổ [start, hạn - biên], total = quota lúc mở cửa sổ; mục tiêu tới run
    này = total * (now + interval - start) / (end - start); due = mục tiêu - đã chi.
    Phần nhỏ chưa đủ 1 lần gọi được TÍCH LẠI sang run sau (không bị bỏ).
    Mở cửa sổ mới khi hạn đổi (allowance/ngày mới) hoặc quota TĂNG ít nhất `topup_min`
    (faucet nạp). Quota tăng ÍT hơn thế = gateway HOÀN phần giữ chỗ thừa của lần gọi trước
    (devnet 2026-10-24: giữ chỗ ~6,5, settle khác) -> trừ vào "đã chi", KHÔNG mở cửa sổ mới.
    Trước đây mỗi lần hoàn tiền đều mở cửa sổ mới: mốc kế hoạch về 0, mất phần chi bù đã tích
    và cảnh báo tụt lại không bao giờ bắn."""
    win = dict(win) if isinstance(win, dict) else {}
    spent = float(win.get("spent") or 0)
    expect = float(win.get("total") or 0) - spent          # quota còn lại nếu không có biến động
    if win.get("horizon") != horizon_ts or remaining - expect >= topup_min:
        win, spent = {"start": now, "horizon": horizon_ts, "total": float(remaining), "spent": 0.0}, 0.0
    elif remaining > expect + 1e-9:                         # hoàn tiền nhỏ -> giảm "đã chi"
        spent = max(0.0, spent - (remaining - expect))
        win["spent"] = spent
    elif remaining < expect - 1e-9:
        # Quota TỤT mà ta không ghi nhận (vd request timeout phía ta nhưng server đã xử lý và trừ
        # tiền). Không cộng vào "đã chi" thì phần đến hạn bị đẩy cao -> chi bù vượt kế hoạch.
        spent += expect - remaining
        win["spent"] = spent
    if remaining <= 0:
        return 0.0, win
    start, end, total = int(win["start"]), horizon_ts - margin, float(win["total"])
    if end - now <= interval or end <= start:
        target = total                                     # sát hạn -> chi hết phần còn lại
    else:
        target = total * min(1.0, (now + interval - start) / (end - start))
    return max(0.0, min(target - spent, float(remaining))), win


def size_call(per_call: float, prompt_tokens: int, price_prompt: float, price_completion: float,
              cap_tokens: int, min_tokens: int):
    """max_tokens để MỘT lần gọi tốn ~per_call FLOP; None nếu không đủ cho min_tokens."""
    if per_call <= 0 or price_completion <= 0:
        return None
    room = per_call - max(0, prompt_tokens) * max(0.0, price_prompt)
    n = math.floor(room / price_completion) if room > 0 else 0
    n = min(n, cap_tokens)
    return n if n >= min_tokens else None


def _price(model: dict):
    """(giá/token prompt, giá/token completion, giá CỐ ĐỊNH/lần gọi). Giá cố định lấy từ
    FLOP_GATEWAY_FIXED_PRICE (operator đặt khi FLOP công bố ở chỗ khác) hoặc `pricing.request`
    (trường OpenRouter). Devnet 2026-10-24 chỉ tính theo token; testnet có thể khác."""
    p = (model or {}).get("pricing") or {}

    def num(v):
        try:
            return max(0.0, float(v or 0))
        except (TypeError, ValueError):
            return 0.0
    fixed = num(os.environ.get("FLOP_GATEWAY_FIXED_PRICE", "").strip()) or num(p.get("request"))
    return num(p.get("prompt")), num(p.get("completion")), fixed


def _completion_cap(model: dict, default: int) -> int:
    for src in ((model or {}).get("per_request_limits") or {}, (model or {}).get("top_provider") or {}):
        for k in ("completion_tokens", "max_completion_tokens"):
            try:
                if src.get(k):
                    return min(default, int(src[k]))
            except (TypeError, ValueError):
                continue
    return default


TOK_BAD_TTL = 86400                    # quên cỡ bị từ chối sau 1 ngày (miner có thể đã khoẻ hơn)
# Request "đang treo" (đã gửi, không rõ kết quả vì timeout/mất kết nối): chờ biên nhận tối đa
# chừng này giây rồi bỏ. /keys/me/receipts chỉ trả 20 biên nhận gần nhất nên không đợi mãi được.
INFLIGHT_TTL_SEC = 30 * 60
# Trần chi TUYỆT ĐỐI của đường gateway (0 = tắt). Mặc định cao hơn mức faucet lớn nhất đã thấy
# (Verified 2000 FLOP/ngày): đây là phanh cho lỗi/cấu hình sai, không phải nhịp chi bình thường.
DEFAULT_MAX_PER_RUN = 100.0
DEFAULT_MAX_PER_DAY = 2500.0
# Mã lỗi của /chat/completions mà KHÔNG chắc server phía sau chưa xử lý: proxy hết giờ chờ
# (502/504, Cloudflare 522/524), 408, 500. Xử lý như timeout: giữ request treo để đối soát,
# không gửi lại cùng đề — gửi lại có thể trả tiền hai lần cho cùng một việc.
AMBIGUOUS_STATUS = frozenset({408, 500, 502, 504, 522, 524})
# Phát lại request treo (cùng Idempotency-Key, cùng thân): các mã này = "chưa xong, thử lại sau";
# mã khác = gateway từ chối hẳn -> không phát lại nữa, chỉ còn đối soát bằng biên nhận.
REPLAY_RETRY_STATUS = AMBIGUOUS_STATUS | {409, 425, 429, 503}
# Biên nhận thật (devnet 2026-10-24) KHÔNG có trường miner, chỉ có channel_id: tra kênh trên
# explorer để biết miner và kênh ĐỨNG TÊN AI (agent). Tối đa ngần này lần tra mỗi lượt.
CHANNEL_LOOKUPS_PER_RUN = 5


def probe_up(tok_cap: int, cap: int, tok_bad, tok_bad_ts, now: int) -> int:
    """Cỡ thử tiếp sau một chuỗi thành công. Không bao giờ nhảy thẳng về trần model: trước
    đây 12 lần ổn định -> bỏ trần -> gửi 8192 -> bị từ chối -> hạ 4096 -> 2048 -> 1024, mất 3
    lượt mỗi chu kỳ (~mỗi giờ trên devnet). Tăng tối đa 25%, và nếu đã biết cỡ bị từ chối
    (trong 1 ngày) thì chỉ thử tới giữa khoảng [cỡ đang chạy, cỡ bị từ chối)."""
    nxt = min(cap, int(tok_cap * 1.25))
    if tok_bad and now - int(tok_bad_ts or 0) < TOK_BAD_TTL and nxt >= int(tok_bad):
        nxt = (tok_cap + int(tok_bad)) // 2
    return max(tok_cap, nxt)


def _receipt_key(r: dict) -> str:
    return str(r.get("request_id") or r.get("tx_hash") or r.get("channel_id")
               or f"{r.get('block_number')}:{r.get('prompt_hash')}")[:96]


# Biên nhận THẬT của devnet (2026-10-24) KHÔNG theo schema InferenceReceipt của OpenAPI: không có
# block_number/tx_hash mà có channel_id, request_id, reserved_spend, settled_spend(_raw), state,
# response_status, terminal_event. "Đã settle" = state thuộc nhóm dưới / có block_number / có
# settled_spend > 0 mà state không thuộc nhóm chưa-xong.
_SETTLED_STATES = {"settled", "finalized", "final", "complete", "completed", "success", "succeeded", "paid"}
_WAITING_STATES = {"pending", "estimated", "reserved", "open", "opened", "in_flight", "inflight", "running",
                   "submitted"}
_OPEN_STATES = _WAITING_STATES | {"failed", "error", "refunded", "released", "cancelled", "canceled", "expired",
                                  "rejected"}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def unsettled_exposure(g) -> float:
    """FLOP đã cam kết mà sổ chưa có: biên nhận đang chờ settle, request treo, biên nhận đã mất dấu."""
    g = g if isinstance(g, dict) else {}
    held = [v[1] for v in (g.get("receipts_open") or {}).values() if isinstance(v, list) and len(v) == 2]
    held += [(g.get("inflight") or {}).get("max_cost"), (g.get("stats") or {}).get("receipts_lost_flop")]
    return sum(_num(v) or 0.0 for v in held)


def _cost(v) -> float:
    """Chi phí từ phản hồi: số hữu hạn dương, còn lại 0 — phản hồi lạ không được làm mất lần đã chi."""
    x = _num(v)
    return x if x is not None and math.isfinite(x) and x > 0 else 0.0


def _spend_value(rc: dict):
    """Số FLOP của biên nhận: `settled_spend`, không có thì `settled_spend_raw` (đơn vị 1e-18)."""
    amt = _num(rc.get("settled_spend"))
    if amt is not None:
        return amt
    raw = _num(rc.get("settled_spend_raw"))
    return raw / 1e18 if raw is not None else None


def settled_amount(rc: dict):
    """FLOP đã settle của 1 biên nhận, hoặc None nếu chưa settle."""
    st = str(rc.get("state") or "").strip().lower()
    amt = _spend_value(rc)
    if rc.get("block_number") is not None or st in _SETTLED_STATES:
        return amt or 0.0
    if amt and amt > 0 and st not in _OPEN_STATES:
        return amt
    return None


# --- cảnh báo chi tụt lại ---------------------------------------------------------------

_LAG_HINTS = {
    "spent": "đã chạy hết công suất mỗi lượt mà vẫn không kịp — tăng FLOP_GATEWAY_MAX_CALLS_PER_RUN "
             "(nếu ít 503), chờ trần token tự tăng, hoặc chọn model đắt hơn",
    "no_miner": "miner không nhận cỡ yêu cầu — agent đang tự hạ cỡ",
    "rate_limited": "gateway 429/503: thiếu miner rảnh hoặc chạm giới hạn tốc độ",
    "payment_required": "402: gateway đòi thanh toán x402 — agent KHÔNG tự trả, cần chỉnh tay",
    "unfunded": "ví chưa đủ số dư tối thiểu — kiểm tra faucet",
    "forbidden": "403 — allowance bị thu hồi/hết hạn",
    "exhausted": "quota/allowance đã hết",
    "auth_reset": "401 — bearer hỏng, đang đăng ký lại",
    "network_mismatch": "gateway báo sai mạng — kiểm FLOP_GATEWAY_URL / FLOP_GATEWAY_NETWORK",
    "error_network": "không đọc được /health của gateway",
    "model_unavailable": "model của key không còn trong catalog",
    "backoff": "đang chờ theo Retry-After / lỗi trước",
    "catalog_empty": "catalog model tạm rỗng — giữ bearer, chờ gateway hồi phục",
    "enroll_unavailable": "mạng này không có /enroll/wallet — cần adapter đăng ký theo API testnet",
    "daily_cap": "chạm trần FLOP_GATEWAY_MAX_PER_DAY",
    "attribution_hold": "đang dừng mua: chain chưa xác nhận chi tiêu đứng tên ví mình",
    "unknown_outcome": "request timeout/mất kết nối — không rõ server đã trừ tiền chưa, đang giữ để đối soát",
    "reconciling": "đang chờ biên nhận của request treo trước khi gọi mới (tối đa 30 phút)",
    "error": "lỗi khác — xem dòng [gateway] lý do",
}


def lag_status(g: dict, now: int, outcome: str, *, pct: float, alert_hours: float,
               realert_hours: float):
    """Theo dõi chi TỤT LẠI so với mục tiêu tuyến tính của cửa sổ. Trả (dòng log | None,
    tin cảnh báo | None, tin hồi phục | None). Tụt = mục tiêu tới giờ - đã chi > pct% tổng
    cửa sổ; cảnh báo khi tụt liên tục >= alert_hours (lặp lại mỗi realert_hours), kèm lý do
    thường gặp nhất trong đợt (đếm outcome mỗi run). Hồi phục khi tụt < pct/2."""
    win = g.get("window") if isinstance(g.get("window"), dict) else None
    lag = dict(g.get("lag") or {})
    if not win or not win.get("total"):
        return None, None, None
    total, spent = float(win["total"]), float(win.get("spent") or 0)
    start = int(win.get("start") or now)
    end = int(win.get("horizon") or now) - int(_env_num("FLOP_GATEWAY_MARGIN_SEC", 1800))
    frac_t = 1.0 if end <= start else min(1.0, max(0.0, (now - start) / (end - start)))
    behind = total * frac_t - spent
    share = behind / total if total > 0 else 0.0
    if share * 100 <= pct / 2:
        g["lag"] = {}
        if lag.get("alerted"):
            return None, None, (f"✅ FLOP gateway: chi đã bắt kịp kế hoạch (đã chi {spent:.2f}/"
                                f"{total * frac_t:.2f} mục tiêu).")
        return None, None, None
    if share * 100 <= pct:
        g["lag"] = lag                               # vùng đệm: không mở/đóng đợt
        return None, None, None
    lag.setdefault("since", now)
    reasons = dict(lag.get("reasons") or {})
    reasons[outcome] = int(reasons.get(outcome) or 0) + 1
    lag["reasons"] = reasons
    top = max(reasons, key=reasons.get)
    hours = (now - int(lag["since"])) / 3600
    line = (f"[gateway] ⚠️ chi tụt lại {behind:.2f} FLOP ({share:.0%} cửa sổ) trong {hours:.1f}h — "
            f"lý do chính: {top}")
    alert = None
    if hours >= alert_hours and now - int(lag.get("alerted") or 0) >= realert_hours * 3600:
        lag["alerted"] = now
        counts = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
        alert = (f"⚠️ FLOP gateway: chi TỤT LẠI {hours:.1f}h liền — đã chi {spent:.2f} / mục tiêu "
                 f"{total * frac_t:.2f} FLOP (thiếu {behind:.2f}, {share:.0%} cửa sổ {total:.2f}).\n"
                 f"Kết quả các lượt: {counts}.\n→ {_LAG_HINTS.get(top, top)}")
    g["lag"] = lag
    return line, alert, None


def report_line(g: dict, now: int):
    """Một dòng cho báo cáo ngày: quota còn, đã chi so với mục tiêu tới giờ, biên nhận đã settle,
    kết quả lượt gần nhất. Báo cáo cũ chỉ biết bộ chia `flop_pacer` -> trên testnet (gateway)
    dòng nhịp biến mất. None nếu gateway chưa chạy lần nào."""
    if not isinstance(g, dict) or not g.get("last_run"):
        return None
    parts = [f"mạng {g.get('network') or '?'}"]
    win = g.get("window") if isinstance(g.get("window"), dict) else None
    if win and win.get("total"):
        total, spent = float(win["total"]), float(win.get("spent") or 0)
        start = int(win.get("start") or now)
        end = int(win.get("horizon") or now) - int(_env_num("FLOP_GATEWAY_MARGIN_SEC", 1800))
        frac = 1.0 if end <= start else min(1.0, max(0.0, (now - start) / (end - start)))
        target = total * frac
        pct = int(spent / target * 100) if target > 0 else 100
        parts.append(f"đã chi {spent:.2f}/{target:.2f} mục tiêu ({pct}%)")
    if g.get("remaining") is not None:
        parts.append(f"quota còn {float(g['remaining']):.2f}")
    st = g.get("stats") or {}
    parts.append(f"biên nhận settle {int(st.get('receipts') or 0)} ({float(st.get('settled') or 0):.2f} FLOP)")
    if st.get("receipts_lost"):
        parts.append(f"⚠️ {int(st['receipts_lost'])} biên nhận rời phản hồi khi chưa settle")
    parts.append(f"lượt cuối: {g.get('last_outcome', '?')}")
    return " · ".join(parts)


# --- một nhịp ---------------------------------------------------------------------------

def profile_of(expect_network: str) -> str:
    """Dấu nhận diện gateway + mạng: đổi dấu này thì trạng thái cũ không còn dùng được."""
    return f"{base_url()}|{expect_network}"


def run_step(state: dict, *, agent_key, http, now: int, prompts: list, expect_network: str,
             book_fn=None, sleep=time.sleep, log=print, rng=None, deadline=None,
             clock=time.time, answer_fn=None, checkpoint=None, new_key=None,
             channel_fn=None, gate=None, after_book=None) -> dict:
    """Một nhịp mua compute. Trả {"outcome", "calls", "spent", ...}; KHÔNG ném lỗi.

    `book_fn(amount, ref, model)` ghi sổ (chỉ khi mạng khớp `expect_network`); None = chạy
    tập trên devnet, không ghi sổ. Trạng thái bền trong state[KEY]: két bearer, model, quota,
    hạn, mốc chờ (Retry-After), biên nhận đã thấy, thống kê.

    Request TREO: trước mỗi lần gọi ghi g['inflight'] = {key (Idempotency-Key), ts, max_cost} rồi
    gọi `checkpoint()` (caller lưu state xuống đĩa) -> run bị giết giữa chừng vẫn để lại dấu vết.
    Lỗi transport (timeout/mất kết nối) = KHÔNG RÕ server đã xử lý chưa: đề vẫn coi như đã gửi
    (không gửi lại cùng đề) và lượt sau KHÔNG gọi mới cho tới khi thấy biên nhận có
    request_id == key, hoặc quá INFLIGHT_TTL_SEC. `new_key()` sinh khoá (tiêm được để test).
    Có thể PHÁT LẠI đúng request với đúng khoá (FLOP_GATEWAY_REPLAY_MAX lần, mặc định 0 = tắt vì
    chưa kiểm chứng gateway không tính phí lần phát lại) để lấy phản hồi đã lưu; kết quả 'replayed'.
    Quota tụt sau một lần phát lại -> ghi g['replay_unsafe'], không phát lại nữa.

    `gate()` -> (lý do dừng | None, số FLOP còn được mua | None), gọi SAU khi đọc biên nhận: có lý do
    thì vẫn đọc biên nhận và đối soát nhưng không gọi mới. `after_book()` chạy ngay khi có biên nhận
    mới: caller lưu sổ TRƯỚC khi checkpoint ghi receipts_seen xuống đĩa, nếu không run bị huỷ giữa
    lần gọi sẽ để lại "đã ghi" trong khi sổ mất khoản đó.

    `channel_fn(channel_id)` -> {"miner", "agent"} | None: tra kênh của biên nhận mới (biên nhận
    không có miner). Kết quả có `miners` và `foreign_channels` = số kênh KHÔNG đứng tên ví mình."""
    import uuid
    new_key = new_key or (lambda: str(uuid.uuid4()))
    g = state.get(KEY) if isinstance(state.get(KEY), dict) else {}
    g = dict(g)
    # Đổi gateway/mạng (vd tập devnet -> testnet thật): bắt đầu SẠCH. Không thì lịch nghỉ, cửa
    # sổ chi, bearer, cỡ yêu cầu và biên nhận của mạng cũ (vd "chờ 1h" sau 403 hết hạn devnet)
    # kẹt sang mạng mới — lượt đầu testnet có thể đứng chờ tới 1 giờ.
    profile = profile_of(expect_network)
    if g and g.get("profile") != profile:
        log(f"[gateway] đổi sang {profile} — bỏ trạng thái của {g.get('profile') or 'mạng cũ'}")
        g = {}
    g["profile"] = profile
    out = {"outcome": "?", "calls": 0, "spent": 0.0}
    calls, spent = 0, 0.0                    # giữ được cả khi lần gọi sau lỗi (báo đúng số đã gọi)
    pending = None                           # Idempotency-Key của lần gọi đang chờ phản hồi
    rs = {}                                  # kết quả đọc biên nhận: trả về ở MỌI nhánh, kể cả nhánh lỗi

    def done(outcome, **kw):
        out.update(rs)
        out.update(outcome=outcome, **kw)
        g["last_outcome"], g["last_run"] = outcome, now
        state[KEY] = g
        return out

    if int(g.get("wait_until") or 0) > now:
        return done("backoff", reason=f"chờ tới {g['wait_until']} ({g.get('wait_why', '')})")
    wkey = wallet_key(agent_key)
    if wkey is None:
        return done("unconfigured", reason="chưa có khoá ví (FLOP_WALLET_SEED_HEX hoặc FLOP_WALLET_USE_DID=on)")
    if not prompts:
        return done("unconfigured", reason="chưa có đề bài")
    base = base_url()
    addr = wallet_address(wkey)
    g["address"] = addr

    net, why = network_detail(http, base)
    if net is None:
        return done("error_network", reason=f"không đọc được /health của gateway ({why})")
    g["network"] = net
    if not network_matches(net, expect_network):
        return done("network_mismatch", reason=f"gateway báo '{net}', cần '{expect_network}' — không chi")

    def backoff(sec, why):
        g["wait_until"], g["wait_why"] = now + int(max(60, sec)), why[:120]

    def charge_day(amount: float) -> None:
        d = time.strftime("%Y-%m-%d", time.gmtime(now))
        dc = g.get("day_spent") if isinstance(g.get("day_spent"), dict) else {}
        base = float(dc.get("spent") or 0) if dc.get("day") == d else 0.0
        g["day_spent"] = {"day": d, "spent": round(base + float(amount or 0), 6)}

    def tally(n_calls: int, amount: float, n_sent: int = None) -> None:
        """Xoay đề + cộng thống kê theo số lần gọi ĐÃ THÀNH CÔNG. Gọi cả khi lượt kết thúc bằng
        lỗi (vd lần 2 bị 429): không thì đề vừa gửi bị gửi LẠI ở lượt sau (prompt_hash trùng)
        và thống kê hụt. `n_sent` > n_calls khi một lần gọi KHÔNG RÕ kết quả (timeout): đề đó
        vẫn xoay qua — gửi lại = có thể trả tiền hai lần cho cùng một việc."""
        n_sent = n_calls if n_sent is None else n_sent
        out["sent"] = n_sent
        g["prompt_idx"] = (int(g.get("prompt_idx") or 0) + n_sent) % max(1, len(prompts))
        g["last_calls"] = n_calls
        st = dict(g.get("stats") or {})
        st["calls"] = int(st.get("calls") or 0) + n_calls
        st["spent"] = round(float(st.get("spent") or 0) + amount, 6)
        g["stats"] = st

    try:
        bearer = open_bearer(agent_key, g.get("vault"))
        exp_s = epoch_s(g.get("expires_at"))
        if bearer and exp_s and exp_s <= now + 120:
            bearer = None                                     # sắp hết hạn -> enroll lại (không hạn -> giữ)
        if not bearer:
            try:
                e = enroll(http, base, wkey, _env("FLOP_GATEWAY_PURPOSE") or None)
            except GatewayError as ge:
                if ge.status not in (404, 405, 410):
                    raise
                # OpenAPI ghi /enroll/* là "Devnet only": trên mạng khác đây là thiếu đường đăng ký,
                # không phải lỗi tạm thời.
                backoff(3600, f"enroll {ge.status}")
                return done("enroll_unavailable",
                            reason=f"gateway không có /enroll/wallet (HTTP {ge.status}) — chưa mua được")
            bearer = e["key"]
            g["vault"] = seal_bearer(agent_key, bearer)
            g.update(model=e["model"], expires_at=e.get("expires_at"),
                     remaining=e.get("remaining_quota"), enrolled_at=now)
            log(f"[gateway] enroll ví {addr} model={e['model']} quota={e.get('remaining_quota')}")
        info = me(http, base, bearer)
        remaining = float(info.get("remaining_quota") or 0)
        g.update(remaining=remaining, expires_at=info.get("expires_at") or g.get("expires_at"))
        model_id = g.get("model")
        models = catalog(http, base)
        if not models:
            # Catalog rỗng = dịch vụ phía sau đang lỗi, không phải model bị gỡ: giữ bearer.
            backoff(600, "catalog_empty")
            return done("catalog_empty", remaining=remaining, reason="catalog model rỗng — thử lại sau")
        if model_id not in models:
            # Key gắn với 1 model; model rời catalog -> không gửi slug không được quảng bá. Bỏ
            # bearer + model để lượt sau ĐĂNG KÝ LẠI: gateway có thể cấp model khác còn sống
            # ("catalog drift may choose another compatible model"). Trước đây chỉ nghỉ 30
            # phút rồi gặp lại đúng lỗi này -> kẹt mãi, không chi gì.
            g.pop("vault", None)
            g.pop("model", None)
            g.pop("tok_cap", None)
            g.pop("tok_bad", None)
            backoff(1800, "model_not_advertised")
            return done("model_unavailable",
                        reason=f"model {model_id} không còn trong catalog — sẽ đăng ký lại ví ở lượt sau")
        model = models[model_id]
        rids = set()                        # request_id của các biên nhận gateway vừa trả
        settled_rids = set()                # ... trong đó đã settle

        def collect(prev_calls: int) -> dict:
            """Đọc biên nhận; ghi sổ (book_fn) MỖI biên nhận mới ĐÃ SETTLE theo settled_spend —
            đúng thước đo "compute purchased in settled sessions". Dedupe theo request_id."""
            seen = list(g.get("receipts_seen") or [])
            waiting = dict(g.get("receipts_open") or {})
            new, amt, hist = [], 0.0, {}
            try:
                items, shape = receipts_detail(http, base, bearer)
            except GatewayError as e:
                items, shape = [], f"HTTP {e.status} {e.code}".strip()
            live = {_receipt_key(rc) for rc in items}
            rids.clear()
            rids.update(str(rc.get("request_id")) for rc in items if rc.get("request_id"))
            settled_rids.clear()
            settled_rids.update(str(rc.get("request_id")) for rc in items
                                if rc.get("request_id") and settled_amount(rc) is not None)
            for rc in items:
                key = f"{rc.get('state')}/{rc.get('response_status')}"
                hist[key] = hist.get(key, 0) + 1
                k = _receipt_key(rc)
                a = settled_amount(rc)
                if a is None and str(rc.get("state") or "").strip().lower() in _WAITING_STATES:
                    waiting.setdefault(k, [now, _num(rc.get("reserved_spend")) or 0.0])
                else:
                    waiting.pop(k, None)
                if a is None or k in seen:
                    continue
                seen.append(k)
                new.append(rc)
                amt += a
                if book_fn and a > 0:
                    book_fn(a, k[:80], str(rc.get("model") or model_id))
                elif book_fn:
                    log(f"[gateway] ⚠️ biên nhận {k[:12]}… đã settle nhưng không có số tiền "
                        f"(keys: {sorted(rc)[:10]})")
            # /keys/me/receipts không phân trang: biên nhận rời phản hồi khi chưa settle thì không
            # còn đọc được số tiền. Không ghi sổ theo ước tính — đếm và báo để đối soát tay.
            lost = [k for k in waiting if k not in live] if live else []
            lost_flop = sum(_cost(v[1]) for v in (waiting.pop(k) for k in lost) if isinstance(v, list))
            if lost:
                log(f"[gateway] ⚠️ {len(lost)} biên nhận rời phản hồi khi chưa settle — sổ có thể thiếu")
            g["receipts_open"] = dict(list(waiting.items())[-100:])
            # Nhớ MỌI biên nhận gateway còn trả (cắt cứng 200 mục từng có thể quên một biên nhận
            # vẫn nằm trong phản hồi -> ghi sổ lần hai) + tối đa 200 cái đã rời phản hồi.
            # Phản hồi rỗng/lỗi -> KHÔNG cắt gì (lần sau gateway trả đủ thì không ghi trùng).
            if live:
                gone = [k for k in seen if k not in live]
                g["receipts_seen"] = gone[-200:] + [k for k in seen if k in live]
            else:
                g["receipts_seen"] = seen[-5000:]
            st = dict(g.get("stats") or {})
            st["receipts"] = int(st.get("receipts") or 0) + len(new)
            st["settled"] = round(float(st.get("settled") or 0) + amt, 6)
            if lost:
                st["receipts_lost"] = int(st.get("receipts_lost") or 0) + len(lost)
                st["receipts_lost_flop"] = round(float(st.get("receipts_lost_flop") or 0) + lost_flop, 6)
            g["stats"] = st
            if prev_calls or new or not st["receipts"]:      # chưa từng thấy biên nhận -> luôn in hình dạng
                log(f"[gateway] biên nhận: settled +{len(new)} ({round(amt, 6)} FLOP) | "
                    f"{', '.join(f'{k}={v}' for k, v in sorted(hist.items())) or shape}")
            miners = [str(x.get("miner") or "") for x in new if x.get("miner")]
            agents = {}
            if channel_fn:
                for rc in [x for x in new if x.get("channel_id")][:CHANNEL_LOOKUPS_PER_RUN]:
                    try:
                        info = channel_fn(str(rc["channel_id"])) or {}
                    except Exception:
                        info = {}
                    if info.get("miner") and not rc.get("miner"):
                        miners.append(str(info["miner"]))
                    if info.get("agent"):
                        agents[str(info["agent"])] = agents.get(str(info["agent"]), 0) + 1
            foreign = {a: n for a, n in agents.items() if a != addr}
            if foreign:
                log(f"[gateway] ⚠️ {sum(foreign.values())} kênh vừa settle đứng tên "
                    f"{', '.join(a[:8] + '…' for a in foreign)} — KHÔNG phải ví mình {addr[:8]}…")
            return {"receipts_new": len(new), "settled_new": round(amt, 6),
                    "receipts_total": st["receipts"], "settled_total": st["settled"],
                    "miners": miners, "channel_agents": agents,
                    "foreign_channels": sum(foreign.values())}

        # Đọc biên nhận TRƯỚC khi gọi: biên nhận settle muộn (của các run trước) luôn được ghi,
        # kể cả khi lần gọi run này lỗi (429/503) hay chưa tới nhịp.
        rs = collect(int(g.get("last_calls") or 0))
        g["last_calls"] = 0
        if after_book and rs.get("receipts_new"):
            try:
                after_book()
            except Exception as e:
                log(f"[gateway] không lưu được sổ sau biên nhận mới ({str(e)[:80]})")
        # ĐỐI SOÁT request treo của lượt trước TRƯỚC khi gọi mới (xem docstring).
        inf = g.get("inflight") if isinstance(g.get("inflight"), dict) else None
        if inf:
            key, age = str(inf.get("key") or ""), now - int(inf.get("ts") or 0)
            ttl = _env_num("FLOP_GATEWAY_INFLIGHT_TTL_SEC", INFLIGHT_TTL_SEC)
            replay_max = int(_env_num("FLOP_GATEWAY_REPLAY_MAX", 0))
            wait_s = int(inf.get("max_wait") or 0) / 1000

            def try_replay():
                """Gửi lại ĐÚNG thân request với ĐÚNG khoá. Trả phản hồi, hoặc None nếu chưa lấy được."""
                inf["replays"] = int(inf.get("replays") or 0) + 1
                g["inflight"] = inf
                if checkpoint:
                    state[KEY] = g
                    try:
                        checkpoint()
                    except Exception as e:
                        log(f"[gateway] không lưu được mốc phát lại ({str(e)[:80]})")
                try:
                    return chat(http, base, bearer, inf["model"], inf["prompt"], int(inf["mt"]),
                                max_cost=float(inf["max_cost"]), max_wait_ms=int(inf["max_wait"]),
                                idempotency_key=key)
                except GatewayError as e:
                    if e.status not in REPLAY_RETRY_STATUS:
                        inf["replays"] = replay_max          # từ chối hẳn -> thôi phát lại
                    log(f"[gateway] phát lại {key[:8]}… chưa được: HTTP {e.status} {e.code}")
                except Exception as e:
                    log(f"[gateway] phát lại {key[:8]}… chưa được: {type(e).__name__}")
                return None

            # Biên nhận chưa có -> phát lại để biết kết quả. Đã settle -> chỉ phát lại khi còn cần
            # câu trả lời. Biên nhận thất bại (released...) -> không phát lại: có thể chạy lại việc.
            if (inf.get("prompt") and inf.get("model") == model_id
                    and int(inf.get("replays") or 0) < replay_max and age < ttl
                    and not g.get("replay_unsafe")
                    and (key not in rids or (answer_fn and key in settled_rids))
                    and not (deadline and clock() + wait_s + 15 > deadline)):
                r = try_replay()
                try:
                    after = float(me(http, base, bearer).get("remaining_quota") or 0)
                except Exception:
                    after = remaining
                drop = max(0.0, remaining - after) if after < remaining - 1e-9 else 0.0
                if drop:
                    # Có thể là chính request treo vừa settle; phân biệt không được nên chọn hướng an
                    # toàn: thôi phát lại vĩnh viễn trên profile này.
                    g["replay_unsafe"] = now
                    out["replay_charged"] = round(drop, 6)
                    log(f"[gateway] ⚠️ phát lại {key[:8]}… làm quota tụt {drop:.4f} FLOP — ngừng phát lại")
                if r is not None:
                    g.pop("inflight", None)
                    usage = r.get("usage") if isinstance(r.get("usage"), dict) else {}
                    cost = _cost(usage.get("cost_flop")) or drop
                    charge_day(cost)
                    if answer_fn:
                        try:
                            answer_fn(inf["prompt"], answer_text(r), cost)
                        except Exception as e:
                            log(f"[gateway] bỏ qua lỗi khi lưu câu trả lời ({str(e)[:80]})")
                    log(f"[gateway] phát lại {key[:8]}… lấy được phản hồi đã lưu")
                    tally(1, cost, n_sent=0)                 # đề đã xoay ở lượt gặp lỗi
                    g["remaining"] = after
                    return done("replayed", calls=1, spent=round(cost, 6), remaining=after, model=model_id)
            if key and key in rids:
                log(f"[gateway] đối soát xong request treo {key[:8]}… — đã có biên nhận")
                g.pop("inflight", None)
            elif age < ttl:
                return done("reconciling", remaining=remaining,
                            reason=(f"request {key[:8]}… gửi {age}s trước chưa rõ kết quả (timeout) — "
                                    f"chờ biên nhận tối đa {int(ttl)}s trước khi gọi mới"))
            else:
                log(f"[gateway] bỏ request treo {key[:8]}… sau {age}s — không thấy biên nhận "
                    "(quota tụt nếu có đã được tính vào 'đã chi')")
                g.pop("inflight", None)
        room = None
        if gate:
            state[KEY] = g                    # gate đọc biên nhận chờ / request treo vừa cập nhật
            hold, room = gate()
            if hold:
                return done("attribution_hold", remaining=remaining, reason=str(hold)[:200])
        if remaining <= 0:
            backoff(1800, "quota_exhausted")
            return done("exhausted", reason="quota gateway đã hết")

        hz = horizon(now, g.get("expires_at"))
        # Ngưỡng coi là faucet NẠP (không phải hoàn tiền): >= 10 FLOP và >= 2 lần cỡ lần gọi gần nhất.
        topup = max(_env_num("FLOP_GATEWAY_TOPUP_MIN", 10), 2 * float((g.get("last") or {}).get("cost") or 0))
        due, win = window_due(g.get("window"), remaining, now, hz,
                              _env_num("FLOP_GATEWAY_RUN_INTERVAL_SEC", 300),
                              _env_num("FLOP_GATEWAY_MARGIN_SEC", 1800), topup)
        g["window"] = win
        run_cap = _env_num("FLOP_GATEWAY_MAX_PER_RUN", DEFAULT_MAX_PER_RUN)
        day_cap = _env_num("FLOP_GATEWAY_MAX_PER_DAY", DEFAULT_MAX_PER_DAY)
        charge_day(0)                                      # sang ngày UTC mới -> đếm lại từ 0
        day_left = day_cap - float(g["day_spent"]["spent"])
        if day_cap > 0 and day_left <= 0:
            return done("daily_cap", due=round(due, 6), remaining=remaining,
                        reason=f"đã chi {g['day_spent']['spent']} FLOP hôm nay, trần {day_cap:g}")
        # Nhịp mua bớt máy móc (như flop_pacer): thỉnh thoảng nghỉ cả lượt và co giãn phần đến
        # hạn ±FLOP_PACE_JITTER_PCT. Phần không chi được TÍCH sang lượt sau (window_due cộng
        # dồn) nên tổng chi trong ngày không đổi. Sát hạn thì không nghỉ, để kịp chi hết.
        rnd = rng or random.random
        interval = _env_num("FLOP_GATEWAY_RUN_INTERVAL_SEC", 300)
        near_end = hz - _env_num("FLOP_GATEWAY_MARGIN_SEC", 1800) - now <= 3 * interval
        if not near_end and rnd() * 100 < _env_num("FLOP_GATEWAY_SKIP_PCT", 10):
            return done("pace_wait", due=round(due, 6), remaining=remaining,
                        reason="nghỉ ngẫu nhiên lượt này (nhịp mua bớt đều đặn) — phần đến hạn dồn sang lượt sau")
        jit = max(0.0, min(50.0, _env_num("FLOP_PACE_JITTER_PCT", 20))) / 100
        if jit and not near_end:
            due = min(float(remaining), due * (1 + (rnd() * 2 - 1) * jit))
        if run_cap > 0:
            due = min(due, run_cap)
        if day_cap > 0:
            due = min(due, day_left)
        max_calls = max(1, int(_env_num("FLOP_GATEWAY_MAX_CALLS_PER_RUN", 2)))
        pp, pc, preq = _price(model)
        if pp <= 0 and pc <= 0 and preq <= 0:
            # Không biết giá -> KHÔNG đoán và KHÔNG im lặng chờ mãi: báo rõ, thử lại sau 30 phút.
            backoff(1800, "price_unknown")
            return done("price_unknown", remaining=remaining,
                        reason=(f"catalog không có giá cho {model_id} (pricing keys: "
                                f"{sorted((model.get('pricing') or {}))[:8]}) — đặt FLOP_GATEWAY_FIXED_PRICE "
                                "nếu FLOP công bố giá cố định/lần gọi"))
        # Trần token/lần: mặc định = giới hạn của model (đo devnet 2026-10-24: trần 1024 chỉ chi
        # được ~3.7k/20k quota trong 24h). Lần gọi vẫn được định cỡ theo phần đến hạn.
        cap = _completion_cap(model, int(_env_num("FLOP_GATEWAY_MAX_TOKENS", 0)) or 8192)
        max_wait = int(_env_num("FLOP_GATEWAY_MAX_WAIT_MS", 180000))
        min_tok = int(_env_num("FLOP_GATEWAY_MIN_TOKENS", 64))
        gap = _env_num("FLOP_GATEWAY_CALL_GAP_SEC", 31)        # allowance: 2 yêu cầu/phút
        idx = int(g.get("prompt_idx") or 0)

        def estimate_or_empty(prompt):
            # /models/estimate chỉ để THAM KHẢO (đã có ước tính len//4 dự phòng): endpoint lỗi
            # hay bị gỡ không được chặn việc mua.
            try:
                return estimate(http, base, model_id, prompt, cap)
            except Exception as e:
                log(f"[gateway] /models/estimate lỗi ({str(e)[:80]}) — dùng ước tính độ dài đề")
                return {}

        # Mỗi lần gọi một đề KHÁC: ít đề hơn số lần gọi thì gọi ít lần hơn, không lặp đề.
        pick = [prompts[(idx + i) % len(prompts)] for i in range(min(max_calls, len(prompts)))]
        est = estimate_or_empty(pick[0])
        ptok = int(est.get("estimated_input_tokens") or len(pick[0]) // 4)
        # Giá cố định/lần gọi: số lần gọi quyết định số chi; cỡ yêu cầu không làm đổi giá nên
        # dùng cỡ lớn nhất miner còn nhận (tok_cap học được) — cùng giá, nhiều compute hơn.
        min_cost = preq + ptok * pp + min_tok * pc
        if room is not None:
            if room < min_cost:
                return done("attribution_hold", remaining=remaining,
                            reason=f"chỉ còn {room:.4f} FLOP được mua trước khi chain xác nhận")
            due = min(due, room)
        if due < min_cost or min_cost <= 0:
            return done("pace_wait", due=round(due, 6), remaining=remaining,
                        reason=f"đến hạn {due:.4f} < giá tối thiểu 1 lần gọi {min_cost:.4f} — tích sang run sau")
        n = min(max_calls, max(1, int(due // min_cost)))      # gọi ít mà to, không nhiều mà vụn
        per_call = min(due / n, remaining)
        spent, calls, last, quota_now = 0.0, 0, {}, remaining
        for i, prompt in enumerate(pick[:n]):
            if i:
                est = estimate_or_empty(prompt)
                ptok = int(est.get("estimated_input_tokens") or len(prompt) // 4)
            # Cỡ được miner nhận học dần: devnet 2026-10-24 từ chối 8192 token ("no admitted miner
            # for model and budget" — ước tính 82 block, quá max_wait), nhận 1024.
            size_cap = min(cap, int(g.get("tok_cap") or cap))
            if pc > 0:
                mt = size_call(per_call - preq, ptok, pp, pc, size_cap, min_tok)
            else:
                mt = size_cap if per_call >= preq + ptok * pp else None
            if mt is None:
                break
            # Ngân sách thời gian của LƯỢT (job GitHub bị giết ở 20 phút): không mở lần gọi mới
            # nếu không kịp xong — phần đến hạn dồn sang lượt sau, không mất.
            if deadline and clock() + (gap if calls else 0) + max_wait / 1000 + 15 > deadline:
                log("[gateway] dừng lượt này: không đủ thời gian cho thêm một lần gọi (ngân sách lượt)")
                break
            if calls:
                sleep(gap)
            g["last_mt"] = mt
            # Trần gửi kèm = giá dự tính của CHÍNH lần gọi này (+10%), không phải cả phần đến hạn.
            # (OpenAPI chỉ định nghĩa kiểu của max_cost_flop; server có cưỡng chế hay không CHƯA
            # được kiểm chứng.)
            est_cost = preq + ptok * pp + mt * pc
            max_cost = min(per_call, est_cost) * 1.1
            pending = new_key()
            # Đủ để phát lại ĐÚNG thân request nếu không nhận được phản hồi.
            g["inflight"] = {"key": pending, "ts": now, "max_cost": round(max_cost, 6),
                             "model": model_id, "prompt": prompt, "mt": mt, "max_wait": max_wait}
            if checkpoint:
                state[KEY] = g
                try:
                    checkpoint()
                except Exception as e:
                    log(f"[gateway] không lưu được mốc request treo ({str(e)[:80]})")
            r = chat(http, base, bearer, model_id, prompt, mt, max_cost=max_cost,
                     max_wait_ms=max_wait, idempotency_key=pending)
            pending = None
            g.pop("inflight", None)              # có phản hồi -> kết quả rõ, không còn treo
            u = r.get("usage") if isinstance(r.get("usage"), dict) else {}
            cost, src = _cost(u.get("cost_flop")), "usage"
            # Devnet 2026-10-24: usage KHÔNG có cost_flop (chỉ token). Số đã chi thật = quota
            # gateway giảm bao nhiêu (/keys/me trước/sau) — sổ của CHÍNH gateway, không phải ước tính.
            try:
                after = float(me(http, base, bearer).get("remaining_quota") or 0)
            except Exception:                    # cả lỗi mạng: lần gọi ĐÃ chi, không được làm mất
                after = None
            if after is not None:
                delta = round(quota_now - after, 9)
                quota_now = after
                if cost <= 0 and delta > 0:
                    cost, src = delta, "quota"
            calls += 1
            spent += cost
            charge_day(cost or per_call)                # không rõ số tiền -> tính theo mức tối đa dự kiến
            if answer_fn:
                # Câu trả lời là SẢN PHẨM của lần mua (caller lưu/dùng) — lỗi ở đây không được
                # làm hỏng việc đếm chi.
                try:
                    answer_fn(prompt, answer_text(r), cost)
                except Exception as e:
                    log(f"[gateway] bỏ qua lỗi khi lưu câu trả lời ({str(e)[:80]})")
            last = {"id": r.get("id"), "tokens": u.get("completion_tokens"), "cost": cost,
                    "src": src, "gflops": u.get("gflops_used")}
            if cost <= 0:
                # Không có cost_flop lẫn quota giảm -> KHÔNG ghi sổ theo ước tính; in khoá thật.
                last["usage_keys"] = sorted(u)[:10]
                log(f"[gateway] ⚠️ không xác định được số đã chi (usage keys: {last['usage_keys']})")
            if g.get("tok_cap"):                       # ổn định 12 lần liền -> thử cỡ lớn hơn
                g["tok_ok"] = int(g.get("tok_ok") or 0) + 1
                if g["tok_ok"] >= 12:
                    g["tok_cap"], g["tok_ok"] = probe_up(int(g["tok_cap"]), cap, g.get("tok_bad"),
                                                         g.get("tok_bad_ts"), now), 0
            # Ghi tiến độ NGAY sau mỗi lần chi: lỗi ở lần gọi sau không làm mất số đã chi.
            g["remaining"] = max(0.0, min(quota_now, remaining - spent))
            g["window"] = {**g["window"], "spent": float(g["window"].get("spent") or 0) + cost}
            if spent >= due - 1e-9 or spent >= remaining:
                break
        tally(calls, spent)
        g["last"] = last
        g["remaining"] = max(0.0, min(quota_now, remaining - spent))
        return done("spent" if calls else "pace_wait", calls=calls, spent=round(spent, 6),
                    due=round(due, 6), remaining=g["remaining"], model=model_id, last=last)
    except GatewayError as e:
        out.update(calls=calls, spent=round(spent, 6))   # lần gọi thành công TRƯỚC lỗi vẫn được báo
        if pending and e.status in AMBIGUOUS_STATUS:
            # Giữ g['inflight'] (đã ghi trước lần gọi) -> lượt sau đối soát theo biên nhận.
            tally(calls, spent, n_sent=calls + 1)
            charge_day((g.get("inflight") or {}).get("max_cost"))
            backoff(900, f"{e.status} {e.code}")
            return done("unknown_outcome",
                        reason=(f"HTTP {e.status} khi chờ phản hồi — có thể server đã xử lý và trừ tiền; "
                                f"giữ request {pending[:8]}… để đối soát, KHÔNG gửi lại đề này"))
        if pending:
            g.pop("inflight", None)              # server ĐÃ trả lời (mã lỗi) -> không còn treo
        if calls:
            tally(calls, spent)
        if e.status == 401:
            g.pop("vault", None)                              # bearer chết -> enroll lại run sau
            return done("auth_reset", reason=f"401 {e.code} — enroll lại ở run sau")
        if e.status == 402 and "exhausted" in e.code:
            backoff(3600, f"402 {e.code}")                    # allowance hết, không phải đòi trả tiền
            return done("exhausted", reason=f"402 {e.code} {e.message}".strip())
        if e.status == 402:
            pay = {k: e.data.get(k) for k in ("error", "price", "asset", "destination",
                                              "timeout_blocks", "hash_lock") if k in e.data}
            backoff(3600, f"402 {e.code}")
            # x402: KHÔNG tự trả. Ghi lại hình dạng để chỉnh adapter bằng tay.
            return done("payment_required", reason=f"402 {e.code or ''} {e.message}".strip(), payment=pay)
        if e.status == 403:
            backoff(3600, f"403 {e.code}")
            return done("unfunded" if "unfunded" in e.code else "forbidden",
                        reason=f"403 {e.code} {e.message}".strip())
        if e.status == 503 and "no admitted miner" in e.message.lower() and g.get("last_mt"):
            # Không miner nào nhận yêu cầu CỠ này -> lần sau gửi nửa cỡ (không phải mạng quá tải).
            bad = int(g["last_mt"])
            if not g.get("tok_bad") or bad < int(g["tok_bad"]):
                g["tok_bad"], g["tok_bad_ts"] = bad, now      # cỡ NHỎ NHẤT từng bị từ chối
            g["tok_cap"] = max(int(_env_num("FLOP_GATEWAY_MIN_TOKENS", 64)) * 2, bad // 2)
            g["tok_ok"] = 0
            backoff(60, "no_admitted_miner")
            return done("no_miner", reason=f"503 {e.code}: {e.message[:120]} — lần sau tối đa {g['tok_cap']} token")
        if e.status in (429, 503):
            backoff(e.retry_after or 600, f"{e.status} {e.code}")
            return done("rate_limited", reason=f"{e.status} {e.code} — chờ {e.retry_after or 600}s")
        backoff(900, f"{e.status} {e.code}")
        return done("error", reason=str(e)[:200])
    except Exception as e:
        unknown = 1 if pending else 0            # timeout/mất kết nối GIỮA lần gọi -> không rõ kết quả
        if calls or unknown:
            out.update(calls=calls, spent=round(spent, 6))
            tally(calls, spent, n_sent=calls + unknown)
        if unknown:
            charge_day((g.get("inflight") or {}).get("max_cost"))
        backoff(900, type(e).__name__)
        if unknown:
            return done("unknown_outcome",
                        reason=(f"{type(e).__name__} khi chờ phản hồi — có thể server đã xử lý và trừ "
                                f"tiền; giữ request {pending[:8]}… để đối soát, KHÔNG gửi lại đề này"))
        return done("error", reason=f"{type(e).__name__}: {str(e)[:160]}")
