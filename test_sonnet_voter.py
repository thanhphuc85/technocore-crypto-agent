"""Test sonnet_voter — vai voter cho sonnet-1 (gated, không tự đoán phiếu)."""

import json

import pytest

import sonnet_voter as sv


def _obj(text):
    return json.loads(text)


# --- Gate mặc định TẮT -------------------------------------------------------------

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SONNET_VOTER_ENABLED", raising=False)
    assert sv.voter_enabled() is False
    # gated: không gửi gì dù có đủ tham số
    out = sv.register_voter(private_key="k", did="did:key:z6MkX", post_fn=lambda *a: True)
    assert out["outcome"] == "skipped_disabled"


def test_enabled_flag(monkeypatch):
    monkeypatch.setenv("SONNET_VOTER_ENABLED", "true")
    assert sv.voter_enabled() is True


# --- Dựng message ------------------------------------------------------------------

def test_build_register_is_voter_without_x(monkeypatch):
    monkeypatch.delenv("SONNET_CONTEST_ID", raising=False)
    o = _obj(sv.build_register("register-1"))
    assert o["type"] == "sonnet.register.v1"
    assert o["role"] == "voter"
    assert o["contest_id"] == "sonnet-2"     # sonnet-1 bị bỏ; contest thật là sonnet-2
    assert "x_account_url" not in o          # voter KHÔNG kèm X
    assert o["request_id"] == "register-1"


def test_default_contest_is_sonnet_2(monkeypatch):
    monkeypatch.delenv("SONNET_CONTEST_ID", raising=False)
    assert sv.contest_id() == "sonnet-2"
    assert sv.reg_room() == "mb-sonnet-2-registration"
    assert sv.votes_room() == "mb-sonnet-2-votes"
    assert sv.submissions_room() == "mb-sonnet-2-submissions"


def test_rooms_follow_contest_id(monkeypatch):
    monkeypatch.setenv("SONNET_CONTEST_ID", "sonnet-3")
    assert sv.reg_room() == "mb-sonnet-3-registration"
    assert sv.votes_room() == "mb-sonnet-3-votes"


def test_referee_did_pinned(monkeypatch):
    monkeypatch.delenv("SONNET_REFEREE_DID", raising=False)
    assert sv.referee_did() == "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
    monkeypatch.setenv("SONNET_REFEREE_DID", "did:key:z6MkOTHER")
    assert sv.referee_did() == "did:key:z6MkOTHER"


def test_build_ballot(monkeypatch):
    did = "did:key:z6MkiCxCfTP6gHmWrJvPgF4UtxYL4upzry6hTAs6g1ni2C8g"
    o = _obj(sv.build_ballot(did, "entry-7", "ballot-1"))
    assert o["type"] == "sonnet.ballot.v1"
    assert o["voter_did"] == did and o["entry_id"] == "entry-7"
    assert o["request_id"] == "ballot-1"


def test_ballot_rejects_empty():
    with pytest.raises(ValueError):
        sv.build_ballot("did:key:z6MkX", "")        # entry rỗng
    with pytest.raises(ValueError):
        sv.build_ballot("", "entry-1")              # did rỗng


def test_compact_single_line():
    text = sv.build_register("r-1")
    assert "\n" not in text and ", " not in text and ": " not in text


def test_request_id_unique():
    a = sv.build_register()
    b = sv.build_register()
    assert _obj(a)["request_id"] != _obj(b)["request_id"]


def test_contest_id_override(monkeypatch):
    monkeypatch.setenv("SONNET_CONTEST_ID", "sonnet-2")
    assert _obj(sv.build_register())["contest_id"] == "sonnet-2"


# --- pre-start evidence ------------------------------------------------------------

def test_prestart_evidence_contains_did():
    did = "did:key:z6MkiCxCfTP6gHmWrJvPgF4UtxYL4upzry6hTAs6g1ni2C8g"
    ev = sv.build_prestart_evidence(did)
    assert did in ev and "2026-09-11T12:00:00Z" in ev
    with pytest.raises(ValueError):
        sv.build_prestart_evidence("")


# --- parse_submissions -------------------------------------------------------------

def test_parse_submissions_filters(monkeypatch):
    monkeypatch.delenv("SONNET_CONTEST_ID", raising=False)
    msgs = [
        {"from": "did:a", "seq": 1, "ts": "t", "text": json.dumps(
            {"type": "sonnet.submit.v1", "contest_id": "sonnet-2", "game_id": "a",
             "poem_room": "d-sonnet-2-team-a", "poem_sha256": "hash", "x_post_ids": ["1"]})},
        {"from": "did:b", "seq": 2, "ts": "t", "text": "not json"},
        {"from": "did:c", "seq": 3, "ts": "t", "text": json.dumps(
            {"type": "sonnet.word.v1", "contest_id": "sonnet-2"})},          # sai type
        {"from": "did:d", "seq": 4, "ts": "t", "text": json.dumps(
            {"type": "sonnet.submit.v1", "contest_id": "sonnet-1"})},        # sai contest (đã bỏ)
    ]
    got = sv.parse_submissions(msgs)
    assert len(got) == 1 and got[0]["game_id"] == "a" and got[0]["poem_sha256"] == "hash"


# --- choose_entry: KHÔNG tự đoán ---------------------------------------------------

def test_choose_prefers_explicit(monkeypatch):
    monkeypatch.delenv("SONNET_BALLOT_ENTRY", raising=False)
    entries = [{"entry_id": "e1"}, {"entry_id": "e2"}]
    assert sv.choose_entry(entries, prefer="e2") == "e2"


def test_choose_prefer_from_env(monkeypatch):
    monkeypatch.setenv("SONNET_BALLOT_ENTRY", "e1")
    assert sv.choose_entry([{"entry_id": "e1"}, {"entry_id": "e2"}]) == "e1"


def test_choose_none_when_no_signal(monkeypatch):
    monkeypatch.delenv("SONNET_BALLOT_ENTRY", raising=False)
    # prefer sai + không evaluator -> None (không bỏ bừa)
    assert sv.choose_entry([{"entry_id": "e1"}], prefer="nope") is None
    assert sv.choose_entry([{"entry_id": "e1"}]) is None


def test_choose_uses_injected_evaluator(monkeypatch):
    monkeypatch.delenv("SONNET_BALLOT_ENTRY", raising=False)
    entries = [{"entry_id": "e1"}, {"entry_id": "e2"}]
    pick = sv.choose_entry(entries, evaluator=lambda es: "e2")
    assert pick == "e2"


# --- cast_ballot: no_choice khi chưa chỉ định --------------------------------------

def test_cast_ballot_no_choice(monkeypatch):
    monkeypatch.setenv("SONNET_VOTER_ENABLED", "true")
    monkeypatch.delenv("SONNET_BALLOT_ENTRY", raising=False)
    out = sv.cast_ballot(private_key="k", did="did:key:z6MkX", post_fn=lambda *a: True, entries=[])
    assert out["outcome"] == "no_choice"


def test_cast_ballot_votes_when_chosen(monkeypatch):
    monkeypatch.setenv("SONNET_VOTER_ENABLED", "true")
    sent = {}
    def fake_post(pk, did, text, room):
        sent["room"] = room
        sent["text"] = text
        return True
    out = sv.cast_ballot(private_key="k", did="did:key:z6MkX", entry_id="e9",
                         post_fn=fake_post)
    assert out["outcome"] == "voted" and out["entry_id"] == "e9"
    assert sent["room"] == sv.votes_room()
    assert _obj(sent["text"])["entry_id"] == "e9"


def test_register_unconfigured_when_missing_key(monkeypatch):
    monkeypatch.setenv("SONNET_VOTER_ENABLED", "true")
    out = sv.register_voter(private_key=None, did=None, post_fn=lambda *a: True)
    assert out["outcome"] == "skipped_unconfigured"
