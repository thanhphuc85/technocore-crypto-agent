import os
import time
import requests
import nacl.signing
import nacl.encoding

# 1. Lấy Private Key từ GitHub Secrets (chuỗi Hex)
PRIVKEY_HEX = os.environ.get("AGENT_PRIVATE_KEY")

if not PRIVKEY_HEX:
    raise ValueError("CHÚ Ý: Chưa tìm thấy AGENT_PRIVATE_KEY trong GitHub Secrets!")

# 2. Tạo chữ ký số Ed25519 & DID
signing_key = nacl.signing.SigningKey(bytes.fromhex(PRIVKEY_HEX.strip()))
verify_key = signing_key.verify_key
pubkey_bytes = verify_key.encode()

# Tạo mã DID multibase
did = f"did:key:z6M{nacl.encoding.Base58Encoder.encode(pubkey_bytes).decode('utf-8')}"

# 3. Lấy dữ liệu BTC
btc_price = "N/A"
try:
    res = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", timeout=10).json()
    btc_price = res.get("bitcoin", {}).get("usd", "N/A")
except Exception as e:
    print(f"Lỗi API: {e}")

# 4. Ký chữ ký số chuẩn Technocore
nonce = str(int(time.time() * 1000))
timestamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
text_payload = f"BTC/USD is ${btc_price}. Source: CoinGecko. Fetched: {timestamp}."

# Ký dữ liệu Ed25519
signed_bytes = signing_key.sign(text_payload.encode('utf-8'))
sig_b64 = nacl.encoding.URLSafeBase64Encoder.encode(signed_bytes.signature).decode('utf-8')

# 5. Gửi lên endpoint say-signed
endpoint = "https://technocore.chat/r/lobby/say-signed"
payload = {
    "did": did,
    "text": text_payload,
    "nonce": nonce,
    "sig_b64url": sig_b64
}

response = requests.post(endpoint, json=payload, headers={"Content-Type": "application/json"})
print(f"Status: {response.status_code}")
print(f"Response: {response.text}")
