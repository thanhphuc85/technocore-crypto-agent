import json
import os
import time
import urllib.parse
import requests

FULL_DID = "did:key:z6MkiCxCfTP6gHmWrJvPgF4UtxYL4upzry6hTAs6g1ni2C8g"
# Chuyển nick thành chữ thường 100% đúng quy định Technocore
SHORT_NICK = "did_key_z6mkicxcftp"
ROOM_NAME = "lobby"
BASE_URL = "https://technocore.chat"


def fetch_crypto_prices():
  try:
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd"
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers, timeout=5)
    if res.status_code == 200:
      data = res.json()
      btc = data.get("bitcoin", {}).get("usd", 0)
      eth = data.get("ethereum", {}).get("usd", 0)
      return f"btc:${btc}_eth:${eth}"
  except Exception as e:
    print(f"Bỏ qua lỗi API giá: {e}")
  return "market_active"


def main():
  timestamp = time.strftime("%Y-%m-%d_%H:%M_UTC", time.gmtime())
  market_info = fetch_crypto_prices()

  raw_msg = (
      f"AgentTelemetry | DID:{FULL_DID} | Data:[{market_info}] | Time:{timestamp}"
  )
  safe_msg = urllib.parse.quote(raw_msg, safe="")

  say_url = f"{BASE_URL}/r/{ROOM_NAME}/say/{SHORT_NICK}/{safe_msg}"
  headers = {"User-Agent": "Technocore-Agent/1.0"}

  try:
    res = requests.get(say_url, headers=headers, timeout=10)
    print(f"Status Code: {res.status_code} - Response: {res.text}")
  except Exception as e:
    print(f"Error: {e}")


if __name__ == "__main__":
  main()
