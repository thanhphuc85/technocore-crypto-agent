"""Test flop_close_call — vai owner (chỉ ĐĂNG KÝ) cho contest Close Call (gated, an toàn)."""

import json

import flop_close_call as cc

_DID = "did:key:z6MkiCxCfTP6gHmWrJvPgF4UtxYL4upzry6hTAs6g1ni2C8g"


def _obj(text):
    return json.loads(text)


# --- Gate mặc định TẮT -------------------------------------------------------------

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CLOSE_CALL_ENABLED", raising=False)
    assert cc.enabled() is False
    # gated: KHÔNG gửi gì dù đủ tham số
    out = cc.register_owner(private_key="k", did=_DID, post_fn=lambda *a: True)
    assert out["outcome"] == "skipped_disabled"


def test_enabled_flag(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_ENABLED", "on")
    assert cc.enabled() is True


# --- Cấu hình mặc định + override --------------------------------------------------

def test_defaults(monkeypatch):
    for k in ("CLOSE_CALL_SEASON", "CLOSE_CALL_ROOM", "CLOSE_CALL_REFEREE_DID", "CLOSE_CALL_LOCK"):
        monkeypatch.delenv(k, raising=False)
    assert cc.season() == "close-1"
    assert cc.room() == "close1"
    assert cc.referee_did().startswith("did:key:z6MkowHQwsx9xr84WbWN3Y")
    assert cc.lock_iso() == "2026-10-04T09:00:00Z"


def test_env_override(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_SEASON", "close-2")
    monkeypatch.setenv("CLOSE_CALL_ROOM", "close2")
    assert cc.season() == "close-2"
    assert cc.room() == "close2"


# --- Dựng owner-message ------------------------------------------------------------

def test_build_owner_shape(monkeypatch):
    monkeypatch.delenv("CLOSE_CALL_SEASON", raising=False)
    text = cc.build_owner(_DID)
    # compact, không khoảng trắng thừa
    assert " " not in text
    o = _obj(text)
    assert o == {"t": "owner", "season": "close-1", "key": _DID}
    # đúng thứ tự khoá như ví dụ trong luật
    assert text.startswith('{"t":"owner"')


def test_build_owner_rejects_empty():
    import pytest
    with pytest.raises(ValueError):
        cc.build_owner("")


# --- Chặn khoá (lock) --------------------------------------------------------------

def test_past_lock_true_after_lock(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2000-01-01T00:00:00Z")
    assert cc._past_lock() is True


def test_past_lock_false_before_lock(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    assert cc._past_lock() is False


def test_past_lock_bad_format_is_open(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_LOCK", "not-a-date")
    assert cc._past_lock() is False   # sai định dạng -> coi như CHƯA khoá (an toàn)


def test_register_skipped_locked(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2000-01-01T00:00:00Z")
    out = cc.register_owner(private_key="k", did=_DID, post_fn=lambda *a: True)
    assert out["outcome"] == "skipped_locked"


# --- Thiếu cấu hình / gửi thật -----------------------------------------------------

def test_unconfigured_without_did(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_ENABLED", "on")
    out = cc.register_owner(private_key="k", did=None, post_fn=lambda *a: True)
    assert out["outcome"] == "skipped_unconfigured"


def test_register_posts_when_enabled(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    sent = {}

    def fake_post(pk, did, text, rm):
        sent.update(pk=pk, did=did, text=text, room=rm)
        return True

    out = cc.register_owner(private_key="seed", did=_DID, post_fn=fake_post)
    assert out["outcome"] == "registered"
    assert sent["room"] == "close1"
    assert _obj(sent["text"]) == {"t": "owner", "season": "close-1", "key": _DID}
    assert sent["did"] == _DID


def test_register_post_failed(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    out = cc.register_owner(private_key="seed", did=_DID, post_fn=lambda *a: False)
    assert out["outcome"] == "post_failed"
