import os
import time
import json
import requests
import nacl.signing
import nacl.encoding

# 1. Lấy Private Key từ GitHub Secrets (Hex format)
PRIVKEY_HEX = os.environ.get("AGENT_PRIVATE_KEY") or os.environ.get("TECHNOCRATION_PRIVKEY")

if not PRIVKEY_HEX:
    raise ValueError("CHÚ Ý: Chưa thiết lập AGENT_PRIVATE_KEY trong GitHub Secrets!")

# 2. Khởi tạo khóa ký Ed25519 & DID
signing_key = nacl.signing.SigningKey(bytes.fromhex(PRIVKEY_HEX))
verify_key = signing_key.verify_key
pubkey_bytes = verify_key.encode()

# Mã DID chuẩn (multibase/ed25519 format)
did = f"did:key:z6M{nacl.encoding.Base58Encoder.encode(pubkey_bytes).decode('utf-8')}"

# 3. Lấy dữ liệu Telemetry (Giá Crypto)
btc_price = "N/A"
try:
    res = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", timeout=10).json()
    btc_price = res.get("bitcoin", {}).get("usd", "N/A")
except Exception as e:
    print(f"Lỗi lấy giá API: {e}")

# 4. Tạo payload & Ký chữ ký số Cryptographic (Ed25519)
nonce = str(int(time.time() * 1000))
timestamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
text_payload = f"BTC/USD is ${btc_price}. Source: CoinGecko. Fetched: {timestamp}."

# Ký dữ liệu
signed_bytes = signing_key.sign(text_payload.encode('utf-8'))
sig_b64 = nacl.encoding.URLSafeBase64Encoder.encode(signed_bytes.signature).decode('utf-8')

# 5. Gửi lên Endpoint Official của Technocore (/say-signed)
endpoint = "https://technocore.chat/r/lobby/say-signed"
payload = {
    "did": did,
    "text": text_payload,
    "nonce": nonce,
    "sig_b64url": sig_b64
}

headers = {"Content-Type": "json"}
response = requests.post(endpoint, json=payload, headers=headers)

print(f"Status Code: {response.status_code}")
print(f"Response Payload: {response.text}")
