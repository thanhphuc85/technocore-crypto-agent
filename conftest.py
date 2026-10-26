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


@pytest.fixture(autouse=True)
def _isolated_state_files(monkeypatch, tmp_path):
    """File trạng thái mặc định (state.json, flop_pacer.json) nằm ở thư mục làm việc = repo. Test
    nào lỡ ghi (vd persist sổ cái -> bản sao state.json; metering -> pacer) sẽ để lại file thật
    trong repo. Mặc định trỏ chúng vào thư mục tạm; test tự đặt đường dẫn riêng vẫn ghi đè được."""
    monkeypatch.setenv("FLOP_PACER_FILE", str(tmp_path / "flop_pacer.json"))
    import agent_cron
    monkeypatch.setattr(agent_cron, "STATE_FILE", str(tmp_path / "state.json"))
    # main() đặt mốc ngân sách thời gian của lượt vào biến toàn cục -> không để rò sang test sau.
    monkeypatch.setattr(agent_cron, "_RUN_STARTED", None)
