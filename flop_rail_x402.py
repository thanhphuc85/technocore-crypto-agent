# SPDX-License-Identifier: MIT
"""flop_rail_x402 — rail HTLC CÓ-GIÁ-TRỊ cho tclk/1 (Phương án A), an toàn & default-OFF.

tclk/1 điều phối deal HTLC bằng tin nhắn ký; TIỀN nằm trên "rail". Rail `paper`
(trong flop_tclk.py) chỉ là escrow DANH NGHĨA (asset PAPER, không giá trị). Module này
là rail giá-trị-thật ĐẦU TIÊN: escrow là một **HTLC contract on-chain** (EVM testnet),
được **fund bằng USDC qua x402 / Circle Gateway**. x402 chỉ là ỐNG DẪN TIỀN; điều kiện
(hash-lock + time-lock) nằm ở contract.

Bốn thao tác rail mà state machine tclk cần (xem flop_tclk.run_tclk_complete):
  • lock(...)         — PAYER: mở escrow, fund USDC, hash-lock=statement, time-lock=refundAfterMs
  • verify_lock(...)  — PAYEE (CỔNG): escrow có thật, đã fund, đúng hash/time/payee, chưa rút?
  • claim(...)        — PAYEE: nộp preimage lên contract -> USDC về địa chỉ payee
  • refund(...)       — PAYER: sau time-lock, đòi lại USDC

TƯƠNG THÍCH HASH: tclk statement = sha256(preimage) (xem flop_tclk._sha256_hex). Vậy HTLC
contract PHẢI dùng **sha256(preimage) == hashlock** (KHÔNG phải keccak256 như nhiều HTLC mẫu).

AN TOÀN (đây là TIỀN THẬT — đọc kỹ):
  1) CONTRACT PHẢI TRẢ VỀ ĐÚNG PAYEE cố định đặt lúc lock (KHÔNG phải msg.sender). Vì tclk
     công bố preimage trong room CÔNG KHAI -> nếu contract trả cho ai nộp preimage thì bị
     front-run rút mất. verify_lock() kiểm state["payee"] == địa chỉ của MÌNH.
  2) CLAIM ON-CHAIN TRƯỚC, POST reveal frame SAU (run_tclk_complete làm đúng thứ tự này).
  3) Phải claim trước refundAfterMs TRỪ margin xác nhận block (claimable()); trễ -> payer
     refund, làm không công.
  4) Key EVM (secp256k1) RIÊNG, KHÔNG phải seed Ed25519 của agent (giống evm_submit_tx).
  5) Mặc định OFF + dry-run; chưa cấu hình -> skipped_unconfigured, KHÔNG BAO GIỜ bịa tx.

Seam tiêm vào (không thêm dependency cứng — như flop_tx/flop_faucet):
  • read_fn(contract_addr, "getContract", contract_id) -> dict|None   (eth_call đã decode)
  • submit_fn({"to","fn","args"}) -> tx_hash str                       (ký secp256k1 + gửi)
Logic thuần (config/normalize/claimable/verify_lock nhận state dict) có test, không mạng.
"""
from __future__ import annotations

import hashlib
import os
import re

RAIL = "x402"

_HEX32_RE = re.compile(r"^0x[0-9a-f]{64}$")          # 32 byte: preimage & sha256 statement
_ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")            # địa chỉ EVM


# ── Env / config (thuần) ─────────────────────────────────────────────────────
def x402_enabled() -> bool:
    """Rail chỉ hoạt động khi bật TƯỜNG MINH (mặc định OFF)."""
    return os.environ.get("FLOP_TCLK_X402_ENABLED", "").strip().lower() in ("1", "true", "on", "yes")


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except (ValueError, TypeError):
        return default


def load_config() -> dict:
    """Đọc cấu hình rail từ env. Mọi giá trị BEST-EFFORT; thiếu -> configured()=False sau này."""
    return {
        "rpc_url": os.environ.get("X402_RPC_URL", "").strip(),
        "htlc_addr": _norm_addr(os.environ.get("X402_HTLC_CONTRACT", "")),
        "payee_addr": _norm_addr(os.environ.get("X402_PAYEE_ADDRESS", "")),  # địa chỉ MÌNH nhận
        # margin: phải claim xong TRƯỚC refundAfterMs - margin để tx kịp confirm (mặc định 3').
        "claim_margin_ms": _env_int("X402_CLAIM_MARGIN_MS", 3 * 60 * 1000),
        # trần an toàn: không nhận deal lớn hơn (đơn vị nhỏ nhất của asset, vd 6-dp USDC).
        "max_amount": _env_int("X402_MAX_AMOUNT", 0),  # 0 = không cấu hình (chưa cho live số lớn)
    }


def _norm_addr(a) -> str:
    a = (str(a or "")).strip().lower()
    return a if _ADDR_RE.match(a) else ""


# ── Chuẩn hoá & guard thuần ──────────────────────────────────────────────────
def normalize_statement(statement) -> str | None:
    """statement hợp lệ = '0x' + 64 hex (sha256). Chuẩn hoá về lowercase; None nếu sai."""
    s = (str(statement or "")).strip().lower()
    return s if _HEX32_RE.match(s) else None


def preimage_matches(statement, preimage_hex) -> bool:
    """True nếu sha256(preimage) == statement (fail-closed) — CHÍNH XÁC như flop_tclk."""
    s = normalize_statement(statement)
    if not s:
        return False
    p = (str(preimage_hex or "")).strip().lower()
    if not _HEX32_RE.match(p):
        return False
    try:
        return "0x" + hashlib.sha256(bytes.fromhex(p[2:])).hexdigest() == s
    except Exception:
        return False


def claimable(now_ms: int, refund_after_ms: int, margin_ms: int) -> bool:
    """Còn kịp claim on-chain? Phải xong TRƯỚC refundAfterMs - margin (để tx confirm kịp)."""
    try:
        return int(now_ms) + int(margin_ms) < int(refund_after_ms)
    except (ValueError, TypeError):
        return False


def verify_lock_state(state, statement, refund_after_ms, my_payee_addr) -> bool:
    """CỔNG PAYEE (thuần, nhận state đã đọc từ chain). True CHỈ KHI escrow:
      - tồn tại & đã fund, chưa rút/refund,
      - hashlock KHỚP statement của mình (sha256),
      - timelock KHỚP refundAfterMs của mình (chống payer đặt refund sớm hơn thoả thuận),
      - payee cố định == địa chỉ MÌNH (chống front-run rút preimage công khai).
    Fail-closed: thiếu field nào -> False."""
    s = normalize_statement(statement)
    me = _norm_addr(my_payee_addr)
    if not (s and me and isinstance(state, dict)):
        return False
    if not state.get("funded") or state.get("withdrawn") or state.get("refunded"):
        return False
    if normalize_statement(state.get("hashlock")) != s:
        return False
    try:
        if int(state.get("timelock_ms")) != int(refund_after_ms):
            return False
    except (ValueError, TypeError):
        return False
    return _norm_addr(state.get("payee")) == me


# ── Rail object (nối vào run_tclk_complete qua value_rail=...) ────────────────
class X402HtlcRail:
    """Rail HTLC-trên-x402. name='x402'. submit_fn/read_fn tiêm vào -> không mạng ở đây."""

    name = RAIL

    def __init__(self, *, config=None, submit_fn=None, read_fn=None, log=print):
        self.cfg = config or load_config()
        self.submit_fn = submit_fn          # ({"to","fn","args"}) -> tx_hash
        self.read_fn = read_fn              # (addr, "getContract", cid) -> state dict|None
        self.log = log

    def configured(self) -> bool:
        """Đủ để chạm chain thật? Thiếu bất kỳ mảnh nào -> chưa cấu hình (fail-closed)."""
        c = self.cfg
        return bool(c.get("rpc_url") and c.get("htlc_addr") and c.get("payee_addr")
                    and self.submit_fn and self.read_fn)

    def _read_state(self, contract):
        if not self.read_fn:
            return None
        try:
            return self.read_fn(self.cfg["htlc_addr"], "getContract", contract)
        except Exception as e:
            self.log(f"[x402] read state lỗi {str(contract)[:14]}: {str(e)[:80]}")
            return None

    def verify_lock(self, contract, statement, refund_after_ms) -> bool:
        """CỔNG: đọc state on-chain rồi verify_lock_state. Chưa cấu hình -> False (coi như chưa lock)."""
        if not self.configured():
            return False
        return verify_lock_state(self._read_state(contract), statement,
                                 refund_after_ms, self.cfg["payee_addr"])

    def claim(self, contract, preimage_hex, *, now_ms, refund_after_ms=None, dry_run=True) -> dict:
        """PAYEE nộp preimage -> withdraw USDC. Kết quả là dict KHÔNG throw (như token_manager):
        claimed | would_claim | skipped_unconfigured | skipped_window | skipped_preimage | error_submit."""
        if not self.configured():
            return {"outcome": "skipped_unconfigured", "rail": RAIL}
        if refund_after_ms is not None and not claimable(now_ms, refund_after_ms, self.cfg["claim_margin_ms"]):
            return {"outcome": "skipped_window", "rail": RAIL}   # trễ -> KHÔNG lộ preimage vô ích
        if dry_run:
            return {"outcome": "would_claim", "rail": RAIL, "contract": contract}
        try:
            tx = self.submit_fn({"to": self.cfg["htlc_addr"], "fn": "withdraw",
                                 "args": [contract, preimage_hex]})
            if not tx:
                return {"outcome": "error_submit", "rail": RAIL, "reason": "no tx hash"}
            return {"outcome": "claimed", "rail": RAIL, "tx": tx}
        except Exception as e:
            return {"outcome": "error_submit", "rail": RAIL, "reason": str(e)[:120]}

    # --- Vai PAYER (tuỳ chọn) — scaffold, cùng ethos: chưa cấu hình -> skipped_unconfigured ---
    def lock(self, contract, statement, amount, refund_after_ms, payee_addr, *, dry_run=True) -> dict:
        """PAYER mở + fund escrow (USDC qua x402/Gateway). TODO: encode newContract + settle."""
        if not self.configured():
            return {"outcome": "skipped_unconfigured", "rail": RAIL}
        mx = self.cfg.get("max_amount", 0)
        if mx and int(amount) > mx:
            return {"outcome": "skipped_over_cap", "rail": RAIL, "cap": mx}
        if dry_run:
            return {"outcome": "would_lock", "rail": RAIL, "contract": contract}
        try:
            tx = self.submit_fn({"to": self.cfg["htlc_addr"], "fn": "newContract",
                                 "args": [contract, statement, int(refund_after_ms),
                                          payee_addr, int(amount)]})
            return {"outcome": "locked", "rail": RAIL, "tx": tx} if tx else \
                   {"outcome": "error_submit", "rail": RAIL, "reason": "no tx hash"}
        except Exception as e:
            return {"outcome": "error_submit", "rail": RAIL, "reason": str(e)[:120]}

    def refund(self, contract, *, dry_run=True) -> dict:
        """PAYER đòi lại USDC sau time-lock. TODO: encode refund()."""
        if not self.configured():
            return {"outcome": "skipped_unconfigured", "rail": RAIL}
        if dry_run:
            return {"outcome": "would_refund", "rail": RAIL, "contract": contract}
        try:
            tx = self.submit_fn({"to": self.cfg["htlc_addr"], "fn": "refund", "args": [contract]})
            return {"outcome": "refunded", "rail": RAIL, "tx": tx} if tx else \
                   {"outcome": "error_submit", "rail": RAIL, "reason": "no tx hash"}
        except Exception as e:
            return {"outcome": "error_submit", "rail": RAIL, "reason": str(e)[:120]}


def build_rail(*, submit_fn=None, read_fn=None, log=print):
    """Factory dùng ở agent_cron: trả None khi rail TẮT (mặc định) -> caller giữ đường paper."""
    if not x402_enabled():
        return None
    return X402HtlcRail(submit_fn=submit_fn, read_fn=read_fn, log=log)


if __name__ == "__main__":                                    # demo offline, không mạng
    st = {"funded": True, "withdrawn": False, "refunded": False,
          "hashlock": "0x" + "ab" * 32, "timelock_ms": 1788399477208,
          "payee": "0x" + "11" * 20}
    print("verify_lock_state (ok):",
          verify_lock_state(st, "0x" + "ab" * 32, 1788399477208, "0x" + "11" * 20))
    print("verify_lock_state (wrong payee):",
          verify_lock_state(st, "0x" + "ab" * 32, 1788399477208, "0x" + "22" * 20))
    print("claimable (in window):", claimable(1788399000000, 1788399477208, 180000))
    print("claimable (too late):", claimable(1788399400000, 1788399477208, 180000))
