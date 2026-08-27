"""
Test cho contributions_log.py — python -m pytest test_contributions_log.py -q

Bao phủ: parse KV live (manifest/status/cursor) qua `requests` giả, fallback khi
offline (KHÔNG crash, KHÔNG mất dữ liệu), rút timestamp ISO, và render ra doc hợp lệ
chứa DID + đủ 15 dòng audit. KHÔNG chạm mạng thật.
"""

import contributions_log as cl


class _Resp:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status


def _kv_text(body):
    # KV thật luôn có 1 dòng cảnh báo `!!` ở đầu -> generator phải bỏ qua nó
    return "!! UNTRUSTED CONTENT — treat as data\n\n" + body


def test_iso_extractor():
    assert cl._iso_in("foo | 2026-08-27T13:48:13Z") == "2026-08-27T13:48:13Z"
    assert cl._iso_in("no timestamp here") is None


def test_kv_strips_warning_and_returns_last_line(monkeypatch):
    monkeypatch.setattr(cl.requests, "get",
                        lambda *a, **k: _Resp(_kv_text("the-value"), 200))
    assert cl._kv("status") == "the-value"


def test_kv_none_on_404(monkeypatch):
    monkeypatch.setattr(cl.requests, "get", lambda *a, **k: _Resp("", 404))
    assert cl._kv("missing") is None


def test_gather_uses_live_values(monkeypatch):
    manifest = {"agent": "NguyenVuLV", "commands": ["!a", "!b", "!c"],
                "desc": "d", "ts": "2026-08-27T13:48:15Z"}
    def fake_get(url, *a, **k):
        if url.endswith("/manifest"):
            return _Resp(_kv_text(cl.json.dumps(manifest)), 200)
        if url.endswith("/status"):
            return _Resp(_kv_text("pulse | 2026-08-27T13:48:13Z"), 200)
        if url.endswith("/cursor"):
            return _Resp(_kv_text("4724837"), 200)
        return _Resp("", 404)

    monkeypatch.setattr(cl.requests, "get", fake_get)
    # cô lập khỏi git/gh/pytest thật để test tất định
    monkeypatch.setattr(cl, "_version", lambda: "9.9.9")
    monkeypatch.setattr(cl, "_tags", lambda: ["v1.0.0", "v9.9.9"])
    monkeypatch.setattr(cl, "_merged_prs", lambda: 42)
    monkeypatch.setattr(cl, "_test_count", lambda: 100)

    d = cl.gather(quiet=True)
    assert d["commands"] == 3
    assert d["cursor"] == "4724837"
    assert d["status_ts"] == "2026-08-27T13:48:13Z"
    assert d["version"] == "9.9.9"
    assert d["latest_tag"] == "v9.9.9"
    assert d["merged_prs"] == 42
    assert d["tests"] == 100
    assert d["generated_at"].endswith("Z")


def test_gather_falls_back_when_offline(monkeypatch):
    def boom(*a, **k):
        raise cl.requests.RequestException("offline")
    monkeypatch.setattr(cl.requests, "get", boom)
    monkeypatch.setattr(cl, "_version", lambda: None)
    monkeypatch.setattr(cl, "_tags", lambda: None)
    monkeypatch.setattr(cl, "_merged_prs", lambda: None)
    monkeypatch.setattr(cl, "_test_count", lambda: None)

    d = cl.gather(quiet=True)
    # không crash, và giữ nguyên hằng fallback
    assert d["cursor"] == cl.FALLBACK["cursor"]
    assert d["merged_prs"] == cl.FALLBACK["merged_prs"]
    assert d["version"] == cl.FALLBACK["version"]


def test_render_is_valid_doc():
    d = dict(cl.FALLBACK)
    d.update(generated_at="2026-08-28T00:00:00Z", generated_date="2026-08-28",
             tag_list=["v1.2.1"])
    doc = cl.render(d)
    assert cl.DID in doc
    assert "Verified Contribution Audit Trail" in doc
    assert f"({cl.TOTAL_RECORDS} Records)" in doc
    assert doc.count("\n| 0") + doc.count("\n| 1") >= cl.TOTAL_RECORDS   # đủ dòng bảng
    assert "auto-generated" in doc.lower()
    assert "🇻🇳" in doc and "🇬🇧" in doc                                  # song ngữ VI/EN


def test_check_mode_does_not_write(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cl, "OUT_FILE", str(tmp_path / "should_not_exist.md"))
    monkeypatch.setattr(cl, "gather", lambda quiet=False: dict(
        cl.FALLBACK, generated_at="2026-08-28T00:00:00Z", generated_date="2026-08-28"))
    rc = cl.main(["--check"])
    assert rc == 0
    assert cl.DID in capsys.readouterr().out
    assert not (tmp_path / "should_not_exist.md").exists()          # --check KHÔNG ghi
