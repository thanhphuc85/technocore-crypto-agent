"""Test flop_units — Effective-FLOPs / G_n theo Yellow Paper §4.1–§4.2."""

import pytest

import flop_units as u


# --- Neo hiệu chuẩn Yellow Paper (quote: 8B -> 16, 70B -> 140) ----------------------

def test_pinned_gn_per_token():
    assert u.g_n_per_token(8_000_000_000) == 16
    assert u.g_n_per_token(70_000_000_000) == 140


def test_pinned_via_model_registry():
    # dense per-token = phần Yellow Paper pin
    assert u.g_n_for_model("llama-3-8b", 1, include_attention=False) == 16
    assert u.g_n_for_model("llama-3-70b", 1, include_attention=False) == 140


def test_model_lookup_normalizes_name():
    assert u.model_spec("LLAMA-3-8B") == u.model_spec("llama_3_8b")
    with pytest.raises(KeyError):
        u.model_spec("gpt-nope")


# --- Lõi dense ----------------------------------------------------------------------

def test_dense_flops_and_gn():
    # 2 · 8e9 · 10 = 160e9 -> G_n = 160
    assert u.dense_flops(8_000_000_000, 10) == 160_000_000_000
    assert u.g_n(8_000_000_000, 10, include_attention=False) == 160


def test_zero_tokens_is_zero():
    assert u.dense_flops(8_000_000_000, 0) == 0
    assert u.g_n(8_000_000_000, 0, include_attention=False) == 0


# --- Floor CHỈ MỘT LẦN (R4.4) -------------------------------------------------------

def test_floor_once_at_division():
    # F_eff = 1 tỉ - 1 -> G_n = 0 ; = 1 tỉ -> 1
    assert u.g_n(u.GIGA - 1, 1, include_attention=False) // 2 == 0  # sanity
    # dùng active_params sao cho 2·P·1 nằm ngay dưới/tại ngưỡng tỉ:
    assert u.g_n(499_999_999, 1, include_attention=False) == 0     # 2·P = 999,999,998
    assert u.g_n(500_000_000, 1, include_attention=False) == 1     # 2·P = 1,000,000,000


def test_no_intermediate_rounding():
    # dense + attention cộng ở mức nguyên rồi mới floor một lần
    p, n = 8_000_000_000, 3
    ctx, layers, d = 1024, 32, 4096
    expect = (2 * p * n + 2 * layers * ctx * d) // u.GIGA
    got = u.g_n(p, n, n_layers=layers, n_ctx=ctx, d_attn=d, include_attention=True)
    assert got == expect


# --- Số hạng attention --------------------------------------------------------------

def test_attention_adds_when_context_present():
    dense = u.g_n_for_model("llama-3-8b", 50, include_attention=False)
    full = u.g_n_for_model("llama-3-8b", 50, n_ctx=4096, include_attention=True)
    assert full > dense


def test_attention_zero_when_no_context():
    # n_ctx=0 -> attention không cộng, bằng dense
    dense = u.g_n_for_model("llama-3-8b", 50, include_attention=False)
    same = u.g_n_for_model("llama-3-8b", 50, n_ctx=0, include_attention=True)
    assert dense == same


def test_attention_formula_value():
    assert u.attention_flops(32, 2048, 4096) == 2 * 32 * 2048 * 4096


# --- Kiểm tham số (lỗi caller -> ValueError) ---------------------------------------

def test_invalid_args_raise():
    with pytest.raises(ValueError):
        u.dense_flops(-1, 10)
    with pytest.raises(ValueError):
        u.dense_flops(8_000_000_000, -5)
    with pytest.raises(ValueError):
        u.dense_flops(0, 10)          # active_params phải dương


# --- Tripwire (§4.1 R4.3): reject, không clamp -------------------------------------

def test_tripwire_reject_when_over_ceiling():
    # gn=1000, latency=1ms -> implied = 1000*1000/1 = 1,000,000 GFLOP/s
    out = u.check_tripwire(1000, 1, ceiling_gflops=5000)
    assert out["ok"] is False and out["outcome"] == "reject_tripwire"
    assert out["implied_gflops"] == pytest.approx(1_000_000.0)


def test_tripwire_accept_within_ceiling():
    out = u.check_tripwire(100, 1000, ceiling_gflops=5000)  # implied = 100 GFLOP/s
    assert out["ok"] is True and out["outcome"] == "accept"


def test_tripwire_skips_when_unconfigured(monkeypatch):
    monkeypatch.delenv("FLOP_THROUGHPUT_TRIPWIRE_GFLOPS", raising=False)
    out = u.check_tripwire(1000, 1)
    assert out["ok"] is True and out["outcome"] == "skipped_unconfigured"


def test_tripwire_reads_env_ceiling(monkeypatch):
    monkeypatch.setenv("FLOP_THROUGHPUT_TRIPWIRE_GFLOPS", "5000")
    out = u.check_tripwire(1000, 1)   # implied huge -> reject
    assert out["outcome"] == "reject_tripwire"


def test_tripwire_bad_latency():
    out = u.check_tripwire(100, 0, ceiling_gflops=5000)
    assert out["ok"] is False and out["outcome"] == "reject_bad_latency"
