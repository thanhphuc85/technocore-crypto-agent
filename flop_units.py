"""
flop_units.py — "bare heuristic" F_eff/G_n của FLOP: CHỈ để SANITY-CHECK, KHÔNG phải giá billed.

Đối chiếu nguồn CHÍNH THỨC (github.com/flop-labs/yellowpaper, yellowpaper.md §4.1–§4.2):

    F_eff ≈ 2 · P_active · N  +  2 · n_layer · n_ctx · d_attn      (§4.2, dòng 714)
    G_n   =  floor(F_eff / 1e9)                                    (§4.1 R4.1)
    per-token/per-layer: QKV 6d², attn output 2d², FFN 16d²        (§4.2, dòng 717)

  · P_active = tham số HOẠT ĐỘNG (MoE-aware) · N = token SINH ra · n_layer = số lớp
  · n_ctx = độ dài ngữ cảnh · d_attn ≈ hidden dim (d_model)

⚠️ VAI TRÒ ĐÚNG (đọc kỹ — đây là chỗ dễ hiểu sai):
  Yellow Paper nói THẲNG công thức trên là "a **sanity check only** (R4.3), never a billed
  amount" (dòng 711). R4.2: G_n tính phí BẮT BUỘC qua primitive tất định `hp_poui::flop_meter`
  (mirror Python: `tee-bridge/inference/attestor/flop_meter.py`), *"not derived from bare 2·P·N"*.
  Bare 2·P·N sai ~2–3× (bỏ quadratic attention, embedding/unembed, LayerNorm/softmax).

  ⇒ File này CHỈ dùng để: (a) SANITY-CHECK tripwire (R4.3, `check_tripwire`), và (b) ước lượng
     thô công suất khi lập kế hoạch. TUYỆT ĐỐI KHÔNG dùng làm G_n tính phí/settlement, và KHÔNG
     nối vào `meter_inference` như con số billed — làm vậy là VI PHẠM R4.2. Khi FLOP công bố
     `flop_meter` tham chiếu -> nạp nó làm bộ đo thật (giữ nguyên API ở đây cho tripwire).

Neo KAT (§4.2 dòng 707/720 — pin theo op-count model ĐẦY ĐỦ, không phải bare heuristic):
  Llama-3-8B → 16 · DeepSeek-V3 → 74 · Llama-3-70B → 140 G_n/token. Bare heuristic TRÙNG các
  giá trị này vì ở 1 token phần dense (2·P) áp đảo (DeepSeek-V3 MoE: 37B active -> 2·37e9/1e9 = 74).

Quy tắc số học (§4.1 R4.4): SỐ NGUYÊN; floor CHỈ MỘT LẦN ở phép chia; không làm tròn/clamp
trung gian. KV-cache KHÔNG chiết khấu G_n (R4.5).

THƯ VIỆN THUẦN, không tác dụng phụ, chưa file nào gọi -> agent 24/7 không đổi. python flop_units.py
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
    # DeepSeek-V3: MoE 671B tổng / 37B ACTIVE per token -> neo KAT 74 (= 2·37e9/1e9).
    "deepseek-v3": ModelSpec(active_params=37_000_000_000, n_layers=61, d_model=7168),
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
    """G_n (bare heuristic) = floor(F_eff / 1e9). Floor MỘT LẦN duy nhất (R4.4). ĐÂY LÀ
    SANITY-CHECK, KHÔNG phải G_n tính phí — billed G_n đi qua hp_poui::flop_meter (R4.2)."""
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
    print("flop_units.py — bare heuristic F_eff/G_n (SANITY-CHECK R4.3, KHÔNG phải billed)\n")

    pins = {"llama-3-8b": 16, "deepseek-v3": 74, "llama-3-70b": 140}
    for name, pin in pins.items():
        per_tok = g_n_per_token(model_spec(name).active_params)
        print(f"{name:12s} dense G_n/token = {per_tok}  (KAT pin: {pin})")

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
