import os
import time
import base64
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOM = "lobby"
BASE_URL = "https://technocore.chat"

SEED_HEX = os.environ.get("AGENT_PRIVATE_KEY")
if not SEED_HEX or len(SEED_HEX.strip()) != 64:
    raise ValueError("Thiếu AGENT_PRIVATE_KEY (64 hex characters)")

MULTICODEC_ED25519 = b"\xed\x01"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def multibase_b58(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = B58[rem] + out
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
    sig = private_key.sign(message.encode("utf-8"))
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")

def main():
    seed = bytes.fromhex(SEED_HEX.strip())
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    did = did_of(private_key)

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        price = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd",
            timeout=8
        ).json()
        btc = price.get("bitcoin", {}).get("usd", "N/A")
        eth = price.get("ethereum", {}).get("usd", "N/A")
        text = f"AgentTelemetry | BTC:${btc} ETH:${eth} | Time:{timestamp}"
    except Exception:
        text = f"AgentTelemetry | market_active | Time:{timestamp}"

    nonce = str(int(time.time() * 1000))
    to_sign = f"{ROOM}|{nonce}|{text}"
    sig = sign_message(private_key, to_sign)

    # Dùng POST
    url = f"{BASE_URL}/r/{ROOM}"
    payload = {
        "did": did,
        "sig": sig,
        "nonce": nonce,
        "text": text
    }

    headers = {
        "User-Agent": "Technocore-Correct-Agent/1.1",
        "Content-Type": "application/json"
    }

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=15)
        print(f"Status: {res.status_code}")
        print(f"DID: {did}")
        print(f"Nonce: {nonce}")
        print(f"Response: {res.text[:500]}")
    except requests.RequestException as e:
        # Server lag / mạng lỗi tạm thời: log lại nhưng không fail workflow
        print(f"Status: request_failed")
        print(f"DID: {did}")
        print(f"Nonce: {nonce}")
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
