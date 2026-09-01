"""
flop_session.py — Mua suy luận từ thợ đào FLOP: ĐƯỜNG EARN AIRDROP CHÍNH (3:1).

Theo bản nháp FLOP (intro.flop.network/agent.html, 27/08/2026): một tác nhân đăng YÊU CẦU
PHIÊN vào mempool với 5 trường — [hash trọng số mô hình · độ trễ tối đa · số FLOPs · cờ bảo
mật · phí]. Một thợ đào nhận, mở kết nối riêng, chạy inference, trả về PoUI (Proof of Useful
Inference). Tác nhân có thể KHIẾU NẠI (dispute) nếu sai; nếu đúng thì THANH TOÁN bằng FLOP.
Airdrop = pro-rata theo FLOP chi cho inference (mỗi 3 FLOP phí -> mở khóa 1 FLOP).

Đây là SCAFFOLD (chưa có testnet FLOP). Phần thanh toán đi qua token_manager.spend() nên khi
TESTNET_ENABLED=true + endpoint thật thì mỗi phiên đã chốt sẽ tự tích lũy mở khóa 3:1. Ở
SIMULATION, một "thợ đào giả" trả PoUI giả để chạy trọn happy-path (build -> submit -> verify
-> settle), nhưng chi mô phỏng KHÔNG tích lũy mở khóa (đúng: chỉ chi THẬT mới tính).

Cùng tinh thần repo: đọc env LIVE, KHÔNG raise top-level, mỗi nhánh trả outcome rõ ràng.
Gate agent: FLOP_SESSION_ENABLED (mặc định TẮT). Chạy thử: python flop_session.py

TRUNG THỰC VỀ PoUI: verify_poui() ở đây CHỈ kiểm tính liên kết + hiện diện (poui gắn đúng
phiên, có commitment + chữ ký thợ đào), KHÔNG kiểm tính đúng mật mã của commitment-activations
(cần spec thật của FLOP). Đánh dấu rõ để không ai tưởng đã xác minh mật mã đầy đủ.
"""

import hashlib
import os
import time

import token_manager as tm

# Ký Ed25519 (import mềm như token_manager) — bằng chứng ý định cho yêu cầu phiên.
try:
    from agent_cron import sign_message
except Exception:  # pragma: no cover
    sign_message = None


def session_enabled() -> bool:
    """Gate mức agent (mặc định TẮT -> agent 24/7 không đổi hành vi)."""
    return os.environ.get("FLOP_SESSION_ENABLED", "").strip().lower() in ("1", "true", "on", "yes")


def mempool_url() -> str:
    """Endpoint mempool để đăng yêu cầu phiên (nạp khi FLOP công bố)."""
    return os.environ.get("FLOP_MEMPOOL_URL", "").strip() or tm.endpoint_url()


# --- Yêu cầu phiên (5 trường) ------------------------------------------------------

def build_request(model_hash: str, max_latency_ms: int, flops: int,
                  security_flags=None, fee: str = None, *, token: str = None,
                  private_key=None, did: str = None, nonce: str = None) -> dict:
    """Dựng yêu cầu phiên đúng 5 trường + id (hash canonical) + chữ ký ý định. Trả dict;
    KHÔNG gửi đi đâu (đăng mempool là việc của submit_request). Ném ValueError nếu tham số
    vô lý (đây là lỗi lập trình của caller, không phải sự kiện runtime)."""
    token = token or tm.default_token()
    if not (model_hash and str(model_hash).strip()):
        raise ValueError("model_hash (hash trọng số mô hình) không được rỗng")
    flops = int(flops)
    max_latency_ms = int(max_latency_ms)
    if flops <= 0 or max_latency_ms <= 0:
        raise ValueError("flops và max_latency_ms phải là số nguyên dương")
    fee = str(fee if fee is not None else os.environ.get("FLOP_INFERENCE_COST", "").strip() or "0.001")
    flags = sorted({str(f).strip() for f in (security_flags or []) if str(f).strip()})
    nonce = nonce or str(int(time.time() * 1000))
    canonical = f"{did or ''}|{model_hash}|{max_latency_ms}|{flops}|{','.join(flags)}|{fee}|{nonce}"
    req_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    sig = sign_message(private_key, canonical) if (sign_message and private_key and did) else None
    return {
        "id": req_id, "token": token, "did": did,
        "model_hash": model_hash, "max_latency_ms": max_latency_ms, "flops": flops,
        "security_flags": flags, "fee": fee, "nonce": nonce, "sig": sig,
    }


# --- Đăng mempool + nhận thợ đào (seam) --------------------------------------------

def _mock_poui(request: dict, miner_did: str) -> dict:
    """PoUI GIẢ cho simulation: commitment = hash(id|model_hash|nonce) để verify liên kết được."""
    commit = hashlib.sha256(
        f"{request['id']}|{request['model_hash']}|{request['nonce']}".encode("utf-8")).hexdigest()
    return {"session_id": request["id"], "miner_did": miner_did,
            "commitment": commit, "miner_sig": f"mock-sig:{commit[:16]}"}


def submit_request(request: dict, *, submit_fn=None, log=print) -> dict:
    """Đăng yêu cầu phiên vào mempool và nhận thợ đào.
      - SIMULATION: trả 1 thợ đào GIẢ + PoUI giả (chạy trọn happy-path, không mạng).
      - TESTNET: gửi qua submit_fn tới FLOP_MEMPOOL_URL. submit_fn trả {session_id, miner_did,
        poui?}. Thiếu endpoint/submit_fn -> skipped_unconfigured (không bịa thợ đào).
    Outcome: accepted (kèm 'accept'={session_id,miner_did,poui?}) / skipped_unconfigured /
    error_submit / no_miner."""
    mode = tm.ledger_mode()
    if mode == "testnet":
        url = mempool_url()
        if not url or submit_fn is None:
            return {"outcome": "skipped_unconfigured",
                    "reason": ("TESTNET_ENABLED=true nhưng thiếu FLOP_MEMPOOL_URL — từ chối đăng "
                               "tới endpoint đoán mò") if not url else
                              "TESTNET_ENABLED=true nhưng chưa tiêm submit_fn — từ chối bịa thợ đào"}
        try:
            accept = submit_fn({"action": "session_request", "url": url, "request": request}) or {}
        except Exception as e:
            return {"outcome": "error_submit", "reason": f"đăng mempool thất bại: {e}"}
        if not accept.get("session_id") or not accept.get("miner_did"):
            return {"outcome": "no_miner", "reason": "không thợ đào nào nhận phiên"}
        return {"outcome": "accepted", "accept": accept}

    # SIMULATION: thợ đào giả nhận ngay + trả PoUI giả.
    miner_did = "did:key:zMOCKminer000000000000000000000000000000"
    accept = {"session_id": request["id"], "miner_did": miner_did,
              "poui": _mock_poui(request, miner_did)}
    log(f"[SIMULATION] session {request['id']} nhận bởi thợ đào giả {miner_did[:16]}…")
    return {"outcome": "accepted", "accept": accept}


# --- Xác minh PoUI (liên kết + hiện diện; KHÔNG phải xác minh mật mã đầy đủ) --------

def verify_poui(request: dict, poui: dict) -> dict:
    """Kiểm PoUI trả về có KHỚP phiên không: gắn đúng session_id, có commitment + chữ ký thợ
    đào. CHỦ Ý CHỈ kiểm liên kết/hiện diện — KHÔNG kiểm tính đúng mật mã của commitment (cần
    spec FLOP). Trả {ok, reason}."""
    if not isinstance(poui, dict):
        return {"ok": False, "reason": "PoUI rỗng/không hợp lệ"}
    if poui.get("session_id") != request.get("id"):
        return {"ok": False, "reason": "PoUI không gắn đúng phiên (session_id lệch)"}
    if not poui.get("commitment") or not poui.get("miner_sig"):
        return {"ok": False, "reason": "PoUI thiếu commitment hoặc chữ ký thợ đào"}
    return {"ok": True, "reason": "PoUI khớp phiên (liên kết + hiện diện; chưa xác minh mật mã)"}


# --- Thanh toán / khiếu nại --------------------------------------------------------

def settle(request: dict, *, path: str = None, submit_tx=None,
           private_key=None, did: str = None, log=print) -> dict:
    """THANH TOÁN phí phiên qua token_manager.spend() -> ở testnet, đây chính là 'inference
    spend' tích lũy mở khóa 3:1. Trả nguyên outcome của spend() (spent_onchain /
    spent_simulated / skipped_* / error_submit)."""
    return tm.spend(request["fee"], f"inference-session:{request['id']}",
                    token=request.get("token"), path=path, submit_tx=submit_tx,
                    private_key=private_key, did=did, log=log)


def dispute(request: dict, poui: dict, reason: str, *, path: str = None, log=print) -> dict:
    """KHIẾU NẠI kết quả sai: KHÔNG thanh toán, ghi 1 entry dispute vào sổ cái để có dấu vết.
    (Phán quyết mạng là việc của FLOP khi testnet mở; đây là bản ghi phía tác nhân.)"""
    state = tm.load_ledger(path)
    state["entries"].append({
        "kind": "dispute", "session_id": request.get("id"),
        "miner_did": (poui or {}).get("miner_did"), "reason": reason,
        "mode": tm.ledger_mode(), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    tm.save_ledger(state, path)
    log(f"[session] DISPUTE {request.get('id')} — {reason}")
    return {"outcome": "disputed", "session_id": request.get("id"), "reason": reason}


# --- Điều phối trọn vòng: mua 1 phiên inference ------------------------------------

def run_inference_session(model_hash: str, max_latency_ms: int, flops: int,
                          security_flags=None, fee: str = None, *, token: str = None,
                          path: str = None, submit_fn=None, submit_tx=None,
                          private_key=None, did: str = None, log=print) -> dict:
    """Mua MỘT phiên inference trọn vòng: build -> đăng mempool -> nhận PoUI -> verify ->
    settle (nếu hợp lệ) hoặc dispute (nếu sai). Trả dict có 'stage' + 'outcome' để caller
    biết dừng ở đâu. SIMULATION chạy trọn happy-path bằng thợ đào giả; TESTNET cần submit_fn
    (đăng mempool) và submit_tx (thanh toán)."""
    req = build_request(model_hash, max_latency_ms, flops, security_flags, fee,
                        token=token, private_key=private_key, did=did)
    sub = submit_request(req, submit_fn=submit_fn, log=log)
    if sub["outcome"] != "accepted":
        return {"stage": "submit", "request_id": req["id"], **sub}
    accept = sub["accept"]
    poui = accept.get("poui")
    if poui is None:
        # Testnet có thể trả bất đồng bộ (PoUI tới sau) -> báo pending, caller xử tiếp.
        return {"stage": "await_poui", "outcome": "pending", "request_id": req["id"],
                "session_id": accept.get("session_id"), "miner_did": accept.get("miner_did")}
    ver = verify_poui(req, poui)
    if not ver["ok"]:
        d = dispute(req, poui, ver["reason"], path=path, log=log)
        return {"stage": "verify", "request_id": req["id"], **d}
    paid = settle(req, path=path, submit_tx=submit_tx, private_key=private_key, did=did, log=log)
    return {"stage": "settle", "request_id": req["id"], "miner_did": accept.get("miner_did"),
            "verified": True, "settle_outcome": paid.get("outcome"),
            "fee": req["fee"], "tx_hash": paid.get("tx_hash"), "outcome": paid.get("outcome")}


def maybe_run_session(*args, **kw) -> dict:
    """Điểm vào cho AGENT: chỉ chạy khi FLOP_SESSION_ENABLED bật (mặc định TẮT ->
    skipped_off). Bọc kín mọi lỗi để không làm sập agent."""
    if not session_enabled():
        return {"outcome": "skipped_off", "reason": "FLOP_SESSION_ENABLED tắt"}
    try:
        return run_inference_session(*args, **kw)
    except Exception as e:
        return {"outcome": "error_session", "reason": str(e)}


# --- Demo offline ------------------------------------------------------------------

def _demo() -> None:
    import tempfile
    path = os.path.join(tempfile.mkdtemp(prefix="flop-session-demo-"), "ledger.json")
    tm.credit("10", path=path)
    print("— FLOP inference session (demo offline, simulation) —")
    print(f"mode: {tm.ledger_mode()}")
    out = run_inference_session(
        model_hash="a3f0" * 8, max_latency_ms=2000, flops=1_000_000_000,
        security_flags=["confidential"], fee="0.05", path=path)
    print(f"session: stage={out['stage']} · outcome={out['outcome']} · fee={out.get('fee')}")
    us = tm.unlock_status(path=path)
    print(f"unlock : spent_testnet={us['spent_testnet']} (đúng: sim không tích) · ratio 1/{us['ratio']}")
    print("\nTrung thực: sim dùng thợ đào giả + PoUI giả, verify chỉ kiểm liên kết. Chỉ "
          "TESTNET_ENABLED=true + FLOP_MEMPOOL_URL + submit_fn/submit_tx thật mới mua phiên "
          "thật & tích lũy mở khóa 3:1.")


if __name__ == "__main__":
    _demo()
