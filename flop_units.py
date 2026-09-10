"""
flop_units.py — Đơn vị công việc tham chiếu của FLOP: Effective-FLOPs (F_eff) -> G_n.

Theo Yellow Paper (flop.finance/intro/yellowpaper/, §4.1–§4.2): FLOP KHÔNG tính phí theo
số phép tính vật lý mà theo một đơn vị công-việc-tham-chiếu tất định:

    F_eff ≈ 2 · P_active · N  +  2 · n_layer · n_ctx · d_attn      (§4.2, heuristic "bare")
    G_n   =  floor(F_eff / 1e9)                                    (§4.1 R4.1)

  · P_active = số tham số hoạt động của mô hình
  · N        = số token SINH ra
  · n_layer  = số lớp transformer
  · n_ctx    = độ dài ngữ cảnh/prompt
  · d_attn   ≈ hidden dimension (d_model) — số hạng attention theo ngữ cảnh

Neo hiệu chuẩn (quote §4.2): "Llama-3-8B → 16, Llama-3-70B → 140 G_n/token". Khớp đúng
phần dense 2·P/1e9 (2·8e9/1e9 = 16 ; 2·70e9/1e9 = 140) -> "G_n/token" chính là số hạng dense,
còn attention là phần CỘNG THÊM theo n_ctx.

TRUNG THỰC VỀ ĐỘ CHÍNH XÁC (đọc kỹ):
  · Phần DENSE (2·P·N) là neo CỨNG, tái tạo đúng hai giá trị Yellow Paper pin -> tin được.
  · Số hạng ATTENTION là heuristic "bare" của §4.2; công thức chi tiết (d_attn, phân rã
    QKV/FFN) có thể khác ở spec cuối. Bộ đo QUYỀN UY là `hp_poui::flop_meter` (§4.2 R4.2) —
    file NÀY KHÔNG tái hiện nó byte-exact. Vì vậy G_n ở đây là ƯỚC LƯỢNG để TỰ-ĐO và định
    lượng phí khi lập kế hoạch, KHÔNG phải giá trị SETTLEMENT. Khi FLOP mở testnet + công bố
    flop_meter tham chiếu -> thay lõi bằng số của nó (giữ nguyên API).

Quy tắc số học (§4.1 R4.4): dùng SỐ NGUYÊN; floor CHỈ MỘT LẦN ở phép chia; KHÔNG làm tròn
hay clamp ở các bước trung gian. Tái dùng KV-cache KHÔNG được chiết khấu G_n đã chốt (R4.5).

Đây là THƯ VIỆN THUẦN, không có tác dụng phụ, không tự chạy gì ở tầng agent (nên vốn đã
"gated": chưa file nào gọi nó -> hành vi 24/7 không đổi). Chạy thử: python flop_units.py
"""

import os
from collections import namedtuple


# Đơn vị tỉ (10^9) reference-FLOPs — mẫu số của G_n.
GIGA = 1_000_000_000


# --- Sổ đăng ký mô hình (tham số DANH ĐỊNH để tái tạo đúng neo Yellow Paper) ---------
# active_params dùng giá trị DANH ĐỊNH (8e9 / 70e9), không phải số thực (8.03e9 / 70.6e9),
# vì chỉ số danh định mới cho ra đúng 16 / 140 mà spec pin.
ModelSpec = namedtuple("ModelSpec", ["active_params", "n_layers", "d_model"])

MODELS = {
    "llama-3-8b":  ModelSpec(active_params=8_000_000_000,  n_layers=32, d_model=4096),
    "llama-3-70b": ModelSpec(active_params=70_000_000_000, n_layers=80, d_model=8192),
}


def model_spec(name: str) -> ModelSpec:
    """Tra cứu mô hình theo tên (không phân biệt hoa/thường, gạch ngang linh hoạt).
    Ném KeyError nếu chưa đăng ký (đây là lỗi lập trình của caller, không phải sự kiện
    runtime -> để nổ rõ ràng thay vì đoán bừa một cấu hình)."""
    key = str(name).strip().lower().replace("_", "-")
    if key not in MODELS:
        raise KeyError(f"mô hình chưa đăng ký: {name!r} (có: {', '.join(sorted(MODELS))})")
    return MODELS[key]


# --- Kiểm tra tham số (lỗi caller -> ValueError, đúng quy ước flop_session.build_request) -

def _nonneg_int(x, field: str) -> int:
    try:
        v = int(x)
    except (TypeError, ValueError):
        raise ValueError(f"{field} phải là số nguyên, nhận {x!r}")
    if v < 0:
        raise ValueError(f"{field} không được âm, nhận {v}")
    return v


def _pos_int(x, field: str) -> int:
    v = _nonneg_int(x, field)
    if v == 0:
        raise ValueError(f"{field} phải dương, nhận 0")
    return v


# --- Lõi: F_eff và G_n --------------------------------------------------------------

def dense_flops(active_params: int, tokens_generated: int) -> int:
    """Số hạng DENSE 2·P·N (reference-FLOPs). Neo cứng, verify được với giá trị pin."""
    p = _pos_int(active_params, "active_params")
    n = _nonneg_int(tokens_generated, "tokens_generated")
    return 2 * p * n


def attention_flops(n_layers: int, n_ctx: int, d_attn: int) -> int:
    """Số hạng ATTENTION 2·n_layer·n_ctx·d_attn (heuristic "bare" §4.2 — ước lượng, xem
    ghi chú TRUNG THỰC ở đầu file). n_ctx=0 -> 0 (không có ngữ cảnh thì không cộng)."""
    layers = _nonneg_int(n_layers, "n_layers")
    ctx = _nonneg_int(n_ctx, "n_ctx")
    d = _nonneg_int(d_attn, "d_attn")
    return 2 * layers * ctx * d


def f_eff(active_params: int, tokens_generated: int, *, n_layers: int = 0,
          n_ctx: int = 0, d_attn: int = 0, include_attention: bool = True) -> int:
    """Effective-FLOPs = dense + (attention nếu bật). Số nguyên, KHÔNG floor ở đây
    (floor chỉ xảy ra một lần trong g_n())."""
    total = dense_flops(active_params, tokens_generated)
    if include_attention and n_ctx and d_attn:
        total += attention_flops(n_layers, n_ctx, d_attn)
    return total


def g_n(active_params: int, tokens_generated: int, *, n_layers: int = 0,
        n_ctx: int = 0, d_attn: int = 0, include_attention: bool = True) -> int:
    """Đơn vị tính phí G_n = floor(F_eff / 1e9). Floor MỘT LẦN duy nhất, ở đây (R4.4)."""
    return f_eff(active_params, tokens_generated, n_layers=n_layers, n_ctx=n_ctx,
                 d_attn=d_attn, include_attention=include_attention) // GIGA


def g_n_per_token(active_params: int) -> int:
    """G_n/token của phần DENSE = floor(2·P / 1e9). Đây là đại lượng Yellow Paper pin
    (8B -> 16 ; 70B -> 140)."""
    return dense_flops(active_params, 1) // GIGA


def g_n_for_model(name: str, tokens_generated: int, *, n_ctx: int = 0,
                  include_attention: bool = True) -> int:
    """G_n cho một mô hình đã đăng ký, tự lấy n_layers/d_model từ spec."""
    m = model_spec(name)
    return g_n(m.active_params, tokens_generated, n_layers=m.n_layers, n_ctx=n_ctx,
               d_attn=m.d_model, include_attention=include_attention)


# --- Tripwire an toàn (§4.1 R4.3): reject, KHÔNG clamp -------------------------------

def tripwire_ceiling_gflops():
    """Trần throughput (GFLOP/s) để chặn claim vô lý. Nạp LIVE từ
    FLOP_THROUGHPUT_TRIPWIRE_GFLOPS. Rỗng/không dương -> None (chưa cấu hình -> không chặn)."""
    raw = os.environ.get("FLOP_THROUGHPUT_TRIPWIRE_GFLOPS", "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v > 0 else None


def check_tripwire(gn: int, latency_ms, ceiling_gflops=None) -> dict:
    """Kiểm claim: throughput ngầm = gn·1000/latency_ms (GFLOP/s). Nếu vượt trần -> REJECT
    (fail-closed, không bao giờ clamp/rewrite — R4.3). Trả outcome rõ ràng, KHÔNG ném lỗi
    runtime (đúng tinh thần repo).
      ceiling None + env chưa đặt -> outcome 'skipped_unconfigured' (không chặn).
      latency_ms <= 0 -> 'reject_bad_latency' (không chia 0)."""
    gn = _nonneg_int(gn, "gn")
    ceiling = ceiling_gflops if ceiling_gflops is not None else tripwire_ceiling_gflops()
    try:
        lat = float(latency_ms)
    except (TypeError, ValueError):
        lat = 0.0
    if lat <= 0:
        return {"ok": False, "outcome": "reject_bad_latency", "implied_gflops": None,
                "ceiling_gflops": ceiling}
    implied = gn * 1000.0 / lat
    if ceiling is None:
        return {"ok": True, "outcome": "skipped_unconfigured", "implied_gflops": implied,
                "ceiling_gflops": None}
    if implied > ceiling:
        return {"ok": False, "outcome": "reject_tripwire", "implied_gflops": implied,
                "ceiling_gflops": ceiling}
    return {"ok": True, "outcome": "accept", "implied_gflops": implied,
            "ceiling_gflops": ceiling}


if __name__ == "__main__":
    print("flop_units.py — Effective-FLOPs / G_n (ước lượng tự-đo, không phải settlement)\n")

    for name in ("llama-3-8b", "llama-3-70b"):
        per_tok = g_n_per_token(model_spec(name).active_params)
        print(f"{name:12s} dense G_n/token = {per_tok}  (Yellow Paper pin: "
              f"{'16' if name.endswith('8b') else '140'})")

    print()
    # Ví dụ 1 phiên: sinh 200 token với ngữ cảnh 8192 trên Llama-3-8B.
    tokens, ctx = 200, 8192
    gn_dense = g_n_for_model("llama-3-8b", tokens, include_attention=False)
    gn_full = g_n_for_model("llama-3-8b", tokens, n_ctx=ctx, include_attention=True)
    print(f"phiên: llama-3-8b · {tokens} token · n_ctx={ctx}")
    print(f"  G_n (chỉ dense)      = {gn_dense}")
    print(f"  G_n (dense+attention)= {gn_full}")

    print()
    # Tripwire minh họa (trần 5000 GFLOP/s, latency 100 ms).
    print("tripwire (ceiling=5000 GFLOP/s, latency=100ms):",
          check_tripwire(gn_full, 100, ceiling_gflops=5000)["outcome"])
    print("tripwire (chưa cấu hình):",
          check_tripwire(gn_full, 100)["outcome"])
