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
  4. GET  /keys/me (quota còn lại) · GET /keys/me/receipts (biên nhận ĐÃ LÊN CHAIN:
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
          timeout: float = 20, ok=(200, 201)) -> dict:
    headers = {"Accept": "application/json"}
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
         max_cost: float, max_wait_ms: int = 60000) -> dict:
    return _call(http, "POST", base + "/chat/completions", bearer=bearer, timeout=max_wait_ms / 1000 + 15,
                 body={"model": model, "max_tokens": int(max_tokens),
                       "messages": [{"role": "user", "content": prompt}],
                       "max_cost_flop": round(float(max_cost), 6), "max_wait_ms": int(max_wait_ms)})


# --- nhịp chi: rải quota còn lại tới lúc hết hạn ----------------------------------------

def horizon(now: int, expires_at_ms) -> int:
    """Mốc phải chi hết: hạn của allowance (ms) nếu có, không thì cuối ngày UTC."""
    try:
        exp = int(float(expires_at_ms)) // 1000
        if exp > now:
            return exp
    except (TypeError, ValueError):
        pass
    return now - now % 86400 + 86400


def window_due(win, remaining: float, now: int, horizon_ts: int, interval: float,
               margin: float):
    """-> (due, win). Rải quota ĐỀU tới hạn theo MỤC TIÊU TUYẾN TÍNH có cộng dồn (như
    flop_pacer): cửa sổ [start, hạn - biên], total = quota lúc mở cửa sổ; mục tiêu tới run
    này = total * (now + interval - start) / (end - start); due = mục tiêu - đã chi.
    Phần nhỏ chưa đủ 1 lần gọi được TÍCH LẠI sang run sau (không bị bỏ).
    Mở cửa sổ mới khi hạn đổi (allowance/ngày mới) hoặc quota TĂNG (faucet nạp)."""
    win = dict(win) if isinstance(win, dict) else {}
    spent = float(win.get("spent") or 0)
    if win.get("horizon") != horizon_ts or remaining > float(win.get("total") or 0) - spent + 1e-9:
        win, spent = {"start": now, "horizon": horizon_ts, "total": float(remaining), "spent": 0.0}, 0.0
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
    p = (model or {}).get("pricing") or {}
    try:
        return float(p.get("prompt") or 0), float(p.get("completion") or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0


def _completion_cap(model: dict, default: int) -> int:
    for src in ((model or {}).get("per_request_limits") or {}, (model or {}).get("top_provider") or {}):
        for k in ("completion_tokens", "max_completion_tokens"):
            try:
                if src.get(k):
                    return min(default, int(src[k]))
            except (TypeError, ValueError):
                continue
    return default


def _receipt_key(r: dict) -> str:
    return str(r.get("request_id") or r.get("tx_hash") or r.get("channel_id")
               or f"{r.get('block_number')}:{r.get('prompt_hash')}")[:96]


# Biên nhận THẬT của devnet (2026-10-24) KHÔNG theo schema InferenceReceipt của OpenAPI: không có
# block_number/tx_hash mà có channel_id, request_id, reserved_spend, settled_spend(_raw), state,
# response_status, terminal_event. "Đã settle" = state thuộc nhóm dưới / có block_number / có
# settled_spend > 0 mà state không thuộc nhóm chưa-xong.
_SETTLED_STATES = {"settled", "finalized", "final", "complete", "completed", "success", "succeeded", "paid"}
_OPEN_STATES = {"pending", "estimated", "reserved", "open", "opened", "in_flight", "inflight", "running", "submitted",
                "failed", "error", "refunded", "released", "cancelled", "canceled", "expired", "rejected"}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def settled_amount(rc: dict):
    """FLOP đã settle của 1 biên nhận, hoặc None nếu chưa settle."""
    st = str(rc.get("state") or "").strip().lower()
    amt = _num(rc.get("settled_spend"))
    if rc.get("block_number") is not None or st in _SETTLED_STATES:
        return amt or 0.0
    if amt and amt > 0 and st not in _OPEN_STATES:
        return amt
    return None


# --- một nhịp ---------------------------------------------------------------------------

def run_step(state: dict, *, agent_key, http, now: int, prompts: list, expect_network: str,
             book_fn=None, sleep=time.sleep, log=print) -> dict:
    """Một nhịp mua compute. Trả {"outcome", "calls", "spent", ...}; KHÔNG ném lỗi.

    `book_fn(amount, ref, model)` ghi sổ (chỉ khi mạng khớp `expect_network`); None = chạy
    tập trên devnet, không ghi sổ. Trạng thái bền trong state[KEY]: két bearer, model, quota,
    hạn, mốc chờ (Retry-After), biên nhận đã thấy, thống kê."""
    g = state.get(KEY) if isinstance(state.get(KEY), dict) else {}
    g = dict(g)
    out = {"outcome": "?", "calls": 0, "spent": 0.0}
    calls, spent = 0, 0.0                    # giữ được cả khi lần gọi sau lỗi (báo đúng số đã gọi)

    def done(outcome, **kw):
        out.update(outcome=outcome, **kw)
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
    if net != expect_network:
        return done("network_mismatch", reason=f"gateway báo '{net}', cần '{expect_network}' — không chi")

    def backoff(sec, why):
        g["wait_until"], g["wait_why"] = now + int(max(60, sec)), why[:120]

    try:
        bearer = open_bearer(agent_key, g.get("vault"))
        if bearer and g.get("expires_at") and int(float(g["expires_at"])) // 1000 <= now + 120:
            bearer = None                                     # sắp hết hạn -> enroll lại (không hạn -> giữ)
        if not bearer:
            e = enroll(http, base, wkey, _env("FLOP_GATEWAY_PURPOSE") or None)
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
        if model_id not in models:
            # Key gắn với 1 model; model rời catalog -> không gửi slug không được quảng bá.
            backoff(1800, "model_not_advertised")
            return done("model_unavailable", reason=f"model {model_id} không còn trong catalog")
        model = models[model_id]

        def collect(prev_calls: int) -> dict:
            """Đọc biên nhận; ghi sổ (book_fn) MỖI biên nhận mới ĐÃ SETTLE theo settled_spend —
            đúng thước đo "compute purchased in settled sessions". Dedupe theo request_id."""
            seen = list(g.get("receipts_seen") or [])
            new, amt, hist = [], 0.0, {}
            try:
                items, shape = receipts_detail(http, base, bearer)
            except GatewayError as e:
                items, shape = [], f"HTTP {e.status} {e.code}".strip()
            for rc in items:
                key = f"{rc.get('state')}/{rc.get('response_status')}"
                hist[key] = hist.get(key, 0) + 1
                k = _receipt_key(rc)
                a = settled_amount(rc)
                if a is None or k in seen:
                    continue
                seen.append(k)
                new.append(rc)
                amt += a
                if book_fn and a > 0:
                    book_fn(a, k[:80], str(rc.get("model") or model_id))
            g["receipts_seen"] = seen[-200:]
            st = dict(g.get("stats") or {})
            st["receipts"] = int(st.get("receipts") or 0) + len(new)
            st["settled"] = round(float(st.get("settled") or 0) + amt, 6)
            g["stats"] = st
            if prev_calls or new or not st["receipts"]:      # chưa từng thấy biên nhận -> luôn in hình dạng
                log(f"[gateway] biên nhận: settled +{len(new)} ({round(amt, 6)} FLOP) | "
                    f"{', '.join(f'{k}={v}' for k, v in sorted(hist.items())) or shape}")
            return {"receipts_new": len(new), "settled_new": round(amt, 6),
                    "receipts_total": st["receipts"], "settled_total": st["settled"],
                    "miners": [str(x.get("miner") or "") for x in new if x.get("miner")]}

        # Đọc biên nhận TRƯỚC khi gọi: biên nhận settle muộn (của các run trước) luôn được ghi,
        # kể cả khi lần gọi run này lỗi (429/503) hay chưa tới nhịp.
        rs = collect(int(g.get("last_calls") or 0))
        g["last_calls"] = 0
        if remaining <= 0:
            backoff(1800, "quota_exhausted")
            return done("exhausted", reason="quota gateway đã hết", **rs)

        hz = horizon(now, g.get("expires_at"))
        due, win = window_due(g.get("window"), remaining, now, hz,
                              _env_num("FLOP_GATEWAY_RUN_INTERVAL_SEC", 300),
                              _env_num("FLOP_GATEWAY_MARGIN_SEC", 1800))
        g["window"] = win
        max_calls = max(1, int(_env_num("FLOP_GATEWAY_MAX_CALLS_PER_RUN", 2)))
        pp, pc = _price(model)
        # Trần token/lần: mặc định = giới hạn của model (đo devnet 2026-10-24: trần 1024 chỉ chi
        # được ~3.7k/20k quota trong 24h). Lần gọi vẫn được định cỡ theo phần đến hạn.
        cap = _completion_cap(model, int(_env_num("FLOP_GATEWAY_MAX_TOKENS", 0)) or 8192)
        max_wait = int(_env_num("FLOP_GATEWAY_MAX_WAIT_MS", 180000))
        min_tok = int(_env_num("FLOP_GATEWAY_MIN_TOKENS", 64))
        gap = _env_num("FLOP_GATEWAY_CALL_GAP_SEC", 31)        # allowance: 2 yêu cầu/phút
        idx = int(g.get("prompt_idx") or 0)
        pick = [prompts[(idx + i) % len(prompts)] for i in range(max_calls)]
        est = estimate(http, base, model_id, pick[0], cap)
        ptok = int(est.get("estimated_input_tokens") or len(pick[0]) // 4)
        min_cost = ptok * pp + min_tok * pc
        if due < min_cost or min_cost <= 0:
            return done("pace_wait", due=round(due, 6), remaining=remaining, **rs,
                        reason=f"đến hạn {due:.4f} < giá tối thiểu 1 lần gọi {min_cost:.4f} — tích sang run sau")
        n = min(max_calls, max(1, int(due // min_cost)))      # gọi ít mà to, không nhiều mà vụn
        per_call = min(due / n, remaining)
        spent, calls, last, quota_now = 0.0, 0, {}, remaining
        for i, prompt in enumerate(pick[:n]):
            if i:
                est = estimate(http, base, model_id, prompt, cap)
                ptok = int(est.get("estimated_input_tokens") or len(prompt) // 4)
            # Cỡ được miner nhận học dần: devnet 2026-10-24 từ chối 8192 token ("no admitted miner
            # for model and budget" — ước tính 82 block, quá max_wait), nhận 1024.
            mt = size_call(per_call, ptok, pp, pc, min(cap, int(g.get("tok_cap") or cap)), min_tok)
            if mt is None:
                break
            if calls:
                sleep(gap)
            g["last_mt"] = mt
            r = chat(http, base, bearer, model_id, prompt, mt, max_cost=per_call * 1.1,
                     max_wait_ms=max_wait)
            u = r.get("usage") or {}
            cost, src = float(u.get("cost_flop") or 0), "usage"
            # Devnet 2026-10-24: usage KHÔNG có cost_flop (chỉ token). Số đã chi thật = quota
            # gateway giảm bao nhiêu (/keys/me trước/sau) — sổ của CHÍNH gateway, không phải ước tính.
            try:
                after = float(me(http, base, bearer).get("remaining_quota") or 0)
            except GatewayError:
                after = None
            if after is not None:
                delta = round(quota_now - after, 9)
                quota_now = after
                if cost <= 0 and delta > 0:
                    cost, src = delta, "quota"
            calls += 1
            spent += cost
            last = {"id": r.get("id"), "tokens": u.get("completion_tokens"), "cost": cost,
                    "src": src, "gflops": u.get("gflops_used")}
            if cost <= 0:
                # Không có cost_flop lẫn quota giảm -> KHÔNG ghi sổ theo ước tính; in khoá thật.
                last["usage_keys"] = sorted(u)[:10]
                log(f"[gateway] ⚠️ không xác định được số đã chi (usage keys: {last['usage_keys']})")
            if g.get("tok_cap"):                       # ổn định 12 lần liền -> thử cỡ lớn hơn 25%
                g["tok_ok"] = int(g.get("tok_ok") or 0) + 1
                if g["tok_ok"] >= 12:
                    g["tok_cap"] = min(cap, int(g["tok_cap"] * 1.25)) if g["tok_cap"] * 1.25 < cap else None
                    g["tok_ok"] = 0
            # Ghi tiến độ NGAY sau mỗi lần chi: lỗi ở lần gọi sau không làm mất số đã chi.
            g["remaining"] = max(0.0, min(quota_now, remaining - spent))
            g["window"] = {**g["window"], "spent": float(g["window"].get("spent") or 0) + cost}
            if spent >= due - 1e-9 or spent >= remaining:
                break
        g["prompt_idx"] = (idx + calls) % max(1, len(prompts) or 1)

        g["last_calls"] = calls
        st = dict(g.get("stats") or {})
        st["calls"] = int(st.get("calls") or 0) + calls
        st["spent"] = round(float(st.get("spent") or 0) + spent, 6)
        g["stats"] = st
        g["last"] = last
        g["remaining"] = max(0.0, min(quota_now, remaining - spent))
        return done("spent" if calls else "pace_wait", calls=calls, spent=round(spent, 6),
                    due=round(due, 6), remaining=g["remaining"], model=model_id, last=last, **rs)
    except GatewayError as e:
        out.update(calls=calls, spent=round(spent, 6))   # lần gọi thành công TRƯỚC lỗi vẫn được báo
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
            g["tok_cap"] = max(int(_env_num("FLOP_GATEWAY_MIN_TOKENS", 64)) * 2, int(g["last_mt"]) // 2)
            g["tok_ok"] = 0
            backoff(60, "no_admitted_miner")
            return done("no_miner", reason=f"503 {e.code}: {e.message[:120]} — lần sau tối đa {g['tok_cap']} token")
        if e.status in (429, 503):
            backoff(e.retry_after or 600, f"{e.status} {e.code}")
            return done("rate_limited", reason=f"{e.status} {e.code} — chờ {e.retry_after or 600}s")
        backoff(900, f"{e.status} {e.code}")
        return done("error", reason=str(e)[:200])
    except Exception as e:
        backoff(900, type(e).__name__)
        return done("error", reason=f"{type(e).__name__}: {str(e)[:160]}")
