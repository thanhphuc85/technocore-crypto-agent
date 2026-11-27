"""agent_cron._explorer_get: thử lại 5xx / lỗi mạng của explorer, không thử lại 4xx."""
import pytest
import requests

import agent_cron as ac


class R:
    def __init__(self, code):
        self.status_code = code


def _seq(monkeypatch, items):
    calls = []
    it = iter(items)

    def get(url, **kw):
        calls.append((url, kw))
        x = next(it)
        if isinstance(x, Exception):
            raise x
        return R(x)
    monkeypatch.setattr(ac.requests, "get", get)
    return calls


def test_retries_503_then_succeeds(monkeypatch):
    calls = _seq(monkeypatch, [503, 503, 200])
    assert ac._explorer_get("https://x/api/a", params={"limit": "5"}).status_code == 200
    assert len(calls) == 3 and calls[0][1]["params"] == {"limit": "5"} and calls[0][1]["timeout"] == 15
    assert calls[0][1]["headers"]["User-Agent"] == ac.UA


def test_404_and_200_are_not_retried(monkeypatch):
    calls = _seq(monkeypatch, [404])
    assert ac._explorer_get("https://x/api/a").status_code == 404 and len(calls) == 1


def test_persistent_5xx_returns_last_response(monkeypatch):
    calls = _seq(monkeypatch, [503] * ac.EXPLORER_TRIES)
    assert ac._explorer_get("https://x/api/a").status_code == 503 and len(calls) == ac.EXPLORER_TRIES


def test_network_errors_are_retried_then_raised(monkeypatch):
    _seq(monkeypatch, [requests.ConnectionError("down"), 200])
    assert ac._explorer_get("https://x/api/a").status_code == 200
    _seq(monkeypatch, [requests.Timeout("slow")] * ac.EXPLORER_TRIES)
    with pytest.raises(requests.Timeout):
        ac._explorer_get("https://x/api/a")
