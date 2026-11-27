"""Nhận 10 FLOP testnet MỘT LẦN từ IP của máy đang chạy, cho ví agent (làn agent của faucet chính thức).

Vì sao có script này: faucet giới hạn theo IP (`/limits` -> faucet.perClient: 3 lần/IP/ngày, 20 lần/dải IP/ngày).
Runner GitHub dùng chung dải IP với rất nhiều repo nên lần xin của agent trên Actions hay bị 5xx/hết lượt. Chạy
script này trên máy nhà (sau khi đủ 24 giờ kể từ lần nhận trước) là cách chắc chắn nhất.

An toàn:
- Seed nhập ẨN (getpass), chỉ nằm trong RAM, không ghi file, không in ra, không đi qua dòng lệnh.
- Địa chỉ suy ra phải đúng ví mong đợi (`--expect <SS58>`); không truyền thì hỏi xác nhận trước khi ký.
- Dùng đúng logic đã review của agent (flop_agent_claim.run_step): kiểm /chain testnet + genesis, /limits open,
  ký `flop-faucet-agent/v1|<genesis>|<address>|<nonce>`, POST /v1/testnet/agent-claims. Lỗi tạm thời (faucet bận,
  5xx) thì gửi lại CÙNG nonce — faucet hứa không trả hai lần. Bị từ chối (vd. chưa đủ 24 giờ, chưa chi lần trước)
  thì dừng, không gõ lặp.
- Chạy song song với agent không sao: faucet chỉ cấp 10 FLOP / ví / 24 giờ, và agent tự phát hiện khoản vừa nhận
  trên chain (incoming_grants) nên không xin thừa.

Cách chạy (từ thư mục repo):  python tools/claim_faucet.py --expect <địa chỉ ví agent>
Dán seed bằng CHUỘT PHẢI trong PowerShell (Ctrl+V không dán được vào ô nhập ẩn).
"""
import argparse
import getpass
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

import flop_agent_claim as fac  # noqa: E402
import flop_gateway  # noqa: E402

UA = "technocore-agent-manual-claim"
TRIES = 15
PAUSE_SEC = 20
# Kết quả tạm thời của run_step (faucet quá tải làm /chain hoặc /limits đọc hụt, hoặc 5xx lúc POST): thử lại.
TRANSIENT = ("retry:", "error-post", "error-read", "chain-mismatch:None", "faucet-closed:None", "faucet-closed:unknown")


def http_get(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def http_post(url, body):
    r = requests.post(url, json=body, headers={"User-Agent": UA}, timeout=30)
    try:
        data = r.json() if r.content else None
    except ValueError:
        data = None
    return r.status_code, data, dict(r.headers)


def read_seed(prompt=getpass.getpass):
    """-> khoá ed25519 | None. Bỏ khoảng trắng, dấu nháy, tiền tố 0x và ký tự điều khiển (Ctrl+V sinh ra \\x16)."""
    raw = prompt("Dán seed ví (64 ký tự hex, sẽ không hiện ra) rồi Enter: ")
    raw = "".join(ch for ch in raw if ch.isprintable() and not ch.isspace()).strip("'\"")
    if raw[:2].lower() == "0x":
        raw = raw[2:]
    try:
        seed = bytes.fromhex(raw)
    except ValueError:
        bad = sum(1 for ch in raw if ch not in "0123456789abcdefABCDEF")
        print(f"Seed không phải hex: nhận {len(raw)} ký tự, {bad} ký tự lạ (cần đúng 64 ký tự 0-9 a-f). Dừng.")
        if not raw:
            print("→ Không dán được gì: clipboard trống hoặc đã bấm Ctrl+V. Copy lại seed rồi dán bằng chuột phải.")
        return None
    if len(seed) != 32:
        print(f"Seed phải đúng 64 ký tự hex, nhận được {len(seed) * 2} ký tự. Dừng.")
        return None
    return Ed25519PrivateKey.from_private_bytes(seed)


def main(argv=None, *, prompt=getpass.getpass, confirm=input, get=http_get, post=http_post, sleep=time.sleep):
    ap = argparse.ArgumentParser(description="Nhận 10 FLOP testnet một lần cho ví agent, từ IP máy này.")
    ap.add_argument("--expect", default="", help="địa chỉ SS58 của ví agent; seed phải suy ra đúng địa chỉ này")
    args = ap.parse_args(argv)
    print("Mẹo: dán bằng CHUỘT PHẢI (Ctrl+V không dán được vào ô ẩn của PowerShell).")
    key = read_seed(prompt)
    if key is None:
        return 1
    addr = flop_gateway.wallet_address(key)
    if args.expect:
        if addr != args.expect.strip():
            print(f"Địa chỉ suy ra {addr[:8]}…{addr[-4:]} KHÁC ví mong đợi. Sai seed — dừng, không ký.")
            return 1
    elif confirm(f"Seed suy ra ví {addr}. Đúng ví agent? (y/N): ").strip().lower() not in ("y", "yes"):
        print("Không xác nhận — dừng, không ký.")
        return 1
    print(f"Ví {addr[:8]}…{addr[-4:]}. Đang nhận…")
    rec = {}
    for i in range(TRIES):
        rec["next_ts"] = 0
        rec, outcome, _alert = fac.run_step(rec, key, addr, time.time(), get, post)
        print(f"[{i + 1}/{TRIES}] Kết quả: {outcome}")
        if outcome == "claimed":
            tx = (rec.get("claims") or [{}])[-1].get("tx", "")
            print(f"Tx: {tx}")
            if tx:
                print(f"Xem: https://testnet.flopscan.io/transactions/{tx}")
            return 0
        if not outcome.startswith(TRANSIENT):
            return 2
        print(f"Faucet đang bận, chờ {PAUSE_SEC} giây rồi thử lại (giữ nguyên nonce)…")
        sleep(PAUSE_SEC)
    return 2


if __name__ == "__main__":
    code = main()
    try:
        input("Xong. Nhấn Enter để đóng (seed bị xoá khỏi bộ nhớ khi chương trình thoát)…")
    except EOFError:
        pass
    sys.exit(code)
