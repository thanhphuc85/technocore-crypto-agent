"""`.env.example` là thứ fork MỚI copy nguyên xi. Nếu nó đặt giá trị YẾU hơn mặc định
trong code thì mọi fork tự hạ phòng thủ xuống mà không ai biết — đã xảy ra: đổi mặc định
jitter ở PR #145 nhưng quên template, để lại FAUCET_JITTER_MIN=1 (mặc định code 30), tức
claim vẫn rơi đúng một khung giờ mỗi ngày. Test này neo template vào mặc định code."""
import os
import flop_faucet as ff
import flop_pacer as fp
import token_manager as tm

ENV_EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.example")


def _env_example() -> dict:
    """Chỉ lấy dòng KEY=VALUE KHÔNG bị comment — đúng thứ fork nhận khi copy."""
    out = {}
    with open(ENV_EXAMPLE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def test_env_example_jitter_not_weaker_than_code_defaults():
    env = _env_example()
    pace = env.get("FLOP_PACE_JITTER_PCT")
    faucet = env.get("FLOP_FAUCET_JITTER_MIN")
    assert pace is not None and faucet is not None, "template phải đặt sẵn 2 núm jitter"
    # "Yếu hơn" = nhiễu ÍT hơn mặc định -> pattern dễ nhận ra hơn.
    assert int(pace) >= int(fp.DEFAULT_JITTER_PCT), (
        f"FLOP_PACE_JITTER_PCT={pace} yếu hơn mặc định code {fp.DEFAULT_JITTER_PCT}")
    assert float(faucet) >= ff.DEFAULT_JITTER_MIN, (
        f"FLOP_FAUCET_JITTER_MIN={faucet} yếu hơn mặc định code {ff.DEFAULT_JITTER_MIN}")


def test_env_example_organic_only_stays_on(monkeypatch):
    val = _env_example().get("FLOP_ORGANIC_ONLY")
    assert val is not None, "template phải đặt FLOP_ORGANIC_ONLY"
    monkeypatch.setenv("FLOP_ORGANIC_ONLY", val)
    assert tm.organic_only() is True, f"template đặt FLOP_ORGANIC_ONLY={val} -> TẮT mất"
