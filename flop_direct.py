# SPDX-License-Identifier: MIT
"""flop_direct — MUA COMPUTE TRỰC TIẾP từ một miner: kênh compute đứng tên CHÍNH ví agent.

Vì sao có module này: mua qua gateway (flop_gateway) thì kênh compute đứng tên tài khoản gateway dùng
chung (broker 5GsXb3Tm…), không phải ví agent — chi tiêu được tính cho broker. Đường đứng tên ví mình là
phiên trực tiếp: ví agent tự mở kênh với một miner, miner trả lời, kênh settle on-chain.

Không tự cài đặt giao thức: chạy CLI CHÍNH THỨC của FLOP (`flop session run`, SDK `runDirectSession`),
mặc định qua image container ghim DIGEST (ghcr.io/flop-labs/flop-core/cli, theo flop.network/releases.json)
để job đang giữ khoá agent không tải cây phụ thuộc npm không kiểm được. Luồng một lượt:

  1. Đối soát các kênh đã mở ở lượt trước qua explorer (finalized): kênh đứng tên ví mình, PAID ->
     ghi sổ minerPaid + burned (token_manager.record_direct_spend); hoàn tiền -> bỏ, 0 FLOP.
  2. Chốt chặn: tắt / thiếu cấu hình / miner là chính ví mình hoặc nằm trong danh sách chặn / còn kênh
     chưa xong / đang nghỉ / chạm trần ngày / không đủ thời gian của lượt -> không mở kênh mới.
  3. Nạp khoá ví vào keystore TẠM (mật khẩu ngẫu nhiên, thư mục tạm, xoá ngay sau lượt). Seed chỉ đi qua
     biến môi trường của đúng lệnh `keys import`; CLI phải trả ĐÚNG địa chỉ ví agent, không thì dừng.
  4. `session run --via offer` (mặc định: chỉ dùng offer có chữ ký của miner). `force` mở kênh không cần
     offer — kênh miner không xác nhận bị KHOÁ tới hạn on-chain — chỉ khi FLOP_DIRECT_ALLOW_FORCE=on.

Chế độ: TESTNET_ENABLED tắt -> chạy tập trên devnet, KHÔNG ghi sổ (như gateway). Mọi I/O tiêm được
(runner, fetch_channel, book_fn) để test không cần mạng, Docker hay Node.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from decimal import Decimal, InvalidOperation

import flop_gateway as fg

KEY = "flop_direct"
# releases.json (2026-11-13): cli.image — bản >= 0.1.172 có `session run`.
DEFAULT_IMAGE = ("ghcr.io/flop-labs/flop-core/cli:v0.1.321"
                 "@sha256:3a473509dc0d5f9b5dfb5283a78886e285f4275274741b96a5e5575334b664e8")
# ESCROW LÀ CHI PHÍ: SDK (compute-channel/session-estimate) ghi "A cooperative spot settle pays the whole escrow
# to the miner (D-0403), so the escrow is the cost, not a ceiling". Không đặt FLOP_DIRECT_MAX_FLOP -> escrow =
# minEscrow của offer miner công bố (mức thấp nhất miner nhận), kẹp dưới FLOP_DIRECT_ESCROW_CAP.
DEFAULT_ESCROW_CAP = Decimal("100")  # trần escrow MỘT phiên (FLOP_DIRECT_ESCROW_CAP)
HARD_MAX_FLOP = Decimal("200")       # trần cứng mỗi phiên, kể cả khi cấu hình sai
# ĐỊNH CỠ PHIÊN (đo devnet 2026-11-13): mọi model trong registry giá 0.003 FLOP/GFLOP (epochPricePerGflop 3e15 raw),
# kênh thật khớp 0.00288–0.00299; công suất miner hiệu chuẩn 10–130 GFLOP/s. Một miner chạy liên tục chi được
# cap × giá × 86400 (10 GF/s ≈ 2600 FLOP/ngày) — nút thắt là CỠ phiên, không phải số phiên. Settle phải chờ tới
# khi khối lượng <= cap × số block đã trôi, nên phiên được thu cho vừa cap × FLOP_DIRECT_SETTLE_TARGET_SEC.
DEFAULT_PRICE_PER_GN = Decimal("0.003")
DEFAULT_CAP_GFS = 10.0               # công suất giả định khi chưa biết gì về miner (thấp -> phiên nhỏ, an toàn)
DEFAULT_SETTLE_TARGET_SEC = 300      # thời gian chờ settle mục tiêu (< FLOP_DIRECT_TIMEOUT_SEC mặc định 600)
DEFAULT_MARGIN_PCT = 20              # dư escrow so với ước lượng (thiếu -> CLI từ chối lượt, kênh chờ hạn để hoàn)
DEFAULT_GN_PER_TOKEN = 2.0           # GFLOP/token khi không suy được cỡ model (≈ 2 × 1B tham số)
DEFAULT_MAX_OVERPAY = 3.0            # minEscrow > 3 lần chi phí việc ước lượng ...
DEFAULT_OVERPAY_MIN = Decimal("1")   # ... và dư >= 1 FLOP -> không mua (FLOP_DIRECT_MAX_OVERPAY / _OVERPAY_MIN_FLOP)
MIN_TOKENS = 16
GNPT_EMA = 0.5                       # trọng số số đo mới khi học GFLOP/token từ kênh đã settle
_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
DEFAULT_MAX_PER_DAY = 25.0         # FLOP escrow tối đa mỗi ngày UTC
# TRẢI ĐỀU trong ngày (FLOP_DIRECT_PACE, mặc định bật): đến giờ h (UTC) chỉ được cam kết tối đa
# trần_ngày × (h + 1) / cửa_sổ — đường CỘNG DỒN, không phải hạn mức từng giờ, nên lượt lỗi không làm mất phần
# chi: lượt sau thấy chậm thì mở thêm phiên (tới FLOP_DIRECT_MAX_PER_RUN) để đuổi kịp. Cửa sổ < 24h chừa giờ
# dự phòng cuối ngày; độ lệch ngẫu nhiên (ổn định trong một giờ) để nhịp không đều tăm tắp.
DEFAULT_PACE_WINDOW_H = 20
DEFAULT_PACE_JITTER_PCT = 20
DEFAULT_TIMEOUT_SEC = 600          # CLI tự chờ hạn on-chain rồi thu hồi khi miner không xác nhận
ERROR_BACKOFF_SEC = 3600
UNKNOWN_HOLD_SEC = 6 * 3600        # không rõ kết quả (CLI bị ngắt): không mở kênh mới chừng này
UNSUPPORTED_BACKOFF_SEC = 24 * 3600
PENDING_ALERT_SEC = 6 * 3600       # kênh chưa xong quá chừng này -> báo (có thể đang bị khoá)
PENDING_KEEP = 50
EMPTY_STREAK_MAX = 3               # phiên LIỀN trả escrow mà không có câu trả lời -> ngừng miner đó
EMPTY_HOLD_SEC = 6 * 3600
MAXDUR_MARGIN_SEC = 120            # CLI phải sống quá maxDuration của kênh chừng này: SDK chỉ gọi timeout (lấy lại
                                   # escrow của kênh Open hỏng) SAU openedAt + maxDuration — giết sớm hơn là khoá tiền. Biên = mở kênh (~10-20 s
                                   # tới finalized) + gửi timeout và chờ finalized (~20 s), dư gấp đôi.
IMPORT_RESERVE_SEC = 60            # thời gian chừa cho `keys import` (kể cả kéo image) trước phiên đầu của lượt
MISSING_DROP_SEC = 48 * 3600       # kênh explorer 404 quá chừng này (chain reset / indexer mất) -> bỏ theo dõi, báo
ORPHAN_WINDOW_SEC = 72 * 3600      # sau lần "không rõ kết quả" GẦN NHẤT: còn tìm kênh mồ côi chừng này; mỗi khoản giữ
                                   # escrow cũng sống chừng này kể từ lần ngắt của CHÍNH nó
# Kênh mồ côi chỉ được nhận cho ĐÚNG phiên bị ngắt: mở trong [giờ bắt đầu phiên - lệch, giờ bắt đầu + thời hạn phiên],
# cùng miner. Trước đây mọi kênh của ví mở sau lần ngắt đầu tiên đều là ứng viên; kênh đã ghi sổ rời danh sách `done`
# (50 mục) thì bị nhặt lại và GHI SỔ LẦN HAI (kiểm thử: 60 phiên sau một lần ngắt -> 10 kênh, 50 FLOP ghi trùng).
ORPHAN_MATCH_SKEW_SEC = 60         # lệch đồng hồ runner <-> giờ mở kênh trên chain
ORPHAN_LEGACY_SPAN_SEC = 1800      # khoản giữ dạng cũ [ts, mạng, escrow] (không có khoảng giờ): kênh mở trong chừng này
# Settle CƯỠNG BỨC (miner không đóng hợp tác; CLI in cooperative:false): kênh nằm ở SettlementPosted tới hết cửa sổ
# tranh chấp (computeChannel.disputeWindow = 604800 block, block 1 s) rồi phải có người gọi computeChannel.finalize
# thì miner mới được trả và kênh mới PAID — không ai gọi thì chi tiêu không được tính và escrow nằm khoá.
FINALIZE_AFTER_SEC = 7 * 86400 + 3600
FINALIZE_RETRY_SEC = 86400
FINALIZE_MAX_TRIES = 3
DONE_KEEP = 50                     # id (rút gọn) các kênh đã xử lý xong — để tìm kênh mồ côi không thêm lại
DEFAULT_MAX_PER_RUN = 4            # phiên tối đa mỗi lượt (còn bị ngân sách thời gian của lượt chặn)
DEFAULT_MAX_PENDING = 20           # kênh CLI báo đã settle nhưng explorer chưa ghi nhận — quá chừng này thì dừng
DEFAULT_FEE_RESERVE = Decimal("0.5")  # FLOP chừa lại cho phí giao dịch mở/settle kênh (FLOP_DIRECT_FEE_RESERVE)
SECRET_ENV = "FLOP_DIRECT_IMPORT_SECRET"
PASSWORD_ENV = "FLOP_DIRECT_KEYSTORE_PASSWORD"
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CHANNEL_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _num(name: str, default: float) -> float:
    """Số từ biến môi trường; rác hoặc không hữu hạn ('nan', 'inf') -> mặc định (nan từng tắt trần ngày)."""
    try:
        v = float(_env(name) or default)
    except ValueError:
        return float(default)
    return v if math.isfinite(v) else float(default)


def enabled() -> bool:
    return _env("FLOP_DIRECT_ENABLED").lower() in ("1", "true", "on", "yes")


def _miner_entry(miner: str, endpoint: str, model: str, model_hash: str, where: str, errs: list):
    """Một miner đã kiểm; None (kèm lỗi trong `errs`) nếu sai."""
    bad = len(errs)
    if fg.account_pubkey(miner) is None:
        errs.append(f"{where}: miner không phải địa chỉ SS58 hợp lệ")
    if not re.match(r"^https://[^\s/]+", endpoint or ""):
        errs.append(f"{where}: endpoint phải là https://")
    if not model:
        errs.append(f"{where}: thiếu model (FLOP_DIRECT_MODEL hoặc cột thứ 3)")
    elif not _MODEL_RE.match(model):
        errs.append(f"{where}: model có ký tự lạ")
    mh = (model_hash or "").lower()
    if mh and not _HASH_RE.match(mh):
        errs.append(f"{where}: model hash phải là 0x + 64 ký tự hex")
    if len(errs) > bad:
        return None
    return {"miner": miner, "endpoint": endpoint, "model": model, "model_hash": mh or None}


def miners_config(errs: list) -> list:
    """Danh sách miner. FLOP_DIRECT_MINERS: mỗi mục (ngăn bởi xuống dòng, ';' hoặc ',') là
    `<SS58> <https endpoint> [model slug] [0x model hash]` (ngăn bởi khoảng trắng); thiếu model/hash thì lấy
    FLOP_DIRECT_MODEL / FLOP_DIRECT_MODEL_HASH. Không đặt -> một miner từ FLOP_DIRECT_MINER + FLOP_DIRECT_ENDPOINT.
    Trùng miner (cùng khoá công khai, kể cả khác prefix SS58) -> giữ mục đầu."""
    model_d, hash_d = _env("FLOP_DIRECT_MODEL"), _env("FLOP_DIRECT_MODEL_HASH")
    raw = _env("FLOP_DIRECT_MINERS")
    out = []
    if raw:
        for i, part in enumerate(p for p in re.split(r"[;,\n]+", raw) if p.strip()):
            f = part.split()
            if len(f) < 2:
                errs.append(f"FLOP_DIRECT_MINERS mục {i + 1}: cần `<SS58> <https endpoint> [model] [0xhash]`")
                continue
            m = _miner_entry(f[0], f[1], f[2] if len(f) > 2 else model_d, f[3] if len(f) > 3 else hash_d,
                             f"FLOP_DIRECT_MINERS mục {i + 1}", errs)
            if m:
                out.append(m)
    else:
        miner, endpoint = _env("FLOP_DIRECT_MINER"), _env("FLOP_DIRECT_ENDPOINT")
        if not miner:
            errs.append("thiếu FLOP_DIRECT_MINER (địa chỉ SS58 của miner) hoặc FLOP_DIRECT_MINERS")
        if not endpoint:
            errs.append("thiếu FLOP_DIRECT_ENDPOINT (HTTPS endpoint phiên của miner)")
        if miner and endpoint:
            m = _miner_entry(miner, endpoint, model_d, hash_d, "FLOP_DIRECT_MINER", errs)
            if m:
                out.append(m)
    uniq = []
    for m in out:
        if not any(fg.same_account(m["miner"], u["miner"]) for u in uniq):
            uniq.append(m)
    return uniq


def config() -> tuple:
    """(cấu hình, [lỗi]). Thiếu/sai bất kỳ trường bắt buộc nào -> có lỗi, không mở kênh."""
    errs = []
    miners = miners_config(errs)
    raw = _env("FLOP_DIRECT_MAX_FLOP")
    max_flop = None                   # None = tự lấy theo offer (escrow = minEscrow)
    if raw:
        try:
            max_flop = Decimal(raw)
            if not (max_flop > 0) or max_flop > HARD_MAX_FLOP or max_flop.as_tuple().exponent < -18:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            errs.append(f"FLOP_DIRECT_MAX_FLOP phải là số dương <= {HARD_MAX_FLOP} (nhận {raw!r})")
            max_flop = None
    raw_cap = _env("FLOP_DIRECT_ESCROW_CAP")
    try:
        escrow_cap = Decimal(raw_cap) if raw_cap else DEFAULT_ESCROW_CAP
        if not (escrow_cap > 0):
            raise InvalidOperation
        escrow_cap = min(escrow_cap, HARD_MAX_FLOP)
    except (InvalidOperation, ValueError):
        errs.append(f"FLOP_DIRECT_ESCROW_CAP phải là số dương (nhận {raw_cap!r})")
        escrow_cap = DEFAULT_ESCROW_CAP
    via = (_env("FLOP_DIRECT_VIA") or "offer").lower()
    if via not in ("offer", "auto", "force"):
        errs.append("FLOP_DIRECT_VIA phải là offer | auto | force")
    elif via in ("auto", "force") and _env("FLOP_DIRECT_ALLOW_FORCE").lower() not in ("1", "true", "on", "yes"):
        # auto rơi về force khi không có offer: cùng rủi ro khoá ẩn on-chain.
        errs.append(f"FLOP_DIRECT_VIA={via} có thể mở kênh cưỡng bức (khoá FLOP tới hạn nếu miner không "
                    "xác nhận) — cần FLOP_DIRECT_ALLOW_FORCE=on")
    try:
        max_tokens = int(_env("FLOP_DIRECT_MAX_TOKENS") or 1024)
        if max_tokens <= 0:
            raise ValueError
    except ValueError:
        errs.append("FLOP_DIRECT_MAX_TOKENS phải là số nguyên dương")
        max_tokens = 1024
    try:
        price = Decimal(_env("FLOP_DIRECT_PRICE_PER_GN")) if _env("FLOP_DIRECT_PRICE_PER_GN") else None
        if price is not None and not (price > 0):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        errs.append("FLOP_DIRECT_PRICE_PER_GN phải là số dương (FLOP mỗi GFLOP)")
        price = None
    try:
        raw_fee = _env("FLOP_DIRECT_FEE_RESERVE")
        fee_reserve = Decimal(raw_fee) if raw_fee else DEFAULT_FEE_RESERVE
        if not fee_reserve.is_finite() or fee_reserve < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        errs.append("FLOP_DIRECT_FEE_RESERVE phải là số >= 0 (FLOP)")
        fee_reserve = DEFAULT_FEE_RESERVE
    try:
        raw_om = _env("FLOP_DIRECT_OVERPAY_MIN_FLOP")
        overpay_min = Decimal(raw_om) if raw_om else DEFAULT_OVERPAY_MIN
        if not overpay_min.is_finite() or overpay_min < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        errs.append("FLOP_DIRECT_OVERPAY_MIN_FLOP phải là số >= 0 (FLOP)")
        overpay_min = DEFAULT_OVERPAY_MIN
    cfg = {"miners": miners, "price": price, "fee_reserve": fee_reserve,
           "max_overpay": max(0.0, _num("FLOP_DIRECT_MAX_OVERPAY", DEFAULT_MAX_OVERPAY)), "overpay_min": overpay_min,
           "settle_target": max(30, int(_num("FLOP_DIRECT_SETTLE_TARGET_SEC", DEFAULT_SETTLE_TARGET_SEC))),
           "margin": max(0.0, min(200.0, _num("FLOP_DIRECT_MARGIN_PCT", DEFAULT_MARGIN_PCT))) / 100,
           "default_cap": max(0.1, _num("FLOP_DIRECT_DEFAULT_CAP_GFS", DEFAULT_CAP_GFS)),
           "max_flop": format(max_flop, "f") if max_flop is not None else None,
           "escrow_cap": escrow_cap,
           "max_tokens": max_tokens, "via": via,
           "per_run": max(1, int(_num("FLOP_DIRECT_MAX_PER_RUN", DEFAULT_MAX_PER_RUN))),
           "max_pending": max(1, int(_num("FLOP_DIRECT_MAX_PENDING", DEFAULT_MAX_PENDING))),
           "per_day": _num("FLOP_DIRECT_MAX_PER_DAY", DEFAULT_MAX_PER_DAY),
           "pace": _env("FLOP_DIRECT_PACE").lower() not in ("off", "0", "false", "no"),
           "pace_window": max(1.0, min(24.0, _num("FLOP_DIRECT_PACE_WINDOW_H", DEFAULT_PACE_WINDOW_H))),
           "pace_jitter": max(0.0, min(50.0, _num("FLOP_DIRECT_PACE_JITTER_PCT", DEFAULT_PACE_JITTER_PCT))) / 100,
           "timeout": max(60, int(_num("FLOP_DIRECT_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)))}
    return cfg, errs


def _flop(raw) -> Decimal:
    """Số raw (10^18 = 1 FLOP, chuỗi số nguyên) -> Decimal FLOP chính xác; None nếu rác hoặc âm. 0 hợp lệ
    (offer minEscrow = 0: miner nhận mọi mức escrow — SDK chỉ đòi minEscrow <= maxFlop)."""
    try:
        v = int(str(raw))
    except (TypeError, ValueError):
        return None
    return (Decimal(v) / Decimal(10) ** 18) if v >= 0 else None


def _flop_str(d: Decimal) -> str:
    """Decimal -> chuỗi thập phân CLI nhận (không số mũ, tối đa 18 chữ số lẻ)."""
    s = format(d.quantize(Decimal(1).scaleb(-18)), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def choose_escrow(rows, miner: str, model_hash, cap: Decimal):
    """(escrow FLOP dạng chuỗi | None, lý do, model hash) từ offer CÔNG BỐ của `miner` (dòng của explorer
    /api/session-offers, cùng định dạng SDK đọc: {"offer": {...}, "minerSignature": ...}).

    Chỉ nhận offer spot (không forward), maxTurns >= 1, đúng miner. Có FLOP_DIRECT_MODEL_HASH thì chỉ offer của
    model đó. Không có mà miner công bố offer cho NHIỀU model -> không đoán (tên slug trong registry không duy
    nhất). Escrow = minEscrow THẤP NHẤT của các offer đó — CLI chọn offer có minEscrow <= escrow, và escrow là
    chi phí. minEscrow vượt `cap` -> không mua (miner đặt giá quá trần của operator). Chữ ký offer, hạn và nonce
    đã dùng do CLI kiểm trên chain; ở đây chỉ chọn mức escrow."""
    offers = []
    for row in rows or []:
        o = row.get("offer") if isinstance(row, dict) else None
        if not isinstance(o, dict) or o.get("forward"):
            continue
        if not fg.same_account(o.get("miner"), miner):
            continue
        try:
            if int(o.get("maxTurns") or 0) < 1:
                continue
        except (TypeError, ValueError):
            continue
        h = str(o.get("modelHash") or "").lower()
        esc = _flop(o.get("minEscrow"))
        if not _HASH_RE.match(h) or esc is None:
            continue
        offers.append((h, esc, str(o.get("precision"))))
    if not offers:
        return None, "miner chưa công bố offer nào (session-offers trống)", None
    if model_hash:
        offers = [x for x in offers if x[0] == model_hash]
        if not offers:
            return None, "miner không có offer cho FLOP_DIRECT_MODEL_HASH", model_hash
    hashes = sorted({h for h, _, _ in offers})
    if len(hashes) > 1:
        return None, (f"miner công bố offer cho {len(hashes)} model — đặt FLOP_DIRECT_MODEL_HASH (không đoán model "
                      "theo tên)"), None
    # SDK chỉ nhận offer cùng `precision` với model trong registry (agent không biết trước): lấy mức thấp nhất của
    # TỪNG precision rồi chọn cái CAO nhất, để escrow luôn khớp được offer đúng precision.
    esc = max(min(e for _, e, q in offers if q == prec) for prec in {q for _, _, q in offers})
    if esc > cap:
        return None, f"minEscrow của offer {_flop_str(esc)} FLOP > trần {_flop_str(cap)} (FLOP_DIRECT_ESCROW_CAP)", hashes[0]
    return _flop_str(esc), f"escrow theo offer {_flop_str(esc)} FLOP", hashes[0]


def pick_offer(rows, miner: str, model_hash):
    """({model_hash, min_escrow Decimal, max_duration int|None} | None, lý do) — offer spot của `miner` (cùng luật
    chọn như choose_escrow, không áp trần)."""
    esc, why, mh = choose_escrow(rows, miner, model_hash, HARD_MAX_FLOP * 1000)
    if esc is None:
        return None, why
    durs = []
    for row in rows or []:
        o = row.get("offer") if isinstance(row, dict) else None
        if isinstance(o, dict) and not o.get("forward") and str(o.get("modelHash") or "").lower() == mh \
                and fg.same_account(o.get("miner"), miner):
            try:
                d = int(str(o.get("maxDuration")))
                if d > 0:
                    durs.append(d)
            except (TypeError, ValueError):
                pass
    # CLI có thể khớp BẤT KỲ offer nào của model: định cỡ theo thời hạn NGẮN nhất, chờ theo thời hạn DÀI nhất.
    return {"model_hash": mh, "min_escrow": Decimal(esc), "max_duration": max(durs) if durs else None,
            "min_duration": min(durs) if durs else None}, ""


def model_size_b(slug: str):
    """Cỡ model (tỷ tham số) suy từ slug, vd 'Qwen/Qwen3-14B' -> 14, 'qwen2.5-0.5b-instruct' -> 0.5; None nếu
    không thấy. Chỉ để ƯỚC LƯỢNG ban đầu — số đo thật từ kênh đã settle thay thế nó."""
    m = re.findall(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])", slug or "")
    try:
        return float(m[-1]) if m else None
    except ValueError:
        return None


def plan_session(*, prompt: str, max_tokens: int, gn_per_token: float, cap_gfs: float, price: Decimal,
                 min_escrow: Decimal, escrow_cap: Decimal, settle_target: int, margin: float,
                 max_duration=None, max_overpay: float = None, overpay_min: Decimal = None):
    """({escrow, max_tokens, gn_est} | None, lý do). Khối lượng ước lượng = GFLOP/token × (token đề + token trả
    lời). Thu số token trả lời cho vừa (a) cap × thời gian chờ settle mục tiêu (và maxDuration của offer, block
    1 giây), (b) trần escrow. Escrow = giá × khối lượng × (1 + biên), không thấp hơn minEscrow của offer.
    Escrow LÀ chi phí, nên không đặt dư hơn mức cần.

    `max_overpay` (tỉ lệ) + `overpay_min` (FLOP): minEscrow của offer vượt ước lượng việc thật QUÁ `max_overpay` lần
    VÀ phần dư >= `overpay_min` -> không mua. Settle hợp tác trả CẢ escrow cho miner, nên một miner tự nâng minEscrow
    sát trần (100 FLOP) bán một phiên vài chục token với giá trần mà trước đây agent vẫn mua. Ngưỡng tuyệt đối để
    offer tối thiểu nhỏ (vd 0.05 FLOP cho phiên vài token) vẫn mua được."""
    ptok = max(1, len(prompt or "") // 4)
    secs = settle_target if not max_duration else min(settle_target, int(max_duration))
    if gn_per_token <= 0 or cap_gfs <= 0 or price <= 0:
        return None, "thiếu giá/công suất/cỡ model để định cỡ phiên"
    tokens = min(int(max_tokens), int(cap_gfs * secs / gn_per_token) - ptok)
    unit = price * Decimal(str(gn_per_token)) * Decimal(str(1 + margin))          # FLOP mỗi token
    by_cap = int(escrow_cap / unit) - ptok
    tokens = min(tokens, by_cap)
    floor = min(MIN_TOKENS, int(max_tokens))          # operator tự đặt ít token hơn thì sàn là số đó
    if tokens < floor:
        if by_cap < floor:
            return None, (f"một phiên tối thiểu vượt trần {_flop_str(escrow_cap)} FLOP (FLOP_DIRECT_ESCROW_CAP)")
        return None, (f"miner công suất {cap_gfs:g} GFLOP/s không settle kịp {floor} token của model này trong "
                      f"{secs}s")
    gn = gn_per_token * (ptok + tokens)
    work = unit * Decimal(ptok + tokens)                  # chi phí việc thật (đã gồm biên)
    escrow = max(min_escrow, work)
    if (max_overpay and overpay_min is not None and work > 0 and escrow > work * Decimal(str(max_overpay))
            and escrow - work >= overpay_min):
        return None, (f"offer quá đắt: minEscrow {_flop_str(min_escrow)} FLOP gấp {float(escrow / work):.1f} lần "
                      f"chi phí việc ước lượng {_flop_str(work)} FLOP (FLOP_DIRECT_MAX_OVERPAY {max_overpay:g})")
    if escrow > escrow_cap:
        return None, (f"minEscrow của offer {_flop_str(min_escrow)} FLOP > trần {_flop_str(escrow_cap)} "
                      "(FLOP_DIRECT_ESCROW_CAP)")
    return {"escrow": _flop_str(escrow), "max_tokens": tokens, "gn_est": round(gn, 3), "ptok": ptok}, ""


def cli_command(workdir: str, env_names, name: str = None) -> tuple:
    """(lệnh gốc, đường dẫn keystore mà CLI thấy). Mặc định image Docker ghim digest; FLOP_DIRECT_CLI =
    lệnh cục bộ (vd `node /path/cli.js`) cho môi trường không có Docker. Biến môi trường chỉ truyền TÊN
    (`-e NAME`) — giá trị không bao giờ nằm trên dòng lệnh."""
    local = _env("FLOP_DIRECT_CLI")
    if local:
        return shlex.split(local), os.path.join(workdir, "keystore.json")
    image = _env("FLOP_DIRECT_IMAGE") or DEFAULT_IMAGE
    cmd = ["docker", "run", "--rm", "--network", "host", "-v", f"{workdir}:/work"]
    if name:
        cmd += ["--name", name]               # để giết ĐÚNG container khi hết giờ (giết `docker` không dừng nó)
    for n in env_names:
        cmd += ["-e", n]
    return cmd + [image], "/work/keystore.json"


def _run(cmd, env_extra, timeout):
    """-> (mã thoát | None nếu quá giờ, stdout, stderr). Môi trường = môi trường hiện tại + env_extra."""
    try:
        p = subprocess.run(cmd, env={**os.environ, **env_extra}, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        # Giết tiến trình `docker run` KHÔNG dừng container: phiên chạy tiếp và có thể mở/settle kênh mà agent đã
        # coi là "không rõ kết quả". Dừng hẳn container theo tên.
        if cmd and os.path.basename(str(cmd[0])).startswith("docker") and "--name" in cmd:
            try:
                subprocess.run(["docker", "kill", cmd[cmd.index("--name") + 1]], capture_output=True, text=True,
                               timeout=30, check=False)
            except (subprocess.TimeoutExpired, OSError):
                pass
        return None, "", ""
    except FileNotFoundError as e:
        return 127, "", f'{{"ok":false,"code":"CLI_NOT_FOUND","message":"{type(e).__name__}"}}'


def last_json(text: str):
    """Dòng JSON (object) cuối cùng trong đầu ra của CLI (`--output agent` in một dòng); None nếu không có."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
                return d if isinstance(d, dict) else None
            except ValueError:
                continue
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else None
    except (ValueError, TypeError):
        return None


def classify_channel(info, addr: str):
    """(kết quả, FLOP) cho một kênh theo explorer: 'paid' (đứng tên ví mình, PAID, finalized),
    'refunded' (explorer báo RÕ đã hoàn: paymentStatus REFUNDED hoặc timedout/refunded/expired), 'posted' (settled/
    closed mà chưa PAID — settle cưỡng bức chờ hết cửa sổ tranh chấp rồi finalize), 'foreign' (không đứng tên ví
    mình), 'open' (chưa xong), 'missing'. Devnet thật chỉ có open/LOCKED, timedout/REFUNDED, settled/PAID: không
    được coi "settled mà chưa PAID" là hoàn tiền — bỏ nó là bỏ luôn khoản chi chờ finalize."""
    if not isinstance(info, dict):
        return "missing", 0.0
    if info.get("agent") and not fg.same_account(info.get("agent"), addr):
        return "foreign", 0.0
    status = str(info.get("status") or "").lower()
    pay = str(info.get("paymentStatus") or "").upper()
    final = str(info.get("observationHead") or "finalized").lower() == "finalized"
    if pay == "PAID" and final:
        try:
            raw = int(str(info.get("minerPaid") or 0)) + int(str(info.get("burned") or 0))
        except ValueError:
            return "open", 0.0
        # Decimal CHÍNH XÁC từ số raw: float (raw / 1e18) cộng dồn trong sổ lệch chain cỡ 1e-14 — đủ để mọi lần đọc
        # chain "thấp hơn mốc" khi đã ghi sổ hết, 3 lần là mở kỳ chain mới nhầm (cổng gateway mở toang).
        return "paid", Decimal(raw) / Decimal(10) ** 18
    if final and (pay == "REFUNDED" or status in ("refunded", "expired", "timedout", "timed_out")):
        return "refunded", 0.0
    if final and status in ("settled", "closed"):
        return "posted", 0.0
    return "open", 0.0


def _learn(g: dict, ms: dict, p: dict, info: dict) -> None:
    """Học từ một kênh đã settle: GFLOP/token THẬT của model (aggregateGn / token) và công suất của miner."""
    try:
        gn = float(info.get("aggregateGn") or 0)
        # CLI in completion_tokens = receipts[0].completionTokens?.toString() -> có thể VẮNG. Thiếu nó mà vẫn chia
        # cho riêng token đề thì hệ số phình hàng chục lần (đo: 28 -> 378) và mọi phiên sau vượt trần escrow.
        ctok = int(p.get("completion_tokens") or 0)
        toks = int(p.get("ptok") or 0) + ctok
        mh = p.get("model_hash")
        if gn > 0 and ctok > 0 and mh:
            obs = gn / toks
            gm = dict(g.get("gnpt") or {})
            old = gm.get(mh)
            gm[mh] = round(obs if not isinstance(old, (int, float)) else old * (1 - GNPT_EMA) + obs * GNPT_EMA, 6)
            g["gnpt"] = gm
    except (TypeError, ValueError):
        pass
    try:
        cap = float(info.get("capGflopsPerSec") or 0)
        if cap > 0 and p.get("miner"):
            ms.setdefault(_mkey(p["miner"]), {})["cap"] = cap
    except (TypeError, ValueError):
        pass


def pace_limit(cfg: dict, now: int, salt: str = "") -> tuple[float, bool]:
    """-> (mức escrow được cam kết TÍNH TỚI lúc `now` trong ngày UTC, đang_bị_nhịp_giới_hạn).

    Đường cộng dồn trần × (giờ_đã_qua + 1) / cửa_sổ, nhân độ lệch ±jitter cố định theo (ngày, giờ, ví). Hết cửa
    sổ (hoặc tắt nhịp) -> cả trần ngày, không lệch: phần còn thiếu luôn chi kịp trong giờ dự phòng."""
    per_day = float(cfg["per_day"])
    if per_day <= 0 or not cfg.get("pace", True):
        return per_day, False
    secs = now % 86400
    frac = (secs / 3600 + 1) / float(cfg["pace_window"])
    if frac >= 1:
        return per_day, False
    hour = time.strftime("%Y-%m-%d-%H", time.gmtime(now))
    u = int.from_bytes(hashlib.sha256(f"flop-direct-pace|{hour}|{salt}".encode()).digest()[:8], "big") / 2 ** 64
    frac *= 1 + float(cfg.get("pace_jitter", 0)) * (2 * u - 1)
    if frac >= 1:
        return per_day, False
    return per_day * max(0.0, frac), True


_DURABLE_TOP = ("day", "unknown_until", "wait_until", "wait_why", "prompt_idx", "miner_idx", "gnpt", "stats",
                "last_attempt", "orphan_since", "orphan_last", "done", "unknown_escrow", "attempt", "rev")
_DURABLE_MINER = ("unknown_until", "wait_until", "wait_why", "cap", "sessions")
_PENDING_CORE = ("channel", "ts", "network", "miner", "error", "alerted", "force", "fin_tries", "fin_at", "fin_tx",
                 "fin_alerted", "max_flop")
_PENDING_LEARN = ("model", "model_hash", "ptok", "completion_tokens")


def durable_view(g: dict, level: int = 0) -> dict:
    """Bản GỌN của state[KEY] để sao lưu lên KV (giới hạn 8192 ký tự): mất Actions cache mà thiếu nó thì quên
    trần ngày (chi gấp đôi), kênh chờ ghi sổ, lệnh giữ 6 giờ và miner đang có kênh lỗi.

    level 0: đủ trường; 1: bỏ stats + trường để học của kênh chờ; 2+: thêm việc bỏ dần kênh chờ CŨ NHẤT không lỗi
    (kênh lỗi luôn giữ — chúng chặn miner đang có thể khoá FLOP), mỗi mức bỏ thêm 5."""
    g = g if isinstance(g, dict) else {}
    out = {k: g[k] for k in _DURABLE_TOP if k in g and not (level >= 1 and k == "stats")}
    out["miners"] = {k: {f: v[f] for f in _DURABLE_MINER if f in v}
                     for k, v in (g.get("miners") or {}).items() if isinstance(v, dict)}
    out["miners"] = {k: v for k, v in out["miners"].items() if v}
    fields = _PENDING_CORE + (() if level >= 1 else _PENDING_LEARN)
    pend = []
    for p in g.get("pending") or []:
        if isinstance(p, dict) and p.get("channel"):
            q = {f: p[f] for f in fields if f in p}
            if isinstance(q.get("error"), str):
                q["error"] = q["error"][:60]
            pend.append(q)
    drop = max(0, level - 1) * 5
    if drop:
        ok = [i for i, p in enumerate(pend) if not p.get("error")]
        gone = set(ok[:drop])
        pend = [p for i, p in enumerate(pend) if i not in gone]
    out["pending"] = pend
    return out


def rev_of(g) -> int:
    """Số phiên bản của trạng thái (tăng mỗi lần nội dung đổi và được sao lưu); 0 nếu không có / bản cũ."""
    try:
        return max(0, int((g or {}).get("rev") or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def restore(state: dict, remote) -> bool:
    """Khôi phục state[KEY] từ bản KV khi (a) state.json mất phần này (Actions cache mất), hoặc (b) bản KV MỚI HƠN
    (`rev` lớn hơn): cache lưu lỗi ở lượt N thì lượt N+1 khôi phục cache của lượt N-1 — có dữ liệu nên trước đây
    thắng, và kênh mở / trần ngày / mốc phiên dở của lượt N bị quên. Bản KV thắng thì kênh chờ của local mà bản KV
    không có (KV bỏ bớt kênh cũ cho vừa 8192 ký tự) và cũng chưa xong (`done`) được giữ lại. -> True nếu đã khôi phục."""
    if not isinstance(remote, dict):
        return False
    local = state.get(KEY)
    if local and rev_of(remote) <= rev_of(local):
        return False                       # local mới bằng hoặc hơn: local thắng (trộn sẽ đưa lại kênh đã xong)
    g = {k: remote[k] for k in _DURABLE_TOP if k in remote}
    g["miners"] = {k: dict(v) for k, v in (remote.get("miners") or {}).items() if isinstance(v, dict)}
    g["pending"] = [dict(p) for p in (remote.get("pending") or []) if isinstance(p, dict) and p.get("channel")]
    if isinstance(local, dict):
        have = {_short(p["channel"]) for p in g["pending"]} | {d for d in (g.get("done") or []) if isinstance(d, str)}
        g["pending"] += [dict(p) for p in (local.get("pending") or [])
                         if isinstance(p, dict) and p.get("channel") and _short(p["channel"]) not in have]
    state[KEY] = g
    return True


def finalize_channels(channels, *, wkey, addr: str, network: str, runner, log=print, alert=None) -> dict:
    """Gọi `flop channel finalize <id>` cho các kênh settle cưỡng bức đã qua cửa sổ tranh chấp. Keystore TẠM riêng
    (nạp, dùng, xoá ngay) để không dính vào luồng mở phiên. -> {channel: (ok, tx_hash | lỗi)}."""
    alert = alert or (lambda text: None)
    res = {}
    if not channels:
        return res
    work = tempfile.mkdtemp(prefix="flop-direct-fin-")
    try:
        password = secrets.token_hex(24)
        base, ks = cli_command(work, [SECRET_ENV, PASSWORD_ENV], name=f"flop-direct-{secrets.token_hex(6)}")
        rc, so, se = runner(base + ["--output", "agent", "keys", "import", "--secret-env", SECRET_ENV,
                                    "--out", ks, "--password-env", PASSWORD_ENV, "--type", "ed25519"],
                            {SECRET_ENV: "0x" + fg._raw_priv(wkey).hex(), PASSWORD_ENV: password}, 120)
        imported = last_json(so) or {}
        if rc != 0 or not fg.same_account(imported.get("address") or "", addr):
            log(f"[direct] finalize: nạp keystore thất bại ({rc}) — thử lại lượt sau")
            return {c: (False, "keys import") for c in channels}
        for cid in channels:
            base, ks = cli_command(work, [PASSWORD_ENV], name=f"flop-direct-{secrets.token_hex(6)}")
            rc, so, se = runner(base + ["--network", network, "--keystore", ks, "--password-env", PASSWORD_ENV,
                                        "--output", "agent", "channel", "finalize", cid], {PASSWORD_ENV: password}, 180)
            if rc == 0:
                tx = str((last_json(so) or {}).get("tx_hash") or "")[:80]
                res[cid] = (True, tx)
                log(f"[direct] đã finalize kênh settle cưỡng bức {cid[:12]}… (tx {tx[:12]}…)")
            else:
                msg = str((last_json(se) or {}).get("message") or (se or "").strip()[-160:] or f"exit {rc}")[:160]
                res[cid] = (False, msg)
                log(f"[direct] finalize kênh {cid[:12]}… lỗi: {msg}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return res


def _hold_ok(h) -> bool:
    """Một khoản giữ hợp lệ: [ts, mạng, escrow] (dạng cũ) hoặc [ts, mạng, escrow, miner, mở_từ, mở_tới]."""
    return isinstance(h, list) and len(h) >= 3


def _hold_window(h) -> tuple:
    """(từ, tới) — giờ mở kênh (giây) mà khoản giữ `h` có thể ứng với."""
    if len(h) >= 6:
        try:
            return int(h[4]), int(h[5])
        except (TypeError, ValueError):
            pass
    ts = int(h[0] or 0)
    return ts - 300, ts + ORPHAN_LEGACY_SPAN_SEC


def _mark_orphan(g: dict, now: int, network: str, max_flop, miner: str = None, start: int = None,
                 span: int = None) -> None:
    """Phiên kết thúc KHÔNG RÕ có mở kênh hay không (CLI bị ngắt, thành công mà thiếu id, "outcome unknown", thoát lỗi
    không rõ, lượt bị giết giữa phiên): bật tìm kênh mồ côi và GIỮ escrow của phiên đó trong `unknown_escrow` =
    [[ts, mạng, escrow, miner, mở_từ, mở_tới]]. Kênh có thể đã settle trên chain mà chưa có trong danh sách chờ; không
    giữ thì cổng đứng tên của gateway đọc phần chain tăng đó thành "gateway đã đứng tên ví mình" và xác nhận nhầm (rồi
    sổ vượt chain -> đọc thấp -> có thể mở kỳ chain mới nhầm). `start` = giờ bắt đầu phiên, `span` = thời hạn phiên:
    chỉ kênh của miner đó mở trong khoảng này mới được nhận là kênh của phiên (xem ORPHAN_MATCH_SKEW_SEC)."""
    g["orphan_since"] = min(int(g.get("orphan_since") or now), now)
    g["orphan_last"] = max(int(g.get("orphan_last") or 0), int(now))     # cửa sổ tìm gia hạn theo lần ngắt mới nhất
    held = [h for h in (g.get("unknown_escrow") or []) if _hold_ok(h)]
    try:
        esc = _flop_str(Decimal(str(max_flop))) if max_flop is not None else _flop_str(DEFAULT_ESCROW_CAP)
    except (InvalidOperation, ValueError):
        esc = _flop_str(DEFAULT_ESCROW_CAP)
    start = int(start if start is not None else now)
    span = int(span or ORPHAN_LEGACY_SPAN_SEC)
    hold = [int(now), network, esc, str(miner or ""), start - ORPHAN_MATCH_SKEW_SEC, start + span]
    g["unknown_escrow"] = (held + [hold])[-20:]


def _never_started(cmd, rc) -> bool:
    """`docker run` không khởi động được container (125 lỗi daemon, 126/127 lệnh không chạy được): CLI CHƯA chạy, chắc
    chắn chưa mở kênh nào."""
    return bool(cmd) and os.path.basename(str(cmd[0])).startswith("docker") and rc in (125, 126, 127)


def _short(cid: str) -> str:
    return str(cid or "")[:18].lower()


def _mark_done(g: dict, cid: str) -> None:
    done = [d for d in (g.get("done") or []) if isinstance(d, str) and d != _short(cid)]
    g["done"] = (done + [_short(cid)])[-DONE_KEEP:]


def _mkey(miner: str) -> str:
    """Khoá trạng thái theo MINER (khoá công khai hex) — cùng ví khác prefix SS58 là một miner."""
    pk = fg.account_pubkey(miner)
    return pk.hex() if pk else str(miner)


def run_step(state: dict, *, agent_key, now: int, network: str, fetch_channel, prompt: str = None,
             prompts=None, book_fn=None, deny=(), deadline=None, runner=None, alert=None, log=print,
             clock=time.time, offers_fn=None, price_fn=None, miner_cap_fn=None, balance_fn=None,
             indexer_url=None, own_channels_fn=None, checkpoint=None, spend_room=None) -> dict:
    """Một nhịp mua trực tiếp: tối đa FLOP_DIRECT_MAX_PER_RUN phiên, xoay vòng các miner. Trả {"outcome", ...};
    KHÔNG ném lỗi. `book_fn(amount, channel_id, model, escrow=)` ghi sổ (None = chạy tập devnet, không ghi).
    `fetch_channel(channel_id)` -> dict explorer | None. `offers_fn(miner, model_hash)` -> các dòng
    /api/session-offers của miner. `prompts` = danh sách đề (mỗi phiên một đề, xoay theo prompt_idx).
    `price_fn(model_hash)` -> FLOP/GFLOP | None; `miner_cap_fn(miner)` -> GFLOP/s đã hiệu chuẩn | None (để định cỡ
    phiên khi chưa học được từ kênh của chính mình).
    `balance_fn(address)` -> số dư khả dụng FLOP | None: escrow được thu cho vừa (trừ FLOP_DIRECT_FEE_RESERVE),
    thiếu tiền thì dừng lượt (`low_balance`) thay vì gọi CLI để nhận lỗi rồi nghỉ 1 giờ.
    `indexer_url`: explorer của mạng, đưa vào CLI qua FLOP_INDEXER_URL — CLI chỉ tự đặt cho devnet; mạng khác SDK
    tìm offer ở http://127.0.0.1:4000 và `--via offer` thất bại. `own_channels_fn(address)` -> các kênh gần đây của
    ví (explorer ?agent=): sau một lần "không rõ kết quả" để tìm kênh mồ côi (CLI bị ngắt nên không biết id).
    `alert(text)` gửi cảnh báo (Telegram). Trạng thái bền trong state[KEY].

    `checkpoint()` (caller lưu state[KEY] xuống đĩa/KV) được gọi NGAY TRƯỚC mỗi lần chạy CLI mở phiên, với mốc
    `attempt` = phiên đang chạy, trần ngày đã giữ và các kênh đã mở trước đó trong lượt; và sau mỗi phiên. Lượt bị giết
    giữa phiên (timeout job, runner mất) thì lượt sau thấy `attempt` còn đó và xử lý như "không rõ kết quả" (giữ escrow,
    tìm kênh mồ côi, chặn miner 6 giờ) thay vì quên hẳn kênh và trần ngày.

    `spend_room()` -> FLOP còn được cam kết hôm nay dưới trần CHUNG của mọi đường mua (None = không có trần chung).

    Chốt chặn theo TỪNG MINER (một miner hỏng không làm dừng miner khác): nghỉ sau lỗi / không có offer, giữ
    6 giờ khi không rõ kết quả, và không mở kênh mới với miner còn kênh LỖI chưa xong (có thể đang khoá FLOP).
    Chốt chung: trần ngày, ngân sách thời gian của lượt, số kênh chờ explorer ghi nhận (FLOP_DIRECT_MAX_PENDING),
    tuỳ chọn CLI không được hỗ trợ (dừng 24 giờ), keystore không ra đúng địa chỉ ví."""
    runner = runner or _run
    alert = alert or (lambda text: None)
    t0 = clock()                                  # giờ bắt đầu một phiên = now + (clock() - t0)
    plist = [p for p in (prompts if prompts is not None else [prompt]) if p]
    g = dict(state.get(KEY) or {})
    out = {"outcome": "?", "opened": 0, "booked": 0, "booked_flop": 0.0, "settled": 0, "sessions": []}

    def done(outcome, **kw):
        out.update(outcome=outcome, **kw)
        g["last_outcome"], g["last_run"] = outcome, now
        state[KEY] = g
        return out

    # Đối soát kênh còn chờ chạy CẢ khi đã tắt / cấu hình lỗi / mọi miner bị chặn: tắt mua trực tiếp lúc còn kênh
    # chưa settle mà bỏ luôn đối soát thì kênh PAID không bao giờ vào sổ.
    has_pending = any(isinstance(p, dict) and p.get("channel") for p in (g.get("pending") or [])) \
        or bool(g.get("orphan_since")) or bool(g.get("attempt"))
    if not enabled() and not has_pending:
        return done("off")
    wkey = fg.wallet_key(agent_key)
    if wkey is None:
        return done("unconfigured", reason="chưa có khoá ví (FLOP_WALLET_SEED_HEX hoặc FLOP_WALLET_USE_DID=on)")
    addr = fg.wallet_address(wkey)
    ms = {k: dict(v) for k, v in (g.get("miners") or {}).items() if isinstance(v, dict)}
    to_finalize = []
    # Lượt TRƯỚC bị giết giữa một phiên (mốc `attempt` lưu trước khi gọi CLI vẫn còn): không biết kênh đã mở chưa.
    att = g.pop("attempt", None)
    if isinstance(att, dict) and att.get("ts"):
        try:
            a_ts = int(att["ts"])
        except (TypeError, ValueError):
            a_ts = now
        a_miner = str(att.get("miner") or "")
        if a_miner:
            mk = _mkey(a_miner)
            ms.setdefault(mk, {})["unknown_until"] = max(int(ms[mk].get("unknown_until") or 0),
                                                         a_ts + UNKNOWN_HOLD_SEC)
        _mark_orphan(g, a_ts, str(att.get("network") or network), att.get("escrow"), miner=a_miner, start=a_ts,
                     span=att.get("span"))
        alert(f"⚠️ FLOP direct: lượt trước bị NGẮT giữa phiên với miner {a_miner[:8]}… (escrow {att.get('escrow')} "
              f"FLOP) — không biết kênh đã mở chưa. Ngừng miner này {UNKNOWN_HOLD_SEC // 3600}h, tìm kênh của ví "
              f"{addr[:8]}… trên explorer trong {ORPHAN_WINDOW_SEC // 3600}h.")
    # 0) Kênh MỒ CÔI: lượt trước CLI bị ngắt / kết quả mơ hồ nên không biết id kênh -> tìm kênh của ví mình mở từ
    # lúc đó mà chưa theo dõi, thêm vào danh sách chờ (đánh dấu lỗi: chặn miner đó tới khi kênh xong, rồi ghi sổ
    # nếu PAID). Tìm trong ORPHAN_WINDOW_SEC; thêm lại không trùng nhờ danh sách chờ + `done`.
    pending = [p for p in (g.get("pending") or []) if isinstance(p, dict) and p.get("channel")]
    since = int(g.get("orphan_since") or 0)
    # Mỗi khoản giữ hết hạn theo tuổi CỦA NÓ; cửa sổ tìm tính từ lần ngắt GẦN NHẤT. Trước đây cửa sổ neo ở lần ngắt
    # ĐẦU: lần ngắt sau đó vài giờ bị xoá cả khoản giữ lẫn việc tìm khi cửa sổ cũ hết -> kênh mồ côi mất hẳn và chi
    # on-chain của nó bị cổng gateway đọc thành "gateway đứng tên ví mình" (kiểm thử ngẫu nhiên: 84.5 FLOP).
    held_all = [h for h in (g.get("unknown_escrow") or []) if _hold_ok(h)]
    held_live = [h for h in held_all if now - int(h[0] or 0) <= ORPHAN_WINDOW_SEC]
    if len(held_live) != len(held_all):
        alert(f"⚠️ FLOP direct: {len(held_all) - len(held_live)} phiên bị ngắt không tìm thấy kênh trên explorer sau "
              f"{ORPHAN_WINDOW_SEC // 3600}h — có thể CLI bị ngắt trước khi mở kênh; nếu không, kiểm ví {addr[:8]}… "
              "trên explorer/RPC (chi tiêu đó chưa vào sổ).")
        g["unknown_escrow"] = held_live
    last = int(g.get("orphan_last") or since or 0)
    if since and now - last > ORPHAN_WINDOW_SEC and not held_live:
        g.pop("orphan_since", None)
        g.pop("orphan_last", None)
        g.pop("unknown_escrow", None)
    elif held_live and own_channels_fn is not None:
        try:
            rows = own_channels_fn(addr) or []
        except Exception as e:
            rows = []
            log(f"[direct] tìm kênh mồ côi lỗi ({type(e).__name__}) — thử lại lượt sau")
        known = {_short(p["channel"]) for p in pending} | set(g.get("done") or [])
        cands = []
        for r in rows:
            try:
                cid = str(r.get("channelId") or "")
                opened = int(r.get("openedAt") or 0) // 1000
            except (AttributeError, TypeError, ValueError):
                continue
            if not _CHANNEL_RE.match(cid) or _short(cid) in known or opened <= 0:
                continue
            if r.get("agent") and not fg.same_account(r.get("agent"), addr):
                continue
            cands.append((opened, cid, r))
        # Mỗi kênh chỉ thay cho MỘT khoản giữ mà nó khớp: cùng mạng, mở trong khoảng giờ của phiên bị ngắt, cùng miner
        # (khoản giữ dạng cũ không có miner thì bỏ qua điều kiện này). Kênh không khớp khoản giữ nào KHÔNG được nhận.
        holds = sorted(held_live, key=lambda h: int(h[0] or 0))
        for opened, cid, r in sorted(cands, key=lambda x: x[0]):
            rm = str(r.get("miner") or "")
            j = next((j for j, h in enumerate(holds)
                      if h[1] == network and _hold_window(h)[0] <= opened <= _hold_window(h)[1]
                      and not (len(h) >= 4 and h[3] and rm and not fg.same_account(h[3], rm))), None)
            if j is None:
                continue
            holds.pop(j)
            rec = {"channel": cid.lower(), "ts": opened or now, "network": network,
                   "miner": rm, "error": "mồ côi (CLI bị ngắt, tìm lại qua explorer)"}
            esc = _flop(r.get("escrow"))
            if esc:
                rec["max_flop"] = _flop_str(esc)       # escrow THẬT (cổng gateway trừ đúng số, không lấy trần)
            pending.append(rec)
            known.add(_short(cid))
            alert(f"⚠️ FLOP direct: tìm thấy kênh {cid[:18]}… của ví mình mà CLI không báo id (lượt bị ngắt) — đã đưa vào "
                  "theo dõi; PAID thì ghi sổ, còn mở thì chặn miner đó tới khi xong.")
        if len(holds) != len(held_live):
            g["unknown_escrow"] = holds                # kênh đã tìm thấy thay cho khoản giữ của nó

    # 1) Đối soát kênh đã mở ở các lượt trước.
    keep = []
    for p in pending:
        if p.get("network") and p.get("network") != network:
            # Kênh của mạng khác (chuyển devnet <-> testnet): explorer mạng này luôn 404, cũng không ghi sổ được ở
            # chế độ này — giữ lại chỉ chặn miner / chiếm chỗ FLOP_DIRECT_MAX_PENDING suốt 48 giờ.
            st = dict(g.get("stats") or {})
            st["other_network"] = int(st.get("other_network") or 0) + 1
            g["stats"] = st
            _mark_done(g, p["channel"])
            log(f"[direct] bỏ theo dõi kênh {p['channel'][:12]}… của mạng {p.get('network')} (đang chạy {network})")
            continue
        try:
            info = fetch_channel(p["channel"])
        except Exception as e:
            log(f"[direct] đọc kênh {p['channel'][:12]}… lỗi ({type(e).__name__}) — thử lại lượt sau")
            keep.append(p)
            continue
        kind, amount = classify_channel(info, addr)
        if kind == "paid":
            if p.get("network") == network and book_fn and amount > 0:
                # Settle HỢP TÁC trả hết escrow (99% miner + 1% phí kiểm toán, không về ví) -> ví mất cả escrow.
                # Settle cưỡng bức trả theo khối lượng, phần còn lại có thể hoàn -> chỉ trừ số đã trả (không đoán).
                coop = info.get("cooperative")
                esc = None
                if coop is True or (coop is None and not p.get("force")):
                    esc = _flop(info.get("escrow")) or (Decimal(str(p["max_flop"])) if p.get("max_flop") else None)
                r = book_fn(amount, p["channel"], p.get("model") or "", escrow=esc) or {}
                if r.get("outcome") not in ("direct_settled", "duplicate"):
                    log(f"[direct] ⚠️ không ghi được sổ cho kênh {p['channel'][:12]}… ({r.get('outcome')})")
                    keep.append(p)
                    continue
                if r.get("outcome") == "direct_settled":
                    out["booked"] += 1
                    out["booked_flop"] += float(amount)
            out["settled"] += 1                        # PAID trên explorer (cả khi tập devnet, không ghi sổ)
            st = dict(g.get("stats") or {})
            st["paid"] = int(st.get("paid") or 0) + 1
            st["paid_flop"] = round(float(st.get("paid_flop") or 0) + float(amount), 9)
            g["stats"] = st
            _learn(g, ms, p, info)
            _mark_done(g, p["channel"])
            log(f"[direct] kênh {p['channel'][:12]}… đã settle đứng tên ví mình: {float(amount):.6g} FLOP"
                + ("" if book_fn else " (devnet tập, không ghi sổ)"))
        elif kind == "refunded":
            st = dict(g.get("stats") or {})
            st["refunded"] = int(st.get("refunded") or 0) + 1
            g["stats"] = st
            _mark_done(g, p["channel"])
            # Escrow đã về ví: trả phần giữ trong trần NGÀY (nếu kênh mở hôm nay), kẻo miner hay hoàn kênh làm cạn trần
            # ngày mà không chi được gì (~20 lần hoàn 100 FLOP = hết 2000). Kênh mở hôm trước: trần ngày đó đã qua.
            today = time.strftime("%Y-%m-%d", time.gmtime(now))
            dd = g.get("day") if isinstance(g.get("day"), dict) else {}
            if dd.get("day") == today and time.strftime("%Y-%m-%d", time.gmtime(int(p.get("ts") or 0))) == today:
                try:
                    esc = Decimal(str(p["max_flop"])) if p.get("max_flop") else _flop((info or {}).get("escrow"))
                except (InvalidOperation, ValueError):
                    esc = None
                if esc:
                    g["day"] = {"day": today, "escrow": round(max(0.0, float(dd.get("escrow") or 0) - float(esc)), 9)}
            log(f"[direct] kênh {p['channel'][:12]}… đóng không trả tiền (hoàn escrow, 0 FLOP)")
        elif kind == "foreign":
            alert(f"⚠️ FLOP direct: kênh {p['channel'][:18]}… KHÔNG đứng tên ví agent theo explorer — không ghi sổ.")
            _mark_done(g, p["channel"])
        elif kind == "missing" and now - int(p.get("ts") or now) >= MISSING_DROP_SEC:
            # Explorer 404 cả hai ngày: chain reset / indexer mất dữ liệu / CLI báo id không tồn tại. Giữ mãi thì
            # đủ FLOP_DIRECT_MAX_PENDING là dừng hẳn (kênh lỗi thì chặn miner mãi) -> bỏ theo dõi và báo để kiểm tay.
            st = dict(g.get("stats") or {})
            st["missing"] = int(st.get("missing") or 0) + 1
            g["stats"] = st
            _mark_done(g, p["channel"])
            alert(f"⚠️ FLOP direct: kênh {p['channel'][:18]}… vẫn không có trên explorer sau "
                  f"{MISSING_DROP_SEC // 3600}h — bỏ theo dõi (không ghi sổ). Kiểm ví {addr[:8]}… trên RPC nếu nghi FLOP bị khoá.")
        else:
            age = now - int(p.get("ts") or now)
            if kind == "posted" and not p.get("force"):
                p = {**p, "force": True}            # explorer cho thấy settle cưỡng bức (kênh mồ côi / khôi phục từ KV)
            if p.get("force"):
                # Settle cưỡng bức: chờ cửa sổ tranh chấp 7 ngày là BÌNH THƯỜNG — không báo "kẹt" sau 6 giờ.
                if p.get("fin_tx"):
                    # Đã finalize thành công: chỉ chờ explorer ghi PAID, KHÔNG gọi lại (lần sau sẽ lỗi vì kênh đã xong,
                    # tốn phí và báo nhầm "thất bại"). Quá FINALIZE_RETRY_SEC × 3 mà chưa PAID thì báo một lần.
                    if now - int(p.get("fin_at") or now) >= FINALIZE_RETRY_SEC * FINALIZE_MAX_TRIES \
                            and not p.get("fin_alerted"):
                        p = {**p, "fin_alerted": now}
                        alert(f"⚠️ FLOP direct: kênh {p['channel'][:18]}… đã finalize (tx {str(p['fin_tx'])[:18]}…) "
                              f"{FINALIZE_MAX_TRIES} ngày mà explorer chưa ghi PAID — kiểm kênh trên explorer/RPC.")
                elif age >= FINALIZE_AFTER_SEC and now - int(p.get("fin_at") or 0) >= FINALIZE_RETRY_SEC \
                        and int(p.get("fin_tries") or 0) < FINALIZE_MAX_TRIES:
                    to_finalize.append(p["channel"])
                keep.append(p)
                continue
            if age >= PENDING_ALERT_SEC and not p.get("alerted"):
                p = {**p, "alerted": now}
                alert(f"⚠️ FLOP direct: kênh {p['channel'][:18]}… chưa xong sau "
                      f"{(now - int(p.get('ts') or now)) // 3600}h ({kind}). FLOP có thể đang bị khoá on-chain "
                      "tới hạn — kiểm kênh trên explorer/RPC.")
            keep.append(p)
    g["pending"] = keep[-PENDING_KEEP:]
    out["booked_flop"] = round(out["booked_flop"], 9)
    g["miners"] = ms                          # công suất vừa học không mất khi lượt dừng sớm ở các chốt dưới
    if not enabled():
        g["miners"] = ms
        return done("off", reason="đã tắt — chỉ đối soát kênh còn chờ")
    cfg, errs = config()
    if errs:
        g["miners"] = ms
        return done("unconfigured", reason="; ".join(errs)[:300])
    deny_all = [d.strip() for d in list(deny or ()) + _env("FLOP_DIRECT_DENY_MINERS").split(",")
                if d.strip() and fg.account_pubkey(d.strip()) is not None]
    miners, refused = [], []
    for m in cfg["miners"]:
        if fg.same_account(m["miner"], addr):
            refused.append("miner là chính ví agent — tự giao dịch không được tính")
        elif any(fg.same_account(d, m["miner"]) for d in deny_all):
            refused.append("miner nằm trong danh sách chặn (independent demand only)")
        else:
            miners.append(m)
    if not miners:
        g["miners"] = ms
        return done("refused", reason="; ".join(dict.fromkeys(refused))[:300])

    def mstate(m):
        return ms.setdefault(_mkey(m["miner"]), {})

    # Settle cưỡng bức đã qua cửa sổ tranh chấp -> tự finalize (tối đa FINALIZE_MAX_TRIES lần, mỗi ngày một lần).
    if to_finalize and (deadline is None or clock() + 120 + 180 * len(to_finalize) < deadline):
        results = finalize_channels(to_finalize, wkey=wkey, addr=addr, network=network, runner=runner, log=log,
                                    alert=alert)
        for p in g["pending"]:
            if p.get("channel") in results:
                ok, info_s = results[p["channel"]]
                p["fin_at"], p["fin_tries"] = now, int(p.get("fin_tries") or 0) + 1
                if ok:
                    p["fin_tx"] = info_s
                elif p["fin_tries"] >= FINALIZE_MAX_TRIES:
                    alert(f"⚠️ FLOP direct: finalize kênh settle cưỡng bức {p['channel'][:18]}… thất bại "
                          f"{p['fin_tries']} lần ({info_s[:100]}) — FLOP còn khoá, chi tiêu chưa được tính; kiểm tay.")


    # 2) Chốt chung.
    if int(g.get("unknown_until") or 0) > now:                      # bản cũ: giữ chung
        return done("hold_unknown", reason="lượt trước không rõ kết quả — chờ đối soát trước khi mở kênh mới")
    if int(g.get("wait_until") or 0) > now:
        return done("backoff", reason=f"nghỉ tới {g['wait_until']} ({g.get('wait_why', '')})")
    if not plist:
        return done("unconfigured", reason="chưa có đề bài (FLOP_GATEWAY_PROMPTS / FLOP_KIBBLE_REQUEST_QUESTIONS)")
    waiting_index = [p for p in keep if not p.get("error")]
    if len(waiting_index) >= cfg["max_pending"]:
        return done("pending", reason=(f"{len(waiting_index)} kênh chờ explorer ghi nhận (>= FLOP_DIRECT_MAX_PENDING) "
                                       "— không mở thêm"))

    def blocked(m):
        """Lý do miner này không dùng được ở lượt này, hoặc ''."""
        st = mstate(m)
        if int(st.get("unknown_until") or 0) > now:
            return "hold_unknown"
        if any(p.get("error") and fg.same_account(p.get("miner") or "", m["miner"]) for p in g["pending"]):
            return "pending"
        if int(st.get("wait_until") or 0) > now:
            return "backoff"
        return ""

    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    ds = g.get("day") if isinstance(g.get("day"), dict) and g["day"].get("day") == day else {"day": day, "escrow": 0.0}
    limit, paced = pace_limit(cfg, now, salt=addr)
    # Khi nhịp đang giới hạn: đợi tích đủ ~một phần giờ (trần/24, ≤ trần phiên) rồi mới mở, kẻo mỗi lượt 5 phút
    # mở một phiên vụn. Hết cửa sổ thì không còn ngưỡng này: phần còn thiếu thu phiên cho vừa.
    chunk = min(float(cfg["escrow_cap"]), float(cfg["per_day"]) / 24) if paced else 0.0
    cap_kind = "paced" if paced else "daily_cap"

    def cap_reason():
        if paced:
            return (f"trải đều: đến giờ này được cam kết {limit:.4g}/{cfg['per_day']:g} FLOP, đã cam kết "
                    f"{float(ds['escrow']):.4g} — lượt sau tiếp")
        return f"đã cam kết {float(ds['escrow']):.4g} FLOP hôm nay, trần {cfg['per_day']:g}"

    # Trần CHUNG mỗi ngày của cả hai đường mua (gateway + trực tiếp): caller trả số FLOP còn được cam kết lúc ĐẦU lượt
    # (đã trừ phần đường kia và phần đường này đã cam kết). Trong lượt, trừ dần escrow vừa giữ.
    try:
        room0 = spend_room() if spend_room is not None else None
        room0 = Decimal(str(room0)) if room0 is not None else None
    except Exception as e:
        room0 = Decimal(0)                                  # không tính được hạn mức chung -> an toàn: không mở phiên
        log(f"[direct] không tính được trần chi chung ({type(e).__name__}) — không mở phiên lượt này")
    esc0 = Decimal(str(ds["escrow"]))

    def combined_left():
        if room0 is None:
            return None
        return room0 - (Decimal(str(ds["escrow"])) - esc0)

    def combined_reason():
        return (f"chạm trần chi chung mỗi ngày (FLOP_MAX_SPEND_PER_DAY, cả gateway lẫn mua trực tiếp): còn "
                f"{_flop_str(max(Decimal(0), combined_left()))} FLOP")

    bal = {"read": False, "avail": None}                 # số dư đọc MỘT lần mỗi lượt, trừ dần escrow đã đưa CLI

    def avail():
        if not bal["read"]:
            bal["read"] = True
            if balance_fn is not None:
                try:
                    b = balance_fn(addr)
                    if b is not None:
                        bal["avail"] = Decimal(str(b)) - cfg["fee_reserve"]
                        out["balance"] = float(b)
                except Exception as e:
                    log(f"[direct] đọc số dư ví lỗi ({type(e).__name__}) — không thu escrow theo số dư lượt này")
        return bal["avail"]

    def low_reason(need):
        return (f"số dư khả dụng {_flop_str(max(Decimal(0), avail()))} FLOP (đã chừa {_flop_str(cfg['fee_reserve'])} "
                f"phí) < {_flop_str(Decimal(str(need)))} FLOP cần cho một phiên")

    offer_cache = {}
    work, password, ks = None, None, None
    last = ("idle", "")                                               # (kết quả, lý do) của lần thử cuối
    start = int(g.get("miner_idx") or 0)
    tried = 0
    dirty = False                                                     # có kết quả phiên chưa lưu xuống đĩa

    def save():
        """Đẩy trạng thái hiện tại cho caller lưu (checkpoint). Lỗi lưu không được làm hỏng lượt."""
        g["miners"] = ms
        state[KEY] = g
        if checkpoint is None:
            return
        try:
            checkpoint()
        except Exception as e:
            log(f"[direct] ⚠️ không lưu được mốc phiên ({type(e).__name__}: {str(e)[:80]})")

    try:
        while out["opened"] < cfg["per_run"] and tried < len(miners) * cfg["per_run"]:
            if dirty:
                save()                                                # kết quả phiên trước (kênh mới) xuống đĩa
                dirty = False
            if deadline is not None and clock() + cfg["timeout"] + 30 > deadline:
                last = ("run_budget", "không đủ thời gian của lượt cho thêm một phiên (CLI có thể chờ hạn on-chain)")
                break
            pick = None
            for i in range(len(miners)):
                m = miners[(start + tried + i) % len(miners)]
                why = blocked(m)
                if not why:
                    pick = m
                    tried += i + 1
                    break
                last = (why, f"miner {m['miner'][:8]}…: {why}")
            if pick is None:
                break
            m, st = pick, mstate(pick)
            # Escrow: cố định (FLOP_DIRECT_MAX_FLOP) hoặc theo offer miner công bố. Chế độ offer mà miner không có
            # offer -> không gọi CLI (khỏi kéo image + mở kết nối chain chỉ để nhận "no live signed offer").
            max_flop, tokens, plan = cfg["max_flop"], cfg["max_tokens"], None
            sess_timeout = cfg["timeout"]
            prompt_i = plist[int(g.get("prompt_idx") or 0) % len(plist)]
            if offers_fn is not None and (max_flop is None or cfg["via"] == "offer"):
                k = _mkey(m["miner"])
                if k not in offer_cache:
                    try:
                        offer_cache[k] = ("ok", offers_fn(m["miner"], m["model_hash"]))
                    except Exception as e:
                        offer_cache[k] = ("err", type(e).__name__)
                kind, rows = offer_cache[k]
                if kind == "err":
                    st.update(wait_until=now + 900, wait_why="offer lookup")
                    last = ("offer_lookup_error", f"không đọc được session-offers ({rows})")
                    continue
                info, why = pick_offer(rows, m["miner"], m["model_hash"])
                if info is None:
                    st.update(wait_until=now + ERROR_BACKOFF_SEC, wait_why="no offer")
                    last = ("no_offer", why)
                    continue
                if info["max_duration"]:
                    sess_timeout = max(cfg["timeout"], int(info["max_duration"]) + MAXDUR_MARGIN_SEC)
                esc = _flop_str(info["min_escrow"])
                if max_flop is None:
                    # Định cỡ theo việc thật: GFLOP/token (đã học, hoặc suy từ cỡ model), công suất miner, giá.
                    mh = info["model_hash"]
                    learned = (g.get("gnpt") or {}).get(mh)
                    size = model_size_b(m["model"])
                    gnpt = float(learned) if isinstance(learned, (int, float)) and learned > 0 else (
                        2.0 * size if size else DEFAULT_GN_PER_TOKEN)
                    cap = st.get("cap")
                    if not cap and miner_cap_fn is not None:
                        try:
                            cap = miner_cap_fn(m["miner"])
                        except Exception:
                            cap = None
                        if cap:
                            st["cap"] = float(cap)
                    price = cfg["price"]
                    if price is None and price_fn is not None:
                        try:
                            price = price_fn(mh)
                        except Exception:
                            price = None
                    if info["min_escrow"] > cfg["escrow_cap"]:
                        st.update(wait_until=now + ERROR_BACKOFF_SEC, wait_why="offer above cap")
                        last = ("no_offer", f"minEscrow của offer {_flop_str(info['min_escrow'])} FLOP > trần "
                                            f"{_flop_str(cfg['escrow_cap'])} (FLOP_DIRECT_ESCROW_CAP)")
                        continue
                    # Phần trần NGÀY còn lại cũng là trần của phiên này: thu phiên cho vừa thay vì bỏ hẳn.
                    cap_eff, limiter = cfg["escrow_cap"], None
                    if cfg["per_day"] > 0:
                        left = Decimal(str(limit)) - Decimal(str(ds["escrow"]))
                        if left <= 0 or (paced and float(left) < chunk):
                            last = (cap_kind, cap_reason())
                            break
                        if left < cap_eff:
                            cap_eff, limiter = left, cap_kind
                    # Trần CHUNG cả hai đường mua (FLOP_MAX_SPEND_PER_DAY, caller tính): cũng thu phiên cho vừa.
                    left_c = combined_left()
                    if left_c is not None:
                        if left_c <= 0:
                            last = ("daily_cap", combined_reason())
                            break
                        if left_c < cap_eff:
                            cap_eff, limiter = left_c, "combined"
                    # Số dư ví cũng vậy: chi hết phần đang có bằng một phiên nhỏ hơn, không gọi CLI để nhận lỗi thiếu tiền.
                    av = avail()
                    if av is not None:
                        if av <= 0 or av < info["min_escrow"]:
                            last = ("low_balance", low_reason(info["min_escrow"]))
                            break
                        if av < cap_eff:
                            cap_eff, limiter = av, "low_balance"
                    plan, why = plan_session(
                        prompt=prompt_i, max_tokens=cfg["max_tokens"], gn_per_token=gnpt,
                        cap_gfs=float(cap or cfg["default_cap"]), price=Decimal(str(price or DEFAULT_PRICE_PER_GN)),
                        min_escrow=info["min_escrow"], escrow_cap=cap_eff,
                        settle_target=min(cfg["settle_target"], cfg["timeout"] - 60),
                        margin=cfg["margin"], max_duration=info["min_duration"],
                        max_overpay=cfg["max_overpay"] or None, overpay_min=cfg["overpay_min"])
                    if plan is None:
                        # Hạn mức (trần ngày / trần chung / số dư) đã thu nhỏ phiên: phiên nhỏ không vừa trần escrow, hoặc
                        # minEscrow thành "quá đắt" so với phiên nhỏ — lỗi của hạn mức, không phải của miner: không phạt
                        # miner 1 giờ, chỉ dừng lượt.
                        squeezed = limiter and ("trần" in why or "quá đắt" in why)
                        if squeezed and limiter == "low_balance":
                            last = ("low_balance", f"số dư khả dụng {_flop_str(cap_eff)} FLOP không đủ một phiên")
                            break
                        if squeezed and limiter == "combined":
                            last = ("daily_cap", f"phần trần chung còn lại ({_flop_str(cap_eff)} FLOP, "
                                                 "FLOP_MAX_SPEND_PER_DAY) không đủ một phiên")
                            break
                        if squeezed:
                            last = (cap_kind, f"phần trần {'tới giờ này' if paced else 'ngày'} còn lại "
                                              f"({_flop_str(cap_eff)} FLOP) không đủ một phiên")
                            break
                        overpriced = "quá đắt" in why
                        st.update(wait_until=now + ERROR_BACKOFF_SEC,
                                  wait_why="offer overpriced" if overpriced else "session sizing")
                        if overpriced and not st.get("overpay_alerted"):
                            st["overpay_alerted"] = now
                            alert(f"⚠️ FLOP direct: KHÔNG mua của miner {m['miner'][:8]}… — {why}. Escrow là chi phí (settle "
                                  "hợp tác trả cả escrow); nới FLOP_DIRECT_MAX_OVERPAY nếu chấp nhận giá này.")
                        last = ("no_offer", why)
                        continue
                    st.pop("overpay_alerted", None)
                    max_flop, tokens = plan["escrow"], plan["max_tokens"]
                    plan = {**plan, "model_hash": mh}
                elif Decimal(max_flop) < Decimal(esc):
                    st.update(wait_until=now + ERROR_BACKOFF_SEC, wait_why="escrow below offer")
                    last = ("no_offer", f"FLOP_DIRECT_MAX_FLOP {max_flop} < minEscrow của offer {esc} — CLI sẽ không "
                                        "khớp offer; bỏ FLOP_DIRECT_MAX_FLOP để tự lấy theo offer")
                    continue
                out["escrow"], out["escrow_src"] = max_flop, ("offer" if cfg["max_flop"] is None else "fixed")
            elif max_flop is None:
                last = ("unconfigured", "chưa có FLOP_DIRECT_MAX_FLOP và không đọc được offer để định escrow")
                break
            per_session = float(max_flop)
            if cfg["per_day"] > 0 and float(ds["escrow"]) + per_session > limit + 1e-9:
                last = (cap_kind, cap_reason())
                break
            left_c = combined_left()
            if left_c is not None and Decimal(max_flop) > left_c:
                last = ("daily_cap", combined_reason())
                break
            av = avail()
            if av is not None and Decimal(max_flop) > av:
                last = ("low_balance", low_reason(max_flop))
                break
            # Lượt phải còn đủ thời gian cho CẢ thời hạn kênh (+ nạp keystore nếu chưa): bị ngắt trước khi SDK tự thu
            # hồi kênh hỏng là FLOP có thể khoá mà không ai gọi timeout.
            if deadline is not None and clock() + sess_timeout + (IMPORT_RESERVE_SEC if work is None else 0) + 30 > deadline:
                last = ("run_budget", f"không đủ thời gian của lượt cho một phiên tới {sess_timeout}s "
                                      "(thời hạn kênh của offer + biên)")
                if sess_timeout > cfg["timeout"]:
                    continue                          # offer thời hạn dài: miner khác có thể vừa
                break
            # Keystore tạm: nạp MỘT lần cho cả lượt.
            if work is None:
                work = tempfile.mkdtemp(prefix="flop-direct-")
                password = secrets.token_hex(24)
                base, ks = cli_command(work, [SECRET_ENV, PASSWORD_ENV], name=f"flop-direct-{secrets.token_hex(6)}")
                rc, so, se = runner(base + ["--output", "agent", "keys", "import", "--secret-env", SECRET_ENV,
                                            "--out", ks, "--password-env", PASSWORD_ENV, "--type", "ed25519"],
                                    {SECRET_ENV: "0x" + fg._raw_priv(wkey).hex(), PASSWORD_ENV: password}, 120)
                imported = last_json(so) or {}
                if rc != 0 or not imported.get("address"):
                    g.update(wait_until=now + ERROR_BACKOFF_SEC, wait_why="keys import")
                    last = ("error", f"keys import thất bại ({(last_json(se) or {}).get('code') or rc})")
                    break
                if not fg.same_account(imported["address"], addr):
                    g.update(wait_until=now + UNSUPPORTED_BACKOFF_SEC, wait_why="keystore address mismatch")
                    alert("⛔ FLOP direct: CLI nạp khoá ra địa chỉ KHÁC ví agent — dừng mua trực tiếp 24h, kiểm phiên bản CLI.")
                    last = ("refused", "keystore không ra đúng địa chỉ ví agent")
                    break
            env_run = {PASSWORD_ENV: password}
            if indexer_url:
                env_run["FLOP_INDEXER_URL"] = str(indexer_url).rstrip("/")
            base, ks = cli_command(work, list(env_run), name=f"flop-direct-{secrets.token_hex(6)}")
            cmd = base + ["--network", network, "--keystore", ks, "--password-env", PASSWORD_ENV,
                          "--output", "agent", "session", "run", "--miner", m["miner"], "--model", m["model"],
                          "--endpoint", m["endpoint"], "--prompt", prompt_i[:4000], "--max-flop", max_flop,
                          "--max-tokens", str(tokens), "--via", cfg["via"]]
            ds = {"day": day, "escrow": round(float(ds["escrow"]) + per_session, 9)}
            g["day"] = ds
            g["last_attempt"] = now
            if bal["avail"] is not None:
                bal["avail"] -= Decimal(max_flop)
            # Mốc "phiên đang chạy" + trần ngày đã giữ XUỐNG ĐĨA trước khi CLI có thể khoá escrow on-chain.
            sess_start = now + max(0, int(clock() - t0))
            orphan_kw = {"miner": m["miner"], "start": sess_start, "span": int(sess_timeout) + MAXDUR_MARGIN_SEC}
            g["attempt"] = {"ts": sess_start, "network": network, "miner": m["miner"], "escrow": max_flop,
                            "span": orphan_kw["span"]}
            save()
            rc, so, se = runner(cmd, env_run, sess_timeout)
            g.pop("attempt", None)
            dirty = True
            if rc is None:
                st["unknown_until"] = now + UNKNOWN_HOLD_SEC
                _mark_orphan(g, now, network, max_flop, **orphan_kw)
                alert(f"⚠️ FLOP direct: phiên với miner {m['miner'][:8]}… quá {sess_timeout}s và bị ngắt — có thể đã mở "
                      f"kênh mà không biết id. Ngừng miner này {UNKNOWN_HOLD_SEC // 3600}h; kiểm ví {addr[:8]}… trên "
                      "explorer/RPC.")
                last = ("unknown_outcome", "CLI quá giờ, không rõ kết quả")
                continue
            if rc == 0:
                res = last_json(so) or {}
                ch = str(res.get("channel") or "")
                if not _CHANNEL_RE.match(ch):
                    st["unknown_until"] = now + UNKNOWN_HOLD_SEC
                    _mark_orphan(g, now, network, max_flop, **orphan_kw)
                    alert(f"⚠️ FLOP direct: CLI báo thành công nhưng không trả id kênh hợp lệ (miner {m['miner'][:8]}…) — "
                          "ngừng miner này, kiểm tay.")
                    last = ("unknown_outcome", "thành công nhưng thiếu id kênh")
                    continue
                rec = {"channel": ch, "ts": now, "network": network, "miner": m["miner"], "model": m["model"],
                       "settle_tx": str(res.get("settle_tx") or "")[:80], "max_flop": max_flop}
                if res.get("cooperative") is False:
                    rec["force"] = True              # chờ cửa sổ tranh chấp rồi tự finalize
                if plan:
                    rec.update(model_hash=plan["model_hash"], ptok=plan["ptok"], gn_est=plan["gn_est"])
                try:
                    rec["completion_tokens"] = int(str(res.get("completion_tokens")))
                except (TypeError, ValueError):
                    pass
                # Escrow đã trả (settle hợp tác trả CẢ escrow) mà không có câu trả lời: 0 token, hoặc CLI có trường
                # `reply` mà rỗng. Đếm liên tiếp theo miner; tới EMPTY_STREAK_MAX thì ngừng miner đó EMPTY_HOLD_SEC.
                if rec.get("completion_tokens") == 0 or ("reply" in res and not str(res.get("reply") or "").strip()):
                    rec["empty"] = True
                    st["empty_streak"] = int(st.get("empty_streak") or 0) + 1
                    out["empty_sessions"] = int(out.get("empty_sessions") or 0) + 1
                    log(f"[direct] ⚠️ phiên với miner {m['miner'][:8]}… trả escrow {max_flop} FLOP mà KHÔNG có câu trả lời "
                        f"({st['empty_streak']} lần liền)")
                    if st["empty_streak"] >= EMPTY_STREAK_MAX:
                        st.update(wait_until=now + EMPTY_HOLD_SEC, wait_why="empty answers")
                        alert(f"⚠️ FLOP direct: miner {m['miner'][:8]}… nhận escrow {st['empty_streak']} phiên liền mà không trả "
                              f"lời — ngừng mua của miner này {EMPTY_HOLD_SEC // 3600}h.")
                        st["empty_streak"] = 0
                else:
                    st.pop("empty_streak", None)
                g["pending"] = g["pending"] + [rec]
                g["prompt_idx"] = int(g.get("prompt_idx") or 0) + 1
                out["opened"] += 1
                out["sessions"].append({"miner": m["miner"], "channel": ch, "escrow": max_flop, "max_tokens": tokens})
                st["sessions"] = int(st.get("sessions") or 0) + 1
                log(f"[direct] phiên với miner {m['miner'][:8]}… xong, kênh {ch[:12]}… "
                    f"({res.get('completion_tokens') or '?'} token, escrow {max_flop}) — chờ explorer finalized để ghi sổ")
                last = ("opened", "")
                continue
            err = last_json(se) or {}
            msg = str(err.get("message") or se.strip()[-200:] or f"exit {rc}")[:240]
            ch = str(err.get("channel") or "")
            has_ch = bool(_CHANNEL_RE.match(ch))
            low = msg.lower()
            unsupported = "unknown option" in low or "unknown command" in low      # lỗi cú pháp: CLI chưa làm gì
            ambiguous = "outcome unknown" in low or "recovery failed" in low
            # Từ chối RÕ RÀNG trước khi có kênh: JSON ok:false không mơ hồ, lỗi cú pháp, hoặc Docker không khởi động được
            # container. Còn lại (thoát lỗi KHÔNG kèm JSON: container bị giết 137, Node crash...) = KHÔNG RÕ: CLI có thể đã
            # mở kênh trước khi chết. Trước đây nhánh này chỉ nghỉ 1 giờ, kênh (nếu có) không bao giờ được tìm lại.
            refused = not ambiguous and (unsupported or err.get("ok") is False or _never_started(cmd, rc))
            if has_ch:
                g["pending"] = g["pending"] + [{"channel": ch, "ts": now, "network": network, "miner": m["miner"],
                                                "model": m["model"], "error": msg[:120], "max_flop": max_flop}]
            elif refused:
                # CLI từ chối RÕ RÀNG trước khi có kênh (vd. ví thiếu FLOP) -> không khoá đồng nào: trả lại phần đã giữ
                # trong trần ngày, kẻo vài lần lỗi "ăn" hết trần dù chưa chi gì. Mơ hồ (quá giờ/không JSON) thì giữ.
                ds = {"day": day, "escrow": round(max(0.0, float(ds["escrow"]) - per_session), 9)}
                g["day"] = ds
                if bal["avail"] is not None:
                    bal["avail"] += Decimal(max_flop)
            if unsupported:
                g.update(wait_until=now + UNSUPPORTED_BACKOFF_SEC, wait_why="CLI unsupported option")
                alert(f"⛔ FLOP direct: CLI không nhận tuỳ chọn ({msg[:120]}) — dừng 24h, kiểm phiên bản image/CLI.")
                last = ("unsupported", msg)
                break
            if ambiguous or not (has_ch or refused):
                st["unknown_until"] = now + UNKNOWN_HOLD_SEC
                if not has_ch:
                    _mark_orphan(g, now, network, max_flop, **orphan_kw)
                if ambiguous:
                    alert(f"⚠️ FLOP direct (miner {m['miner'][:8]}…): {msg[:160]}" + (f" — kênh {ch[:18]}…" if ch else ""))
                else:
                    alert(f"⚠️ FLOP direct: CLI thoát mã {rc} không kèm kết quả rõ ràng (miner {m['miner'][:8]}…: "
                          f"{msg[:120]}) — có thể đã mở kênh. Ngừng miner này {UNKNOWN_HOLD_SEC // 3600}h, tìm kênh của "
                          f"ví {addr[:8]}… trên explorer.")
                last = ("unknown_outcome", msg)
                continue
            st.update(wait_until=now + ERROR_BACKOFF_SEC, wait_why=str(err.get("code") or "session failed")[:60])
            last = ("no_offer" if "no live signed offer" in msg.lower() else "error", msg)
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
        g["miners"] = ms
        g["miner_idx"] = (start + tried) % max(1, len(miners))
        state[KEY] = g                     # ngoại lệ giữa phiên: mốc `attempt` vẫn tới tay caller để lưu
    if out["opened"]:
        return done("opened", channel=out["sessions"][-1]["channel"],
                    reason=(None if last[0] == "opened" else f"dừng: {last[1]}"))
    return done(last[0], reason=last[1] or None)
