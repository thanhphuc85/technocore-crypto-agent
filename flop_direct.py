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

import json
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
DEFAULT_ESCROW_CAP = Decimal("1")  # trần escrow MỘT phiên khi tự lấy theo offer (kênh devnet thật: 0.2–1.05 FLOP)
HARD_MAX_FLOP = Decimal("5")       # trần cứng mỗi phiên, kể cả khi cấu hình sai
_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
DEFAULT_MAX_PER_DAY = 25.0         # FLOP escrow tối đa mỗi ngày UTC
DEFAULT_TIMEOUT_SEC = 600          # CLI tự chờ hạn on-chain rồi thu hồi khi miner không xác nhận
ERROR_BACKOFF_SEC = 3600
UNKNOWN_HOLD_SEC = 6 * 3600        # không rõ kết quả (CLI bị ngắt): không mở kênh mới chừng này
UNSUPPORTED_BACKOFF_SEC = 24 * 3600
PENDING_ALERT_SEC = 6 * 3600       # kênh chưa xong quá chừng này -> báo (có thể đang bị khoá)
PENDING_KEEP = 50
SECRET_ENV = "FLOP_DIRECT_IMPORT_SECRET"
PASSWORD_ENV = "FLOP_DIRECT_KEYSTORE_PASSWORD"
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CHANNEL_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _num(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return float(default)


def enabled() -> bool:
    return _env("FLOP_DIRECT_ENABLED").lower() in ("1", "true", "on", "yes")


def config() -> tuple:
    """(cấu hình, [lỗi]). Thiếu/sai bất kỳ trường bắt buộc nào -> có lỗi, không mở kênh."""
    errs = []
    miner = _env("FLOP_DIRECT_MINER")
    if not miner:
        errs.append("thiếu FLOP_DIRECT_MINER (địa chỉ SS58 của miner)")
    elif fg.account_pubkey(miner) is None:
        errs.append("FLOP_DIRECT_MINER không phải địa chỉ SS58 hợp lệ")
    endpoint = _env("FLOP_DIRECT_ENDPOINT")
    if not endpoint:
        errs.append("thiếu FLOP_DIRECT_ENDPOINT (HTTPS endpoint phiên của miner)")
    elif not re.match(r"^https://[^\s/]+", endpoint):
        errs.append("FLOP_DIRECT_ENDPOINT phải là https://")
    model = _env("FLOP_DIRECT_MODEL")
    if not model:
        errs.append("thiếu FLOP_DIRECT_MODEL (slug model miner phục vụ)")
    elif not _MODEL_RE.match(model):
        errs.append("FLOP_DIRECT_MODEL có ký tự lạ")
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
    model_hash = _env("FLOP_DIRECT_MODEL_HASH").lower()
    if model_hash and not _HASH_RE.match(model_hash):
        errs.append("FLOP_DIRECT_MODEL_HASH phải là 0x + 64 ký tự hex")
    via = (_env("FLOP_DIRECT_VIA") or "offer").lower()
    if via not in ("offer", "auto", "force"):
        errs.append("FLOP_DIRECT_VIA phải là offer | auto | force")
    elif via in ("auto", "force") and _env("FLOP_DIRECT_ALLOW_FORCE").lower() not in ("1", "true", "on", "yes"):
        # auto rơi về force khi không có offer: cùng rủi ro khoá ẩn on-chain.
        errs.append(f"FLOP_DIRECT_VIA={via} có thể mở kênh cưỡng bức (khoá FLOP tới hạn nếu miner không "
                    "xác nhận) — cần FLOP_DIRECT_ALLOW_FORCE=on")
    try:
        max_tokens = int(_env("FLOP_DIRECT_MAX_TOKENS") or 256)
        if max_tokens <= 0:
            raise ValueError
    except ValueError:
        errs.append("FLOP_DIRECT_MAX_TOKENS phải là số nguyên dương")
        max_tokens = 256
    cfg = {"miner": miner, "endpoint": endpoint, "model": model, "model_hash": model_hash or None,
           "max_flop": format(max_flop, "f") if max_flop is not None else None,
           "escrow_cap": escrow_cap,
           "max_tokens": max_tokens, "via": via,
           "per_run": max(1, int(_num("FLOP_DIRECT_MAX_PER_RUN", 1))),
           "per_day": _num("FLOP_DIRECT_MAX_PER_DAY", DEFAULT_MAX_PER_DAY),
           "timeout": max(60, int(_num("FLOP_DIRECT_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)))}
    return cfg, errs


def _flop(raw) -> Decimal:
    """Số raw (10^18 = 1 FLOP, chuỗi số nguyên) -> Decimal FLOP chính xác; None nếu rác."""
    try:
        v = int(str(raw))
    except (TypeError, ValueError):
        return None
    return (Decimal(v) / Decimal(10) ** 18) if v > 0 else None


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
        offers.append((h, esc))
    if not offers:
        return None, "miner chưa công bố offer nào (session-offers trống)", None
    if model_hash:
        offers = [x for x in offers if x[0] == model_hash]
        if not offers:
            return None, "miner không có offer cho FLOP_DIRECT_MODEL_HASH", model_hash
    hashes = sorted({h for h, _ in offers})
    if len(hashes) > 1:
        return None, (f"miner công bố offer cho {len(hashes)} model — đặt FLOP_DIRECT_MODEL_HASH (không đoán model "
                      "theo tên)"), None
    esc = min(e for _, e in offers)
    if esc > cap:
        return None, f"minEscrow của offer {_flop_str(esc)} FLOP > trần {_flop_str(cap)} (FLOP_DIRECT_ESCROW_CAP)", hashes[0]
    return _flop_str(esc), f"escrow theo offer {_flop_str(esc)} FLOP", hashes[0]


def cli_command(workdir: str, env_names) -> tuple:
    """(lệnh gốc, đường dẫn keystore mà CLI thấy). Mặc định image Docker ghim digest; FLOP_DIRECT_CLI =
    lệnh cục bộ (vd `node /path/cli.js`) cho môi trường không có Docker. Biến môi trường chỉ truyền TÊN
    (`-e NAME`) — giá trị không bao giờ nằm trên dòng lệnh."""
    local = _env("FLOP_DIRECT_CLI")
    if local:
        return shlex.split(local), os.path.join(workdir, "keystore.json")
    image = _env("FLOP_DIRECT_IMAGE") or DEFAULT_IMAGE
    cmd = ["docker", "run", "--rm", "--network", "host", "-v", f"{workdir}:/work"]
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
    'refunded' (đóng mà không trả), 'foreign' (không đứng tên ví mình), 'open' (chưa xong), 'missing'."""
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
        return "paid", raw / 1e18
    if final and status in ("settled", "closed", "refunded", "expired", "timedout", "timed_out"):
        return "refunded", 0.0
    return "open", 0.0


def run_step(state: dict, *, agent_key, now: int, prompt: str, network: str, fetch_channel,
             book_fn=None, deny=(), deadline=None, runner=None, alert=None, log=print,
             clock=time.time, offers_fn=None) -> dict:
    """Một nhịp mua trực tiếp. Trả {"outcome", ...}; KHÔNG ném lỗi. `book_fn(amount, channel_id, model)`
    ghi sổ (None = chạy tập devnet, không ghi). `fetch_channel(channel_id)` -> dict explorer | None.
    `offers_fn(miner, model_hash)` -> các dòng /api/session-offers của miner (để định escrow và để KHÔNG gọi CLI
    khi miner không có offer). `alert(text)` gửi cảnh báo (Telegram). Trạng thái bền trong state[KEY]."""
    runner = runner or _run
    alert = alert or (lambda text: None)
    g = dict(state.get(KEY) or {})
    out = {"outcome": "?", "opened": 0, "booked": 0, "booked_flop": 0.0}

    def done(outcome, **kw):
        out.update(outcome=outcome, **kw)
        g["last_outcome"], g["last_run"] = outcome, now
        state[KEY] = g
        return out

    if not enabled():
        return done("off")
    cfg, errs = config()
    if errs:
        return done("unconfigured", reason="; ".join(errs)[:300])
    wkey = fg.wallet_key(agent_key)
    if wkey is None:
        return done("unconfigured", reason="chưa có khoá ví (FLOP_WALLET_SEED_HEX hoặc FLOP_WALLET_USE_DID=on)")
    addr = fg.wallet_address(wkey)
    if fg.same_account(cfg["miner"], addr):
        return done("refused", reason="miner là chính ví agent — tự giao dịch không được tính")
    denied = [d for d in (list(deny or ()) + [x for x in _env("FLOP_DIRECT_DENY_MINERS").split(",") if x.strip()])
              if fg.account_pubkey(d.strip()) is not None and fg.same_account(d.strip(), cfg["miner"])]
    if denied:
        return done("refused", reason="miner nằm trong danh sách chặn (independent demand only)")

    # 1) Đối soát kênh đã mở ở các lượt trước.
    pending = [p for p in (g.get("pending") or []) if isinstance(p, dict) and p.get("channel")]
    keep = []
    for p in pending:
        try:
            info = fetch_channel(p["channel"])
        except Exception as e:
            log(f"[direct] đọc kênh {p['channel'][:12]}… lỗi ({type(e).__name__}) — thử lại lượt sau")
            keep.append(p)
            continue
        kind, amount = classify_channel(info, addr)
        if kind == "paid":
            if p.get("network") == network and book_fn and amount > 0:
                r = book_fn(amount, p["channel"], p.get("model") or cfg["model"]) or {}
                if r.get("outcome") in ("direct_settled", "duplicate"):
                    out["booked"] += 1 if r.get("outcome") == "direct_settled" else 0
                    out["booked_flop"] += amount if r.get("outcome") == "direct_settled" else 0.0
                else:
                    log(f"[direct] ⚠️ không ghi được sổ cho kênh {p['channel'][:12]}… ({r.get('outcome')})")
                    keep.append(p)
                    continue
            st = dict(g.get("stats") or {})
            st["paid"] = int(st.get("paid") or 0) + 1
            st["paid_flop"] = round(float(st.get("paid_flop") or 0) + amount, 9)
            g["stats"] = st
            log(f"[direct] kênh {p['channel'][:12]}… đã settle đứng tên ví mình: {amount:.6g} FLOP"
                + ("" if book_fn else " (devnet tập, không ghi sổ)"))
        elif kind == "refunded":
            st = dict(g.get("stats") or {})
            st["refunded"] = int(st.get("refunded") or 0) + 1
            g["stats"] = st
            log(f"[direct] kênh {p['channel'][:12]}… đóng không trả tiền (hoàn escrow, 0 FLOP)")
        elif kind == "foreign":
            alert(f"⚠️ FLOP direct: kênh {p['channel'][:18]}… KHÔNG đứng tên ví agent theo explorer — không ghi sổ.")
        else:
            if now - int(p.get("ts") or now) >= PENDING_ALERT_SEC and not p.get("alerted"):
                p = {**p, "alerted": now}
                alert(f"⚠️ FLOP direct: kênh {p['channel'][:18]}… chưa xong sau "
                      f"{(now - int(p.get('ts') or now)) // 3600}h ({kind}). FLOP có thể đang bị khoá on-chain "
                      "tới hạn — kiểm kênh trên explorer/RPC.")
            keep.append(p)
    g["pending"] = keep[-PENDING_KEEP:]
    if out["booked"]:
        out["booked_flop"] = round(out["booked_flop"], 9)

    # 2) Chốt chặn trước khi mở kênh mới.
    if int(g.get("unknown_until") or 0) > now:
        return done("hold_unknown", reason="lượt trước không rõ kết quả — chờ đối soát trước khi mở kênh mới")
    if int(g.get("wait_until") or 0) > now:
        return done("backoff", reason=f"nghỉ tới {g['wait_until']} ({g.get('wait_why', '')})")
    if keep:
        return done("pending", reason=f"{len(keep)} kênh chưa xong — không mở kênh mới chồng lên")
    if not prompt:
        return done("unconfigured", reason="chưa có đề bài (FLOP_GATEWAY_PROMPTS / FLOP_KIBBLE_REQUEST_QUESTIONS)")
    # Escrow: cố định (FLOP_DIRECT_MAX_FLOP) hoặc theo offer miner công bố. Chế độ offer mà miner không có offer
    # -> không gọi CLI (khỏi kéo image + mở kết nối chain mỗi lượt chỉ để nhận "no live signed offer").
    max_flop = cfg["max_flop"]
    if offers_fn is not None and (max_flop is None or cfg["via"] == "offer"):
        try:
            rows = offers_fn(cfg["miner"], cfg["model_hash"])
        except Exception as e:
            g.update(wait_until=now + 900, wait_why="offer lookup")
            return done("offer_lookup_error", reason=f"không đọc được session-offers ({type(e).__name__})")
        esc, why, mh = choose_escrow(rows, cfg["miner"], cfg["model_hash"], cfg["escrow_cap"])
        if esc is None:
            g.update(wait_until=now + ERROR_BACKOFF_SEC, wait_why="no offer")
            return done("no_offer", reason=why)
        if max_flop is None:
            max_flop = esc
        elif Decimal(max_flop) < Decimal(esc):
            g.update(wait_until=now + ERROR_BACKOFF_SEC, wait_why="escrow below offer")
            return done("no_offer", reason=f"FLOP_DIRECT_MAX_FLOP {max_flop} < minEscrow của offer {esc} — CLI sẽ không "
                                           "khớp offer; bỏ FLOP_DIRECT_MAX_FLOP để tự lấy theo offer")
        out["escrow"], out["escrow_src"] = max_flop, ("offer" if cfg["max_flop"] is None else "fixed")
    elif max_flop is None:
        return done("unconfigured", reason="chưa có FLOP_DIRECT_MAX_FLOP và không đọc được offer để định escrow")
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    ds = g.get("day") if isinstance(g.get("day"), dict) and g["day"].get("day") == day else {"day": day, "escrow": 0.0}
    per_session = float(max_flop)
    if cfg["per_day"] > 0 and float(ds["escrow"]) + per_session > cfg["per_day"] + 1e-9:
        g["day"] = ds
        return done("daily_cap", reason=f"đã cam kết {ds['escrow']:.4g} FLOP hôm nay, trần {cfg['per_day']:g}")
    if deadline is not None and clock() + cfg["timeout"] + 30 > deadline:
        return done("run_budget", reason="không đủ thời gian của lượt cho một phiên (CLI có thể chờ hạn on-chain)")

    # 3) Keystore tạm + phiên.
    work = tempfile.mkdtemp(prefix="flop-direct-")
    try:
        password = secrets.token_hex(24)
        base, ks = cli_command(work, [SECRET_ENV, PASSWORD_ENV])
        rc, so, se = runner(base + ["--output", "agent", "keys", "import", "--secret-env", SECRET_ENV,
                                    "--out", ks, "--password-env", PASSWORD_ENV, "--type", "ed25519"],
                            {SECRET_ENV: "0x" + fg._raw_priv(wkey).hex(), PASSWORD_ENV: password}, 120)
        imported = last_json(so) or {}
        if rc != 0 or not imported.get("address"):
            g.update(wait_until=now + ERROR_BACKOFF_SEC, wait_why="keys import")
            return done("error", reason=f"keys import thất bại ({(last_json(se) or {}).get('code') or rc})")
        if not fg.same_account(imported["address"], addr):
            g.update(wait_until=now + UNSUPPORTED_BACKOFF_SEC, wait_why="keystore address mismatch")
            alert("⛔ FLOP direct: CLI nạp khoá ra địa chỉ KHÁC ví agent — dừng mua trực tiếp 24h, kiểm phiên bản CLI.")
            return done("refused", reason="keystore không ra đúng địa chỉ ví agent")
        base, ks = cli_command(work, [PASSWORD_ENV])
        cmd = base + ["--network", network, "--keystore", ks, "--password-env", PASSWORD_ENV,
                      "--output", "agent", "session", "run", "--miner", cfg["miner"], "--model", cfg["model"],
                      "--endpoint", cfg["endpoint"], "--prompt", prompt[:4000], "--max-flop", max_flop,
                      "--max-tokens", str(cfg["max_tokens"]), "--via", cfg["via"]]
        g["day"] = {"day": day, "escrow": round(float(ds["escrow"]) + per_session, 9)}
        g["last_attempt"] = now
        rc, so, se = runner(cmd, {PASSWORD_ENV: password}, cfg["timeout"])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if rc is None:
        g["unknown_until"] = now + UNKNOWN_HOLD_SEC
        alert(f"⚠️ FLOP direct: phiên quá {cfg['timeout']}s và bị ngắt — có thể đã mở kênh mà không biết id. "
              f"Ngừng mở kênh mới {UNKNOWN_HOLD_SEC // 3600}h; kiểm ví {addr[:8]}… trên explorer/RPC.")
        return done("unknown_outcome", reason="CLI quá giờ, không rõ kết quả")
    if rc == 0:
        res = last_json(so) or {}
        ch = str(res.get("channel") or "")
        if not _CHANNEL_RE.match(ch):
            g["unknown_until"] = now + UNKNOWN_HOLD_SEC
            alert("⚠️ FLOP direct: CLI báo thành công nhưng không trả id kênh hợp lệ — ngừng mở kênh mới, kiểm tay.")
            return done("unknown_outcome", reason="thành công nhưng thiếu id kênh")
        g["pending"] = (g.get("pending") or []) + [{"channel": ch, "ts": now, "network": network,
                                                    "model": cfg["model"], "settle_tx": str(res.get("settle_tx") or "")[:80],
                                                    "max_flop": max_flop}]
        g["prompt_idx"] = int(g.get("prompt_idx") or 0) + 1
        log(f"[direct] phiên với miner {cfg['miner'][:8]}… xong, kênh {ch[:12]}… "
            f"({res.get('completion_tokens') or '?'} token) — chờ explorer finalized để ghi sổ")
        return done("opened", opened=1, channel=ch)
    err = last_json(se) or {}
    msg = str(err.get("message") or se.strip()[-200:] or f"exit {rc}")[:240]
    ch = str(err.get("channel") or "")
    if _CHANNEL_RE.match(ch):
        g["pending"] = (g.get("pending") or []) + [{"channel": ch, "ts": now, "network": network,
                                                    "model": cfg["model"], "error": msg[:120]}]
    if "unknown option" in msg.lower() or "unknown command" in msg.lower():
        g.update(wait_until=now + UNSUPPORTED_BACKOFF_SEC, wait_why="CLI unsupported option")
        alert(f"⛔ FLOP direct: CLI không nhận tuỳ chọn ({msg[:120]}) — dừng 24h, kiểm phiên bản image/CLI.")
        return done("unsupported", reason=msg)
    if "outcome unknown" in msg.lower() or "recovery failed" in msg.lower():
        g["unknown_until"] = now + UNKNOWN_HOLD_SEC
        alert(f"⚠️ FLOP direct: {msg[:160]}" + (f" — kênh {ch[:18]}…" if ch else ""))
        return done("unknown_outcome", reason=msg)
    g.update(wait_until=now + ERROR_BACKOFF_SEC, wait_why=str(err.get("code") or "session failed")[:60])
    return done("no_offer" if "no live signed offer" in msg.lower() else "error", reason=msg,
                channel=ch or None)
