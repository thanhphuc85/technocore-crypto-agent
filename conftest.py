"""Chặn MẠNG THẬT trong mọi test.

CI chạy với GITHUB_REPOSITORY của repo chủ -> KV_NS = namespace THẬT của agent. Test nào lỡ
gọi main() mà không stub hết đường mạng sẽ ghi đè KV thật (đã xảy ra: sổ cái rỗng ký bằng
khoá test đè lên `token_ledger` mỗi lần CI chạy -> runner thật báo "bỏ qua sổ KV không có
chữ ký hợp lệ"). Test cần mạng phải tự giả `requests` (monkeypatch) — gọi lọt ra ngoài sẽ
nhận ConnectionError (là RequestException, nên code xử lý như mất mạng bình thường).
"""
import socket

import pytest
import requests

_LOCAL = {"localhost", "127.0.0.1", "::1"}


def _blocked_request(self, method, url, *a, **k):
    raise requests.ConnectionError(f"test không được gọi mạng thật: {method} {url}")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    monkeypatch.setattr(requests.Session, "request", _blocked_request)
    real_gai = socket.getaddrinfo

    def guarded_gai(host, *a, **k):                 # lưới thứ 2: urllib / socket trực tiếp
        if host not in _LOCAL:
            raise OSError(f"test không được phân giải mạng thật: {host}")
        return real_gai(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_gai)
