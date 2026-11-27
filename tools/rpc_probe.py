"""Thử kết nối RPC testnet FLOP từ máy đang chạy (dùng trên GitHub Actions để xem IP runner có bị chặn không).

KHÔNG dùng seed/secret nào. Chỉ thư viện chuẩn: HTTP JSON-RPC + websocket tự viết (giống cách CLI nối wss).
"""
import base64
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.request

HOST = os.environ.get("PROBE_HOST", "testnet.flop.network")
UA = "technocore-rpc-probe"
TRIES = int(os.environ.get("PROBE_TRIES", "5"))
CRLF2 = b"\r\n\r\n"


def http_rpc(method):
    req = urllib.request.Request(f"https://{HOST}/", data=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": []}).encode(),
        headers={"User-Agent": UA, "Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return f"HTTP {r.status} {time.time() - t:.2f}s {r.read(120).decode(errors='replace')}"
    except Exception as e:
        return f"HTTP LỖI sau {time.time() - t:.2f}s: {e!r}"[:300]


class _Reader:
    def __init__(self, sock, buf=b""):
        self.s, self.buf = sock, buf

    def take(self, n):
        while len(self.buf) < n:
            c = self.s.recv(4096)
            if not c:
                raise ConnectionError("đóng kết nối")
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def ws_rpc(method, timeout=10.0):
    """Mở wss, gửi 1 lệnh JSON-RPC, đọc khung trả lời. Ngưỡng 10 s = ngưỡng của CLI."""
    t = time.time()
    stage = "tcp"
    try:
        raw = socket.create_connection((HOST, 443), timeout=timeout)
        stage = "tls"
        s = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
        s.settimeout(timeout)
        t_tls = time.time() - t
        stage = "upgrade"
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET / HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: {UA}\r\n\r\n").encode())
        hdr = b""
        while CRLF2 not in hdr:
            c = s.recv(1024)
            if not c:
                raise ConnectionError("đóng khi upgrade")
            hdr += c
        head, rest = hdr.split(CRLF2, 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            return f"WS từ chối sau {time.time() - t:.2f}s: {status} | {head[:300]!r}"
        t_up = time.time() - t
        stage = "rpc"
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": []}).encode()
        mask = os.urandom(4)
        frame = bytes([0x81, 0x80 | len(payload)]) if len(payload) < 126 else \
            bytes([0x81, 0x80 | 126]) + struct.pack(">H", len(payload))
        s.sendall(frame + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        rd, body = _Reader(s, rest), ""
        for _ in range(10):                            # bỏ qua khung rỗng/ping cho tới khung văn bản có nội dung
            h = rd.take(2)
            n = h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", rd.take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", rd.take(8))[0]
            data = rd.take(n)
            if (h[0] & 0x0F) == 1 and data:
                body = data.decode(errors="replace")
                break
        s.close()
        return f"WS OK tls={t_tls:.2f}s upgrade={t_up:.2f}s tổng={time.time() - t:.2f}s {body[:100]}"
    except Exception as e:
        return f"WS LỖI ở bước {stage} sau {time.time() - t:.2f}s: {e!r}"[:300]


def trace():
    try:
        req = urllib.request.Request(f"https://{HOST}/cdn-cgi/trace", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            kv = dict(x.split("=", 1) for x in r.read().decode().splitlines() if "=" in x)
        return f"Cloudflare colo={kv.get('colo')} loc={kv.get('loc')}"
    except Exception as e:
        return f"trace lỗi: {e!r}"[:200]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(trace())
    ok = 0
    for i in range(TRIES):
        print(f"[{i + 1}/{TRIES}] {http_rpc('system_chain')}")
        r = ws_rpc("system_chain")
        ok += r.startswith("WS OK")
        print(f"[{i + 1}/{TRIES}] {r}")
        time.sleep(3)
    print(f"KẾT LUẬN: websocket thành công {ok}/{TRIES} lần (ngưỡng 10 s như CLI)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
