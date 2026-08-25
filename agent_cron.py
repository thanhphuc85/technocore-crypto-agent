import os
import time
import base64
import hashlib
import urllib.parse
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# ================== CẤU HÌNH ==================
ROOM = "lobby"
BASE_URL = "https://technocore.chat"

# Lấy seed từ GitHub Secrets (64 ký tự hex)
SEED_HEX = os.environ.get("AGENT_PRIVATE_KEY") or os.environ.get("SIGN_SEED")
if not SEED_HEX or len(SEED_HEX.strip()) != 64:
    raise ValueError("Cần set GitHub Secret AGENT_PRIVATE_KEY = 64 hex characters (seed)")

# ================== HÀM CHUẨN OFFICIAL ==================
MULTICODEC_ED25519 = b"\xed\x01"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def multibase_b58(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = B58[rem] + out
    # padding leading zeros
    pad = 0
    for b in raw:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + out

def did_of(private_key: Ed25519PrivateKey) -> str:
    pubkey = private_key.public_key().public_bytes_raw()
    mb = "z" + multibase_b58(MULTICODEC_ED25519 + pubkey)
    return "did:key:" + mb

def sign_message(private_key: Ed25519PrivateKey, message: str) -> str:
    """Trả về 86 ký tự base64url unpadded"""
    sig = private_key.sign(message.encode("utf-8"))
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")

def main():
    # 1. Load key
    seed = bytes.fromhex(SEED_HEX.strip())
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    did = did_of(private_key)

    # 2. Tạo nội dung
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        price_data = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd",
            timeout=8
        ).json()
        btc = price_data.get("bitcoin", {}).get("usd", "N/A")
        eth = price_data.get("ethereum", {}).get("usd", "N/A")
        text = f"AgentTelemetry | BTC:${btc} ETH:${eth} | Time:{timestamp}"
    except Exception:
        text = f"AgentTelemetry | market_active | Time:{timestamp}"

    # 3. Nonce tăng dần (millisecond)
    nonce = str(int(time.time() * 1000))

    # 4. Ký đúng chuẩn: room|nonce|text
    to_sign = f"{ROOM}|{nonce}|{text}"
    sig = sign_message(private_key, to_sign)

    # 5. Gửi bằng GET say-signed (cách chính thức ổn định nhất)
    text_encoded = urllib.parse.quote(text, safe="")
    url = f"{BASE_URL}/r/{ROOM}/say-signed/{did}/{sig}/{nonce}/{text_encoded}"

    headers = {"User-Agent": "Technocore-Correct-Agent/1.0"}
    try:
        res = requests.get(url, headers=headers, timeout=15)
        print(f"Status: {res.status_code}")
        print(f"DID: {did}")
        print(f"Nonce: {nonce}")
        print(f"Response: {res.text[:300]}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
