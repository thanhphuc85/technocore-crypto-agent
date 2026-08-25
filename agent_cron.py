import json
import os
import time
import urllib.parse
import requests

# 1. Cấu hình DID và Endpoint Technocore
MY_DID = os.getenv("DID_NICK", "did:flop:0x9a8b7c6d5e4f3a2b1c0d")
ROOM_NAME = "lobby"
BASE_URL = "https://technocore.chat"


def fetch_crypto_prices():
  """Thu thập dữ liệu giá crypto thực tế từ CoinGecko làm nội dung hữu ích"""
  try:
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd"
    res = requests.get(url, timeout=10).json()

    btc = res.get("bitcoin", {}).get("usd", 0)
    eth = res.get("ethereum", {}).get("usd", 0)
    sol = res.get("solana", {}).get("usd", 0)

    return f"BTC:${btc}_ETH:${eth}_SOL:${sol}"
  except Exception as e:
    print(f"Error fetching prices: {e}")
    return "Market_Data_Active"


def main():
  timestamp = time.strftime("%Y-%m-%d_%H:%M_UTC", time.gmtime())
  market_info = fetch_crypto_prices()

  # Tạo thông điệp mang giá trị thật đóng góp cho cộng đồng Agent
  raw_message = (
      f"AgentReport | DID:{MY_DID} | Status:Active | Data:[{market_info}] |"
      f" Time:{timestamp}"
  )

  # Mã hóa URL để tránh gãy ký tự đặc biệt
  safe_did = urllib.parse.quote(MY_DID, safe="")
  safe_msg = urllib.parse.quote(raw_message, safe="")

  # 2. Gửi thông tin vào Phòng Chat công khai (/r/lobby)
  say_url = f"{BASE_URL}/r/{ROOM_NAME}/say/{safe_did}/{safe_msg}"

  # 3. Lưu trạng thái bền vững vào Key-Value Store (/kv/...)
  kv_url = f"{BASE_URL}/kv/{safe_did}/status/set/{safe_msg}"

  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Technocore-Agent/1.0"
      )
  }

  print(f"--- Bắt đầu kích hoạt Agent [{timestamp}] ---")
  try:
    # Gửi tới Room
    r_say = requests.get(say_url, headers=headers, timeout=10)
    print(f"[Say Status]: {r_say.status_code} - Response: {r_say.text[:100]}")

    # Lưu KV Store
    r_kv = requests.get(kv_url, headers=headers, timeout=10)
    print(f"[KV Status]: {r_kv.status_code} - Response: {r_kv.text[:100]}")

    print("---> Tác vụ thành công!")

  except Exception as e:
    print(f"Xảy ra lỗi kết nối: {e}")


if __name__ == "__main__":
  main()
