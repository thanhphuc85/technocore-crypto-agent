"""
flop_agent_claim.py — nhận FLOP TESTNET qua làn agent của faucet (GATED, mặc định tắt).

Theo hướng dẫn CHÍNH THỨC (flop.network/llms.txt, mục "Get Testnet FLOP", 2026-11-25):
  1. GET <faucet>/chain phải báo network=testnet và genesisHash = hash block 0 của testnet.
  2. GET <faucet>/limits phải có faucet.status=open và faucet.agent.claimPath.
  3. Ký bằng KHOÁ CỦA CHÍNH VÍ chuỗi UTF-8 `flop-faucet-agent/v1|<genesis>|<address>|<nonce>`
     (nonce thập phân, tối đa 19 chữ số).
  4. POST <faucet><claimPath> JSON {"address", "nonce", "signature": "0x<hex>"}.
Gửi lại CÙNG nonce trả cùng giao dịch, không bao giờ trả hai lần -> nonce chỉ đổi khi yêu cầu trước
đã có kết quả cuối (cấp / từ chối). 10 FLOP mỗi ví mỗi 24 giờ; lần sau cần đã chi on-chain lần trước.

Ví = khoá ed25519 (DID). Ký thô trước; faucet báo sai chữ ký thì thử MỘT lần bản bọc `<Bytes>..</Bytes>`
(kiểu signRaw của polkadot-js, network#1255) với nonce mới; cả hai sai -> dừng 7 ngày và báo, không gõ lặp.

KHÔNG ghi sổ cái: sổ testnet chỉ bật cùng TESTNET_ENABLED (cùng lúc gateway testnet). Số dư thật nằm trên chain;
module chỉ lưu lịch sử claim (tx) trong state để báo cáo.

Env (Repo Variables):
  FLOP_FAUCET_AGENT_CLAIM      on -> bật (mặc định tắt).
  FLOP_FAUCET_TESTNET_URL      gốc faucet testnet (mặc định https://faucet.flop.network).
  FLOP_FAUCET_TESTNET_GENESIS  genesis testnet phải khớp (mặc định hash công bố 2026-11-24).
"""

import os
import time

KEY = "testnet_claim"
DEFAULT_URL = "https://faucet.flop.network"
DEFAULT_GENESIS = "0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800"
PREFIX = "flop-faucet-agent/v1"
DAY = 86400
WAIT_MAX = 2 * DAY                 # hẹn xa hơn 2 ngày = rác, kẹp lại
SIG_FAIL_PAUSE = 7 * DAY
REFUSE_MIN = 6 * 3600              # bị từ chối không kèm giờ chờ -> thử lại sau ≥ 6 giờ
NETWORK_RETRY = 900                # lỗi mạng / 5xx -> 15 phút, GIỮ nonce (gửi lại an toàn)
CLOSED_RETRY = 1800                # faucet đóng / hết tiền -> 30 phút
MAX_AMOUNT = 1000.0                # làn agent cấp 10; số lớn hơn mức này là đơn vị sai -> không tin
# Nonce chưa có kết quả cuối (5xx/mạng) giữ tối đa chừng này rồi đổi nonce mới. Testnet 26/11: faucet trả 502 cho
# mọi lần gửi nhưng VẪN chuyển 10 FLOP lúc 19:25Z -> agent giữ nonce cũ mãi, không biết đã nhận. Đổi nonce an toàn:
# faucet tự giới hạn 10 FLOP / ví / 24 giờ nên không thể trả hai lần.
NONCE_MAX_AGE = 6 * 3600
_TRANSFERS = {"transfer", "transferkeepalive", "transferallowdeath"}
_BATCHES = {"batch", "batchall", "forcebatch"}
_PLANCK = 10 ** 18


def enabled() -> bool:
    return os.environ.get("FLOP_FAUCET_AGENT_CLAIM", "").strip().lower() in ("1", "true", "on", "yes")


def base_url() -> str:
    return (os.environ.get("FLOP_FAUCET_TESTNET_URL", "").strip() or DEFAULT_URL).rstrip("/")


def expected_genesis() -> str:
    return (os.environ.get("FLOP_FAUCET_TESTNET_GENESIS", "").strip() or DEFAULT_GENESIS).lower()


def message(genesis: str, address: str, nonce: str) -> bytes:
    return f"{PREFIX}|{genesis}|{address}|{nonce}".encode("utf-8")


def sign(wallet_priv, msg: bytes, wrapped: bool) -> str:
    data = b"<Bytes>" + msg + b"</Bytes>" if wrapped else msg
    return "0x" + wallet_priv.sign(data).hex()


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _wait_s(resp_headers, body) -> int:
    """Giây chờ do faucet báo (retryAfterSeconds / Retry-After), kẹp [0, WAIT_MAX]; không có -> None."""
    v = _num((body or {}).get("retryAfterSeconds")) if isinstance(body, dict) else None
    if v is None:
        v = _num((resp_headers or {}).get("Retry-After"))
    return None if v is None else int(max(0, min(v, WAIT_MAX)))


def _new_nonce(rec: dict, now: float) -> str:
    n = int(now * 1000)
    try:
        n = max(n, int(rec.get("last_nonce") or 0) + 1)
    except (TypeError, ValueError):
        pass
    return str(n)


def _same_account(a: str, b: str) -> bool:
    a, b = str(a or ""), str(b or "")
    if a == b:
        return bool(a)
    try:
        import flop_gateway as fg
        pa, pb = fg.account_pubkey(a), fg.account_pubkey(b)
        return pa is not None and pa == pb
    except Exception:
        return False


def _planck(v):
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def incoming_grants(txs, address: str) -> list:
    """Các khoản FLOP NGƯỜI KHÁC chuyển vào `address` trong giao dịch explorer (/api/accounts/<ví>/transactions):
    balances.transfer* trực tiếp hoặc nằm trong utility.batch/batchAll (faucet gộp nhiều ví một giao dịch).
    Trả [{"ts": giây, "amount": FLOP, "tx": hash}]."""
    out = []
    for t in txs or []:
        if not isinstance(t, dict) or t.get("success") is not True or _same_account(t.get("sender"), address):
            continue
        try:
            ts = int(t.get("timestamp")) // 1000
        except (TypeError, ValueError):
            continue
        sec, meth, params = str(t.get("section") or "").lower(), str(t.get("method") or "").lower(), t.get("params")
        calls = []
        if sec == "utility" and meth in _BATCHES and isinstance(params, list) and params and isinstance(params[0], list):
            calls = [c for c in params[0] if isinstance(c, dict)]
        elif sec == "balances" and meth in _TRANSFERS:
            calls = [{"section": sec, "method": meth, "args": params}]
        total = 0
        for c in calls:
            if str(c.get("section") or "").lower() != "balances" or str(c.get("method") or "").lower() not in _TRANSFERS:
                continue
            a = c.get("args")
            if isinstance(a, list) and len(a) >= 2:
                dest, value = a[0], a[1]
            elif isinstance(a, dict):
                dest, value = a.get("dest"), a.get("value")
            else:
                continue
            if isinstance(dest, dict):
                dest = dest.get("Id") or dest.get("id")
            v = _planck(value)
            if v and v > 0 and _same_account(dest, address):
                total += v
        if total:
            out.append({"ts": ts, "amount": total / _PLANCK, "tx": str(t.get("hash") or "")[:80]})
    return out


def _trim(rec: dict) -> dict:
    rec["claims"] = list(rec.get("claims") or [])[-10:]
    return rec


def run_step(rec, wallet_priv, address: str, now: float, http_get, http_post, grants_fn=None):
    """Một lượt. `http_get(url) -> (status, json|None)`, `http_post(url, body) -> (status, json|None, headers)`.
    `grants_fn(address)` -> giao dịch explorer gần đây của ví (None = không kiểm on-chain).
    Trả (rec mới, outcome, alert|None). Không ném lỗi mạng ra ngoài; rec luôn có `at` (luật blob bền)."""
    rec = dict(rec) if isinstance(rec, dict) else {}
    now_i = int(now)
    if now_i < int(rec.get("next_ts") or 0):
        return rec, "wait", None
    base = base_url()

    def done(outcome, alert=None, retry=None):
        if retry is not None:
            rec["next_ts"] = now_i + int(retry)
        rec["at"] = now_i
        rec["last_outcome"] = outcome
        return _trim(rec), outcome, alert

    # Yêu cầu trước CHƯA có kết quả cuối (5xx/mạng, nonce còn giữ): faucet có thể đã chuyển tiền dù trả lỗi.
    # Kiểm on-chain: có khoản người khác chuyển vào ví từ lúc tạo nonce (nonce = mili giây lúc tạo) -> coi là đã nhận.
    pend = str(rec.get("nonce") or "")
    if pend:
        try:
            born = int(pend) // 1000
        except ValueError:
            born = None
        if grants_fn is not None and born is not None:
            known = {c.get("tx") for c in rec.get("claims") or [] if isinstance(c, dict)}
            try:
                gs = incoming_grants(grants_fn(address), address)
            except Exception:
                gs = []
            hit = sorted((g for g in gs if g["ts"] >= born - 120 and g["tx"] and g["tx"] not in known
                          and 0 < g["amount"] <= MAX_AMOUNT), key=lambda g: g["ts"])
            if hit:
                g = hit[0]
                rec["nonce"] = None
                rec["last_ok_ts"] = g["ts"]
                rec.setdefault("claims", []).append({"ts": g["ts"], "amount": g["amount"], "tx": g["tx"],
                                                     "tier": "onchain"})
                return done("claimed-onchain", retry=max(60, g["ts"] + DAY + 120 - now_i),
                            alert=(f"✅ Faucet testnet: phản hồi lỗi nhưng {g['amount']:g} FLOP đã vào ví "
                                   f"{address[:8]}…{address[-4:]} (tx {g['tx'][:18]}…) — ghi nhận, xin lại sau 24 giờ."))
        if born is not None and now_i - born > NONCE_MAX_AGE:
            rec["nonce"] = None                        # quá lâu không có kết quả cuối: nonce mới (faucet chặn trả 2 lần)

    try:
        cs, chain = http_get(f"{base}/chain")
        ls, lim = http_get(f"{base}/limits")
    except Exception as e:
        return done(f"error-read:{type(e).__name__}", retry=NETWORK_RETRY)
    chain = chain if isinstance(chain, dict) else {}
    gen = str(chain.get("genesisHash") or "").lower()
    if cs != 200 or chain.get("network") != "testnet" or gen != expected_genesis():
        # Faucet đổi mạng / genesis khác -> KHÔNG ký (ký là cam kết địa chỉ cho chuỗi đó).
        return done(f"chain-mismatch:{chain.get('network')}:{gen[:10]}", retry=3600)
    fau = (lim or {}).get("faucet") if isinstance(lim, dict) else None
    fau = fau if isinstance(fau, dict) else {}
    agent = fau.get("agent") if isinstance(fau.get("agent"), dict) else {}
    path = agent.get("claimPath")
    if ls != 200 or fau.get("status") != "open" or not isinstance(path, str) or not path.startswith("/"):
        return done(f"faucet-closed:{fau.get('status')}", retry=CLOSED_RETRY)

    wrapped = rec.get("variant") == "wrapped"
    for _attempt in range(2):
        nonce = str(rec.get("nonce") or "") or _new_nonce(rec, now)
        rec["nonce"] = nonce
        rec["last_nonce"] = nonce
        sig = sign(wallet_priv, message(gen, address, nonce), wrapped)
        try:
            st, body, hdr = http_post(f"{base}{path}", {"address": address, "nonce": nonce, "signature": sig})
        except Exception as e:
            return done(f"error-post:{type(e).__name__}", retry=NETWORK_RETRY)     # giữ nonce: gửi lại an toàn
        body = body if isinstance(body, dict) else {}
        reason = str(body.get("reason") or "")[:60]
        wait = _wait_s(hdr, body)
        if st == 200 and body.get("success") is True:
            amt = _num(body.get("amount"))
            tx = str(body.get("txHash") or "")[:80]
            rec["nonce"] = None
            rec["variant"] = "wrapped" if wrapped else "raw"
            rec["last_ok_ts"] = now_i
            rec.setdefault("claims", []).append({"ts": now_i, "amount": amt if amt is not None and amt <= MAX_AMOUNT
                                                 else None, "tx": tx, "tier": str(body.get("tier") or "")[:20]})
            shown = f"{amt:g}" if amt is not None and amt <= MAX_AMOUNT else "?"
            return done("claimed", alert=(f"✅ Faucet testnet: nhận {shown} FLOP vào ví {address[:8]}…{address[-4:]} "
                                          f"(tx {tx[:18]}…). Chưa có gateway testnet nên FLOP nằm chờ trong ví."),
                        retry=DAY + 120)
        if st == 401 and reason == "claim_signature_invalid":
            rec["nonce"] = None                                  # kết quả cuối -> nonce mới cho lượt sau
            if not wrapped:
                wrapped = True                                   # thử MỘT lần bản <Bytes>
                continue
            rec["variant"] = None
            return done("sig-unsupported", retry=SIG_FAIL_PAUSE,
                        alert=("⚠️ Faucet testnet từ chối chữ ký của ví ed25519 (cả bản thô lẫn bản <Bytes>). "
                               "Agent tạm dừng xin 7 ngày; cần FLOP xác nhận faucet có nhận ví ed25519 không."))
        if st >= 500 or st == 0 or reason in ("indexer_unavailable", "retry_later"):
            # Chưa có kết quả cuối -> GIỮ nonce (faucet hứa gửi lại cùng nonce không trả hai lần).
            return done(f"retry:{st}:{reason}",
                        retry=max(60, wait) if wait is not None else (300 if reason else NETWORK_RETRY))
        # 4xx khác / 429: kết quả cuối -> nonce mới lần sau; chờ đúng giờ faucet hẹn, không có thì ≥ 6 giờ.
        rec["nonce"] = None
        first = rec.get("refused_reason") != reason
        rec["refused_reason"] = reason
        msg = str(body.get("error") or "")[:160]
        return done(f"refused:{st}:{reason}",
                    retry=max(60, wait) if wait is not None else (3600 if st == 429 else REFUSE_MIN),
                    alert=(f"ℹ️ Faucet testnet từ chối ({st} {reason}): {msg}" if first and st != 429 else None))
    return done("sig-unsupported", retry=SIG_FAIL_PAUSE)
