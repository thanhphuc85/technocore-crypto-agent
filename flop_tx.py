"""
flop_tx.py — Khung `submit_tx` cho token_manager: KÝ + GỬI một giao dịch token thật
khi TESTNET_ENABLED=true. Đây là "seam" được tiêm vào `spend()`; token_manager giữ
kế toán, còn file này giữ phần chạm mạng/chain. Đến ngày FLOP công bố RPC/relayer,
chỉ cần điền endpoint (và, nếu là EVM, cắm hàm ký EVM) — logic kế toán KHÔNG đổi.

Hợp đồng (contract) với token_manager.spend():
    submit_tx(tx: dict) -> {"tx_hash": <str>}
    tx = {
        "token":   "FLOP",
        "amount":  "0.001",          # chuỗi thập phân
        "memo":    "Gemini Inference",
        "rpc_url": "<FLOP_RPC_URL>",
        "signed":  {"did":.., "token":.., "amount":.., "nonce":.., "memo":.., "sig":..}
                   # payload đã KÝ Ed25519 (do token_manager.sign_transaction tạo)
    }
  - Thành công -> trả {"tx_hash": ...}.
  - Thất bại  -> RAISE; token_manager.spend() sẽ bắt và trả outcome 'error_submit'
                (không làm sập agent).

Chọn adapter qua biến môi trường FLOP_TX_MODE:
    'relay' (mặc định) -> relay_submit_tx   (POST payload đã-ký tới FLOP_SUBMIT_URL)
    'evm'              -> evm_submit_tx      (STUB: cần secp256k1, xem chú thích)
    'off'/'none'       -> None               (tắt: spend testnet -> skipped_unconfigured)
"""

import os
import requests


def _clean(v) -> str:
    return (v or "").strip()


def _extract_tx_hash(data) -> str:
    """Rút tx hash từ nhiều dạng body phổ biến (relayer / JSON-RPC). None nếu không thấy."""
    if not isinstance(data, dict):
        return None
    for k in ("tx_hash", "txHash", "hash", "txid", "transactionHash"):
        v = data.get(k)
        if isinstance(v, str) and v:
            return v
    res = data.get("result")
    if isinstance(res, str) and res:              # JSON-RPC eth_sendRawTransaction -> result = hash
        return res
    if isinstance(res, dict):
        for k in ("tx_hash", "txHash", "hash"):
            if isinstance(res.get(k), str) and res[k]:
                return res[k]
    return None


def relay_submit_tx(tx: dict) -> dict:
    """Adapter MẶC ĐỊNH: relay một giao dịch ĐÃ KÝ Ed25519 tới một endpoint HTTP —
    đúng pattern agent đang dùng để POST tin có ký lên technocore.chat. Không cần
    thêm dependency (chỉ `requests`), và dùng lại chính chữ ký Ed25519 của agent.

    Endpoint lấy từ FLOP_SUBMIT_URL (ưu tiên) hoặc tx['rpc_url']. Body = payload đã ký.
    Điều chỉnh khi biết wire-format thật của FLOP: đây là chỗ DUY NHẤT cần sửa."""
    signed = tx.get("signed")
    if not signed:
        raise RuntimeError(
            "relay_submit_tx cần payload đã ký — truyền private_key + did vào spend()/meter_inference()"
        )
    url = _clean(os.environ.get("FLOP_SUBMIT_URL")) or _clean(tx.get("rpc_url"))
    if not url:
        raise RuntimeError("thiếu FLOP_SUBMIT_URL / FLOP_RPC_URL để relay giao dịch")

    body = dict(signed)                            # {did, token, amount, nonce, memo, sig}
    headers = {
        "User-Agent": _clean(os.environ.get("FLOP_TX_UA")) or "flop-agent/1.0",
        "Content-Type": "application/json",
    }
    r = requests.post(url, json=body, headers=headers, timeout=20)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        data = {}
    tx_hash = _extract_tx_hash(data)
    if not tx_hash:
        raise RuntimeError(f"relay không trả tx hash: {str(data)[:160]}")
    return {"tx_hash": tx_hash}


def evm_submit_tx(tx: dict) -> dict:
    """STUB adapter EVM (eth_sendRawTransaction). CHƯA nối vì:
      - EVM ký bằng secp256k1 (RLP + keccak), KHÁC khóa Ed25519 của agent -> cần một
        khóa EVM riêng và thư viện ký (eth-account / web3), không có trong repo hiện tại.
      - FLOP chưa xác nhận chain là EVM hay contract token địa chỉ nào.
    Khi FLOP xác nhận EVM: đặt EVM_PRIVATE_KEY (secp256k1), FLOP_TOKEN_CONTRACT, rồi
    hiện thực: build transfer(to,amount) -> ký -> POST eth_sendRawTransaction tới rpc_url,
    trả {"tx_hash": result}. Trước đó, raise rõ ràng để không ai tưởng đã gửi thật."""
    raise NotImplementedError(
        "evm_submit_tx chưa nối: cần khóa secp256k1 + eth-account và địa chỉ contract "
        "token EVM. Dùng relay_submit_tx (mặc định) cho tới khi FLOP xác nhận chain EVM."
    )


def build_submit_tx(env: dict = None):
    """Factory: chọn adapter theo FLOP_TX_MODE. Trả None khi tắt hoặc CHƯA cấu hình
    endpoint (relay) -> token_manager.spend() sẽ báo 'skipped_unconfigured' thay vì gửi
    tới nơi đoán mò. token_manager gọi hàm này để tự nối submit_tx ở chế độ testnet."""
    env = env if env is not None else os.environ
    mode = _clean(env.get("FLOP_TX_MODE")).lower() or "relay"
    if mode in ("off", "none"):
        return None
    if mode == "evm":
        return evm_submit_tx
    # relay: chỉ trả adapter khi đã có endpoint (không thì để spend() báo unconfigured)
    if not (_clean(env.get("FLOP_SUBMIT_URL")) or _clean(env.get("FLOP_RPC_URL"))):
        return None
    return relay_submit_tx
