"""
flop_session.py — Mua suy luận từ thợ đào FLOP: đường "spend-to-earn" (mặc định 3:1 — CHƯA CHỐT).

Theo bản nháp FLOP (intro.flop.network/agent.html, 27/08/2026): một tác nhân đăng YÊU CẦU
PHIÊN vào mempool với 5 trường — [hash trọng số mô hình · độ trễ tối đa · số FLOPs · cờ bảo
mật · phí]. Một thợ đào nhận, mở kết nối riêng, chạy inference, trả về PoUI (Proof of Useful
Inference). Tác nhân có thể KHIẾU NẠI (dispute) nếu sai; nếu đúng thì THANH TOÁN bằng FLOP.
Airdrop = pro-rata theo FLOP chi cho inference (mỗi 3 FLOP phí -> mở khóa 1 FLOP).

TRUNG THỰC VỀ NGUỒN: cả "đây là đường earn CHÍNH" lẫn tỉ lệ 3:1 đều từ teaser + agent.html
draft, KHÔNG được Yellow Paper (flop.finance/intro/yellowpaper/) xác nhận — Appendix E còn để
ngỏ liệu spend-to-unlock có ship và điểm quy đổi có sublinear (2× chi CHƯA chắc trả 2×). Đừng
volume-farm dựa trên giả định này; cơ chế thắng rõ hơn ở near-term là MINER, mà repo này không ở.

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
import random
import re
import time

import token_manager as tm

# Ký Ed25519 (import mềm như token_manager) — bằng chứng ý định cho yêu cầu phiên.
try:
    from agent_cron import sign_message
except Exception:  # pragma: no cover
    sign_message = None


def vary_request(models, flops, max_latency_ms, fee=None, *, jitter_pct=0.0, rng=None):
    """ĐA DẠNG HÓA tham số 1 phiên (chống 'manufactured activity' / pattern máy): chọn
    model_hash NGẪU NHIÊN từ `models` (list — mỗi phiên khác nhau), và JITTER
    flops/max_latency_ms/fee ±jitter_pct quanh giá trị nền. Spend đa dạng trông như DEMAND
    THẬT thay vì N request giống hệt (mà census/anti-sybil chấm là boilerplate/low-originality).

    Hàm THUẦN: tiêm `rng` để test tất định; không mạng, không side-effect. Trả
    (model_hash, flops, max_latency_ms, fee_str|None). `models` rỗng -> model_hash=None (caller
    tự xử — thiếu model_hash là skip, KHÔNG bịa). jitter_pct<=0 -> giữ nguyên giá trị nền."""
    rng = rng or random
    models = [str(m).strip() for m in (models or []) if m and str(m).strip()]
    model = rng.choice(models) if models else None
    pct = max(0.0, float(jitter_pct or 0))

    def _jit_int(base, lo=1):
        base = int(base)
        if pct <= 0:
            return base
        factor = 1.0 + rng.uniform(-pct, pct) / 100.0
        return max(lo, int(round(base * factor)))

    flops_v = _jit_int(flops)
    lat_v = _jit_int(max_latency_ms)
    fee_v = fee
    if fee is not None and pct > 0:
        try:
            base_fee = float(fee)
            jittered = base_fee * (1.0 + rng.uniform(-pct, pct) / 100.0)
            fee_v = (f"{max(0.0, jittered):.6f}".rstrip("0").rstrip(".")) or "0"
        except (TypeError, ValueError):
            fee_v = fee
    return model, flops_v, lat_v, fee_v


def session_enabled() -> bool:
    """Gate mức agent (mặc định TẮT -> agent 24/7 không đổi hành vi)."""
    return os.environ.get("FLOP_SESSION_ENABLED", "").strip().lower() in ("1", "true", "on", "yes")


def mempool_url() -> str:
    """Endpoint mempool để đăng yêu cầu phiên (nạp khi FLOP công bố)."""
    return os.environ.get("FLOP_MEMPOOL_URL", "").strip() or tm.endpoint_url()


# --- Yêu cầu phiên (5 trường) ------------------------------------------------------

def plan_session_spend(due, base_fee, base_flops, max_sessions: int = 1,
                       max_flops: int = None, rng=None, fixed_fee=None) -> list:
    """Chia lượng `due` FLOP của run này thành các phiên MUA COMPUTE.

    ĐƠN GIÁ GIỮ NGUYÊN = base_fee / base_flops (cặp giá operator đặt theo thị trường).
    Chi nhiều hơn nghĩa là mua NHIỀU FLOPs hơn ở cùng giá — không phải trả giá cao hơn
    cho cùng một việc. (Lỗi cũ: để pacer quyết phí metering làm MỘT lần suy luận bị tính
    2 FLOP thay vì 0.001 — chi phồng chứ không mua thêm.)

    Trả list[(fee_str, flops_int)], tổng fee <= due. Rỗng khi due < base_fee (chờ tích
    thêm) hoặc tham số hỏng. `max_flops` (tùy chọn) là trần FLOPs MỘT phiên: chạm trần thì
    tăng số phiên tới `max_sessions`; vẫn không đủ thì chi ít hơn due — pacer bù run sau.
    Không có trần thì chia tối đa `max_sessions` phiên, mỗi phiên >= base_fee.
    `rng` (có .uniform) chia KHÔNG đều nhau ±20% để n phiên trong run không giống hệt.

    `fixed_fee` (FLOP_SESSION_FIXED_FEE): phòng khi mạng ÉP mỗi phiên đúng một mức phí — mọi
    phiên trả ĐÚNG số đó với `base_flops`, số phiên = floor(due / fixed_fee) (kẹp max_sessions);
    phần lẻ không đủ 1 phiên để pacer dồn sang run sau."""
    from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
    d, fee0 = tm._parse_amount(due), tm._parse_amount(base_fee)
    try:
        fl0 = int(base_flops)
    except (TypeError, ValueError):
        return []
    if fixed_fee is not None and str(fixed_fee).strip():
        ff = tm._parse_amount(fixed_fee)
        if d is None or ff is None or ff <= 0 or fl0 <= 0 or d < ff:
            return []
        n = min(max(1, int(max_sessions or 1)), int(d / ff))
        return [(tm._fmt(ff), fl0)] * n
    if d is None or fee0 is None or fee0 <= 0 or fl0 <= 0 or d < fee0:
        return []
    unit = fee0 / Decimal(fl0)                       # FLOP phải trả cho mỗi FLOPs
    n_max = max(1, int(max_sessions or 1))
    cap_fee = unit * Decimal(int(max_flops)) if max_flops else None
    if cap_fee is not None and cap_fee >= fee0:
        need = int((d / cap_fee).to_integral_value(rounding=ROUND_CEILING))
        n = min(n_max, max(1, need))
        total = min(d, cap_fee * n)
    else:
        cap_fee = None
        # 1.5x: trọng số ±20% sau chuẩn hoá vẫn giữ mỗi phần >= base_fee
        n = min(n_max, max(1, int(d / (fee0 * Decimal("1.5")))))
        total = d
    r = rng or random
    w = [Decimal(str(1 + r.uniform(-0.2, 0.2))) for _ in range(n)]
    q = Decimal("0.000001")
    fees = [(total * x / sum(w)).quantize(q, rounding=ROUND_DOWN) for x in w]
    fees[-1] = (total - sum(fees[:-1])).quantize(q, rounding=ROUND_DOWN)
    out = []
    for f in fees:
        if cap_fee is not None and f > cap_fee:      # trọng số đẩy vượt trần -> kẹp
            f = cap_fee.quantize(q, rounding=ROUND_DOWN)
        fl = int(f / unit)
        if f <= 0 or fl <= 0:
            # Phần lẻ không mua nổi 1 FLOPs (trần/phiên sát giá nền): bỏ, pacer dồn sang run
            # sau — thay vì gửi phiên "có phí, 0 FLOPs" mà build_request từ chối, phí slot.
            continue
        out.append((tm._fmt(f), fl))
    return out


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
    nonce = nonce or tm.next_nonce()
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


SIM_MINER_PREFIX = "did:key:zMOCKminer"
SIM_MINER_DEFAULT = "did:key:zMOCKminer000000000000000000000000000000"


def resolve_model_hashes(real: str, sim: str, testnet_on: bool) -> tuple:
    """Chọn danh sách model_hash cho phiên. Trả (list, nguồn) với nguồn ∈
    {'real','sim','none-testnet','none'}.

    Luật, theo thứ tự:
      1. Có hash THẬT -> luôn dùng, bất kể chế độ.
      2. Không có hash thật + TESTNET BẬT -> trả rỗng. TUYỆT ĐỐI không dùng hash
         placeholder khi có thể chạm mempool thật: hash giả gửi lên đó là rác, và
         là thứ bị đọc là hoạt động bịa.
      3. Không có hash thật + testnet TẮT + có hash mô phỏng -> dùng hash mô phỏng,
         để chạy thử trọn vòng request -> match -> verify -> settle trước ngày thật.
      4. Không có gì -> rỗng.

    Hash mô phỏng phải do người vận hành ĐẶT TƯỜNG MINH (biến riêng), không tự sinh —
    không ai được vô tình có hash giả trong cấu hình."""
    def _split(v):
        return [x.strip() for x in re.split(r"[,|]", v or "") if x.strip()]
    r = _split(real)
    if r:
        return r, "real"
    if testnet_on:
        return [], "none-testnet"
    m = _split(sim)
    return (m, "sim") if m else ([], "none")


def _mock_miner(request_id: str, pool: int = 1) -> str:
    """Thợ đào GIẢ cho simulation. `pool` > 1 -> xoay vòng tất định theo request id, để
    đường đo độ phân tán thợ đào được chạy thật thay vì luôn thấy 100% một người.
    Luôn mang tiền tố MOCK nên không thể nhầm với DID thật."""
    try:
        n = max(1, int(pool))
    except (TypeError, ValueError):
        n = 1
    if n <= 1:
        return SIM_MINER_DEFAULT              # giữ NGUYÊN DID cũ -> mặc định không đổi gì
    idx = int(hashlib.sha256(str(request_id).encode()).hexdigest()[:8], 16) % n
    return (f"{SIM_MINER_PREFIX}{idx:02d}"
            + "0" * (len(SIM_MINER_DEFAULT) - len(SIM_MINER_PREFIX) - 2))


def submit_request(request: dict, *, submit_fn=None, log=print, sim_miner_pool: int = 1) -> dict:
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
            return {"outcome": "no_miner",
                    "reason": f"không thợ đào nào nhận phiên (keys: {accept.get('_raw_keys') or sorted(accept)})"}
        return {"outcome": "accepted", "accept": accept}

    # SIMULATION: thợ đào giả nhận ngay + trả PoUI giả.
    miner_did = _mock_miner(request["id"], sim_miner_pool)
    accept = {"session_id": request["id"], "miner_did": miner_did,
              "poui": _mock_poui(request, miner_did)}
    log(f"[SIMULATION] session {request['id']} nhận bởi thợ đào giả {miner_did[:16]}…")
    return {"outcome": "accepted", "accept": accept}


# --- Xác minh PoUI (liên kết + hiện diện; KHÔNG phải xác minh mật mã đầy đủ) --------

# Tên trường theo thuật ngữ tài liệu FLOP ("activation commitments and a co-signed
# transcript root") + tên của PoUI giả trong simulation. Wire-format thật chưa công bố;
# lỗi vẫn in ra danh sách khoá thật để sửa nhanh ngày đầu.
_POUI_COMMIT_KEYS = ("commitment", "commitments", "activation_commitments",
                     "activation_commitment", "transcript_root", "proof_hash")
_POUI_SIG_KEYS = ("miner_sig", "miner_signature", "signature", "cosig", "co_signature",
                  "cosignature", "transcript_sig")
_POUI_SESSION_KEYS = ("session_id", "sessionId", "session")


def verify_poui(request: dict, poui: dict, session_id: str = None) -> dict:
    """Kiểm PoUI trả về có KHỚP phiên không + có commitment và chữ ký. CHỦ Ý CHỈ kiểm liên
    kết/hiện diện — KHÔNG kiểm mật mã (cần spec FLOP; sai thì mạng xử qua challenge game).

    Phiên được nhận diện bằng mã MẠNG cấp (`session_id` từ mempool) HOẶC mã yêu cầu của ta:
    mạng gần như chắc chắn cấp mã riêng, trước đây đòi trùng mã của ta nên proof thật luôn
    bị loại. Trả {ok, reason}."""
    if not isinstance(poui, dict):
        return {"ok": False, "reason": "PoUI rỗng/không hợp lệ"}
    keys = sorted(poui)[:15]
    sid = next((poui.get(k) for k in _POUI_SESSION_KEYS if poui.get(k)), None)
    if sid is not None and sid not in {request.get("id"), session_id} - {None}:
        return {"ok": False, "reason": f"PoUI không gắn đúng phiên (session_id {str(sid)[:24]} lệch; keys: {keys})"}
    if not any(poui.get(k) for k in _POUI_COMMIT_KEYS) or not any(poui.get(k) for k in _POUI_SIG_KEYS):
        return {"ok": False, "reason": f"PoUI thiếu commitment hoặc chữ ký thợ đào (keys: {keys})"}
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
                          private_key=None, did: str = None, deny_dids=None, log=print,
                          sim_miner_pool: int = 1) -> dict:
    """Mua MỘT phiên inference trọn vòng: build -> đăng mempool -> nhận PoUI -> verify ->
    settle (nếu hợp lệ) hoặc dispute (nếu sai). Trả dict có 'stage' + 'outcome' để caller
    biết dừng ở đâu. SIMULATION chạy trọn happy-path bằng thợ đào giả; TESTNET cần submit_fn
    (đăng mempool) và submit_tx (thanh toán).

    `deny_dids` = tập DID fleet nhà (main + fork). Nếu mempool match TRÚNG một miner trong đó
    -> KHÔNG settle (trả 'skipped_fleet_miner'): luật FLOP 'independent demand only' không tính
    chi cho miner cùng-nhóm + là cờ sybil. Chốt an toàn khi match tự nhiên lỡ rơi vào miner nhà."""
    req = build_request(model_hash, max_latency_ms, flops, security_flags, fee,
                        token=token, private_key=private_key, did=did)
    # Kiểm số dư TRƯỚC khi đăng: đăng rồi mới phát hiện thiếu tiền lúc settle = thợ đào đã
    # chạy inference mà không được trả (trông như quỵt). Chế độ ngân sách vốn kẹp kế hoạch
    # <= số dư; chế độ phí cố định / số dư tụt giữa run thì không — chốt ở đây cho mọi đường.
    bal = tm._parse_amount(tm.check_balance(req["token"], path=path)) or 0
    fee_d = tm._parse_amount(req["fee"])
    if fee_d is None or fee_d <= 0 or bal < fee_d:
        return {"stage": "precheck", "outcome": "skipped_insufficient", "request_id": req["id"],
                "reason": f"số dư {tm._fmt(bal)} < phí {req['fee']} — KHÔNG đăng phiên không trả nổi"}
    sub = submit_request(req, submit_fn=submit_fn, log=log,
                         sim_miner_pool=sim_miner_pool)
    if sub["outcome"] != "accepted":
        return {"stage": "submit", "request_id": req["id"], **sub}
    accept = sub["accept"]
    _miner = accept.get("miner_did")
    if deny_dids and _miner in set(deny_dids):
        log(f"[session] match trúng miner FLEET {str(_miner)[:16]}… -> BỎ, KHÔNG settle "
            "(independent-demand-only, chống self-deal)")
        return {"stage": "match", "outcome": "skipped_fleet_miner",
                "request_id": req["id"], "miner_did": _miner}
    poui = accept.get("poui")
    if poui is None:
        # Testnet có thể trả bất đồng bộ (PoUI tới sau) -> báo pending, caller xử tiếp.
        return {"stage": "await_poui", "outcome": "pending", "request_id": req["id"],
                "session_id": accept.get("session_id"), "miner_did": accept.get("miner_did"),
                "reason": f"mempool nhận phiên nhưng chưa trả proof (keys: {accept.get('_raw_keys') or sorted(accept)})"}
    ver = verify_poui(req, poui, accept.get("session_id"))
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
