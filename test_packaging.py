"""Bản cài từ PyPI phải có MỌI module agent import lúc chạy."""
import glob
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def _py_modules():
    with open(os.path.join(HERE, "pyproject.toml"), encoding="utf-8") as f:
        text = f.read()
    block = re.search(r"py-modules\s*=\s*\[(.*?)\]", text, re.S).group(1)
    return set(re.findall(r'"([A-Za-z0-9_]+)"', block))


def test_every_flop_module_is_packaged():
    shipped = _py_modules()
    needed = {"agent_cron", "token_manager"} | {
        os.path.basename(p)[:-3] for p in glob.glob(os.path.join(HERE, "flop_*.py"))}
    assert needed - shipped == set(), f"thiếu trong py-modules: {sorted(needed - shipped)}"
    assert shipped - needed == set(), f"py-modules trỏ tới file không tồn tại: {sorted(shipped - needed)}"
