"""Test cho flop_status_feed — thuần, không mạng."""
from flop_status_feed import build_status_line, collect_metrics


def test_full_line_has_all_parts():
    m = {
        "eth_gas_gwei": 3.2, "btc_dom": 58.1, "eth_dom": 12.4,
        "fear_value": 64, "fear_class": "Greed",
        "kibble_useful_rate": 0.22, "kibble_n": 63, "chat_write_ok": True,
    }
    line = build_status_line(m, agent="a", ts="T")
    assert line is not None
    assert "ETH gas 3.2 gwei" in line
    assert "BTC dom 58.1% ETH 12.4%" in line
    assert "F&G 64 Greed" in line
    assert "kibble useful 22% (n=63)" in line
    assert "chat-write ok" in line
    assert line.endswith("| T")


def test_empty_metrics_returns_none():
    # KHÔNG có mảnh số liệu nào -> KHÔNG đăng (chống boilerplate rỗng bị anti-sybil cờ)
    assert build_status_line({}, agent="a", ts="T") is None


def test_partial_metrics_skips_missing():
    line = build_status_line({"eth_gas_gwei": 5}, agent="a", ts="T")
    assert line == "[a] 📡 status | ETH gas 5 gwei | T"


def test_degraded_write_flag():
    line = build_status_line({"chat_write_ok": False}, agent="a")
    assert "chat-write degraded" in line


def test_max_chars_truncates():
    line = build_status_line({"fear_value": "x" * 500}, agent="a", max_chars=40)
    assert len(line) <= 40


def test_collect_metrics_swallows_fetcher_errors():
    def boom():
        raise RuntimeError("network down")

    m = collect_metrics(
        get_eth_gas=boom,
        get_dominance=lambda: (58.1, 12.4),
        get_fear_greed=lambda: (None, None),
        kibble_summary={"useful_rate": 0.5, "useful": 3, "not": 1},
        chat_write_ok=True,
    )
    assert "eth_gas_gwei" not in m          # fetcher lỗi -> bỏ, không sập
    assert m["btc_dom"] == 58.1 and m["eth_dom"] == 12.4
    assert "fear_value" not in m            # (None, None) -> bỏ
    assert m["kibble_useful_rate"] == 0.5 and m["kibble_n"] == 4
    assert m["chat_write_ok"] is True


def test_collect_metrics_drops_kibble_when_no_decided():
    m = collect_metrics(kibble_summary={"useful_rate": None, "useful": 0, "not": 0})
    assert "kibble_useful_rate" not in m
