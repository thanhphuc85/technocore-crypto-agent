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


# ============================ GIAO DỊCH (trading) ============================
from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

_MAKER = "did:key:z6MkwEUPPPoNdxn9XYaAvmEwVjUniHQsVjPXpMKNXJwFff51"


def _fake_sign(monkeypatch):
    monkeypatch.setattr(cc, "sign_message", lambda pk, msg: "SIG(" + msg[-16:] + ")")


# --- gates -----------------------------------------------------------------

def test_trade_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CLOSE_CALL_TRADE_ENABLED", raising=False)
    assert cc.trade_enabled() is False


def test_dry_run_on_by_default(monkeypatch):
    monkeypatch.delenv("CLOSE_CALL_TRADE_DRY_RUN", raising=False)
    assert cc.trade_dry_run() is True
    monkeypatch.setenv("CLOSE_CALL_TRADE_DRY_RUN", "off")
    assert cc.trade_dry_run() is False


def test_max_qty_default_and_override(monkeypatch):
    monkeypatch.delenv("CLOSE_CALL_MAX_QTY", raising=False)
    assert cc.max_qty() == Decimal("5")
    monkeypatch.setenv("CLOSE_CALL_MAX_QTY", "2.5")
    assert cc.max_qty() == Decimal("2.5")
    monkeypatch.setenv("CLOSE_CALL_MAX_QTY", "junk")
    assert cc.max_qty() == Decimal("5")


# --- số / terms ------------------------------------------------------------

def test_dec2_ok_and_step():
    assert cc._dec2("224.5") == "224.50"
    assert cc._dec2(3) == "3.00"
    with pytest.raises(ValueError):
        cc._dec2("1.234")     # quá 2 chữ số
    with pytest.raises(ValueError):
        cc._dec2("abc")


def test_canonical_terms_sorted():
    t = {"until": 10, "side": "buy", "id": "x", "maker": _MAKER, "px": "1.00", "qty": "1.00", "taker": "any"}
    s = cc.canonical_terms(t)
    assert s.startswith('{"id":"x","maker":')
    assert s.index('"px"') < s.index('"qty"') < s.index('"side"') < s.index('"taker"') < s.index('"until"')
    assert " " not in s


def test_build_terms_valid():
    t = cc.build_terms(_MAKER, "sell", "2", "225.25", 479, tid="abc")
    assert t == {"id": "abc", "maker": _MAKER, "px": "225.25", "qty": "2.00",
                 "side": "sell", "taker": "any", "until": 479}


@pytest.mark.parametrize("kw", [
    {"side": "hold"},                       # side sai
    {"qty": "0.05"},                        # dưới min 0.1
    {"px": "1.234"},                        # sai bước
    {"until": 999999},                      # vượt lock
    {"until": "10"},                        # until không phải int
    {"taker": "bob"},                       # taker không hợp lệ
])
def test_build_terms_rejects(kw):
    base = dict(maker=_MAKER, side="buy", qty="1", px="224.50", until=100)
    base.update(kw)
    with pytest.raises(ValueError):
        cc.build_terms(**base)


def test_sign_strings(monkeypatch):
    monkeypatch.delenv("CLOSE_CALL_SEASON", raising=False)
    assert cc.terms_sign_str("T").startswith("close-1|terms|")
    assert cc.accept_sign_str("T", "did:key:z6MkX") == "close-1|accept|T|did:key:z6MkX"


def test_within_limits():
    assert cc.within_limits("224.00", "213.00", "236.00") is True
    assert cc.within_limits("240.00", "213.00", "236.00") is False
    assert cc.within_limits("bad", "1", "2") is False


# --- parse_order -----------------------------------------------------------

def test_parse_order_market_and_relative():
    od = cc.parse_order("buy,1,market,+80", ref_px="224.50", n_next=100)
    assert od == {"side": "buy", "qty": "1", "px": "224.50", "until": 180}


def test_parse_order_absolute():
    od = cc.parse_order("sell,3.0,225.00,479", ref_px=None, n_next=None)
    assert od["side"] == "sell" and od["px"] == "225.00" and od["until"] == 479


def test_parse_order_until_capped():
    od = cc.parse_order("buy,1,market,999999", ref_px="1.00")
    assert od["until"] == cc.LOCK_SWEEP


def test_parse_order_bad_arity():
    with pytest.raises(ValueError):
        cc.parse_order("buy,1,market")


def test_parse_order_market_needs_ref():
    with pytest.raises(ValueError):
        cc.parse_order("buy,1,market,100", ref_px=None)


# --- parse_offer -----------------------------------------------------------

def test_parse_offer_valid(monkeypatch):
    monkeypatch.delenv("CLOSE_CALL_SEASON", raising=False)
    terms = {"id": "z1", "maker": _MAKER, "px": "224.00", "qty": "1.00", "side": "sell", "taker": "any", "until": 90}
    text = cc.build_offer_msg(terms, "MSIG")
    off = cc.parse_offer(text)
    assert off["terms"]["id"] == "z1" and off["maker_sig"] == "MSIG"


def test_parse_offer_rejects():
    assert cc.parse_offer("not json") is None
    assert cc.parse_offer('{"t":"trade"}') is None
    assert cc.parse_offer('{"t":"offer","season":"WRONG","terms":{},"maker_sig":"x"}') is None


# --- post_offer ------------------------------------------------------------

def test_post_offer_disabled(monkeypatch):
    monkeypatch.delenv("CLOSE_CALL_TRADE_ENABLED", raising=False)
    out = cc.post_offer("k", _MAKER, post_fn=lambda *a: True)
    assert out["outcome"] == "skipped_disabled"


def test_post_offer_no_order(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_TRADE_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    monkeypatch.delenv("CLOSE_CALL_ORDER", raising=False)
    out = cc.post_offer("k", _MAKER, post_fn=lambda *a: True)
    assert out["outcome"] == "skipped_no_order"


def test_post_offer_dry_run(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_TRADE_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    monkeypatch.setenv("CLOSE_CALL_ORDER", "buy,1,market,+50")
    monkeypatch.delenv("CLOSE_CALL_TRADE_DRY_RUN", raising=False)   # dry-run mặc định
    _fake_sign(monkeypatch)
    monkeypatch.setattr(cc, "read_reference", lambda: {"ok": True, "ref_px": "224.50",
                                                       "low": "213.00", "high": "236.00", "n_next": 100})
    posted = []
    out = cc.post_offer("k", _MAKER, post_fn=lambda *a: posted.append(a) or True)
    assert out["outcome"] == "dry_run"
    assert posted == []                       # KHÔNG gửi khi dry-run
    assert '"t":"offer"' in out["message"]


def test_post_offer_out_of_limits(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_TRADE_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    monkeypatch.setenv("CLOSE_CALL_ORDER", "buy,1,300.00,+50")
    _fake_sign(monkeypatch)
    monkeypatch.setattr(cc, "read_reference", lambda: {"ok": True, "ref_px": "224.50",
                                                       "low": "213.00", "high": "236.00", "n_next": 100})
    out = cc.post_offer("k", _MAKER, post_fn=lambda *a: True)
    assert out["outcome"] == "out_of_limits"


def test_post_offer_qty_over_max(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_TRADE_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    monkeypatch.setenv("CLOSE_CALL_ORDER", "buy,99,market,+50")
    monkeypatch.setenv("CLOSE_CALL_MAX_QTY", "5")
    monkeypatch.setattr(cc, "read_reference", lambda: {"ok": True, "ref_px": "224.50",
                                                       "low": "213.00", "high": "236.00", "n_next": 100})
    out = cc.post_offer("k", _MAKER, post_fn=lambda *a: True)
    assert out["outcome"] == "invalid_order"


def test_post_offer_posts_live(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_TRADE_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    monkeypatch.setenv("CLOSE_CALL_ORDER", "sell,2,market,+50")
    monkeypatch.setenv("CLOSE_CALL_TRADE_DRY_RUN", "off")
    _fake_sign(monkeypatch)
    monkeypatch.setattr(cc, "read_reference", lambda: {"ok": True, "ref_px": "224.50",
                                                       "low": "213.00", "high": "236.00", "n_next": 100})
    sent = {}
    out = cc.post_offer("k", _MAKER, post_fn=lambda pk, did, text, rm: sent.update(text=text, room=rm) or True)
    assert out["outcome"] == "posted"
    assert sent["room"] == "close1" and '"t":"offer"' in sent["text"]


# --- take_offer ------------------------------------------------------------

def _offer_room(terms, maker_sig="MSIG"):
    return {"messages": [{"text": cc.build_offer_msg(terms, maker_sig)}]}


def test_take_offer_no_id(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_TRADE_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    monkeypatch.delenv("CLOSE_CALL_ACCEPT", raising=False)
    out = cc.take_offer("k", "did:key:z6MkMe", post_fn=lambda *a: True, fetch_fn=lambda **kw: {"messages": []})
    assert out["outcome"] == "skipped_no_id"


def test_take_offer_not_found(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_TRADE_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    out = cc.take_offer("k", "did:key:z6MkMe", offer_id="nope",
                        post_fn=lambda *a: True, fetch_fn=lambda **kw: {"messages": []})
    assert out["outcome"] == "not_found"


def test_take_offer_self(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_TRADE_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    terms = cc.build_terms(_MAKER, "buy", "1", "224.50", 200, tid="mine")
    out = cc.take_offer("k", _MAKER, offer_id="mine",
                        post_fn=lambda *a: True, fetch_fn=lambda **kw: _offer_room(terms))
    assert out["outcome"] == "self_offer"


def test_take_offer_dry_run(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_TRADE_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    monkeypatch.delenv("CLOSE_CALL_TRADE_DRY_RUN", raising=False)
    _fake_sign(monkeypatch)
    monkeypatch.setattr(cc, "read_reference", lambda: {"ok": True, "ref_px": "224.50",
                                                       "low": "213.00", "high": "236.00", "n_next": 100})
    terms = cc.build_terms(_MAKER, "sell", "1", "224.50", 200, tid="t1")
    posted = []
    out = cc.take_offer("k", "did:key:z6MkTaker", offer_id="t1",
                        post_fn=lambda *a: posted.append(a) or True, fetch_fn=lambda **kw: _offer_room(terms))
    assert out["outcome"] == "dry_run" and posted == []
    assert '"t":"trade"' in out["message"] and '"taker":"did:key:z6MkTaker"' in out["message"]


def test_take_offer_posts_live(monkeypatch):
    monkeypatch.setenv("CLOSE_CALL_TRADE_ENABLED", "on")
    monkeypatch.setenv("CLOSE_CALL_LOCK", "2099-01-01T00:00:00Z")
    monkeypatch.setenv("CLOSE_CALL_TRADE_DRY_RUN", "off")
    _fake_sign(monkeypatch)
    monkeypatch.setattr(cc, "read_reference", lambda: {"ok": True, "ref_px": "224.50",
                                                       "low": "213.00", "high": "236.00", "n_next": 100})
    terms = cc.build_terms(_MAKER, "sell", "1", "224.50", 200, tid="t2")
    sent = {}
    out = cc.take_offer("k", "did:key:z6MkTaker", offer_id="t2",
                        post_fn=lambda pk, did, text, rm: sent.update(text=text, room=rm) or True,
                        fetch_fn=lambda **kw: _offer_room(terms))
    assert out["outcome"] == "posted"
    assert '"t":"trade"' in sent["text"] and sent["room"] == "close1"
