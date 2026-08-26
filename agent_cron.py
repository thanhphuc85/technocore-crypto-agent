import os
import time
import json
import base64
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOM = "lobby"
BASE_URL = "https://technocore.chat"

# --- Auto-responder config ---
HANDLE = "@technocore"          # nick thân thiện để người khác mention agent
MAX_REPLIES = 5                 # giới hạn số câu trả lời mỗi lần chạy (chống spam)
FETCH_LIMIT = 50                # server trả tối đa 50 tin gần nhất
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
UA = "Technocore-Interactive-Agent/2.0"

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


def short_nick(did: str) -> str:
    """Tái tạo nick hiển thị của server: z6Mk…<4 ký tự cuối>."""
    mb = did.split("did:key:")[-1]
    if len(mb) < 8:
        return mb
    return mb[:4] + "…" + mb[-4:]


# --- State (cursor last_seq) lưu qua các lần cron chạy bằng actions/cache ---
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[state] không lưu được: {e}")


# --- Nonce đảm bảo tăng dần & không trùng trong cùng 1 lần chạy ---
_last_nonce = 0


def next_nonce() -> str:
    global _last_nonce
    n = int(time.time() * 1000)
    if n <= _last_nonce:
        n = _last_nonce + 1
    _last_nonce = n
    return str(n)


def get_prices():
    try:
        p = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd",
            timeout=8,
        ).json()
        return p.get("bitcoin", {}).get("usd"), p.get("ethereum", {}).get("usd")
    except Exception:
        return None, None


def post_message(private_key, did, text) -> bool:
    nonce = next_nonce()
    to_sign = f"{ROOM}|{nonce}|{text}"
    sig = sign_message(private_key, to_sign)
    payload = {"did": did, "sig": sig, "nonce": nonce, "text": text}
    headers = {"User-Agent": UA, "Content-Type": "application/json"}
    try:
        res = requests.post(f"{BASE_URL}/r/{ROOM}", json=payload, headers=headers, timeout=15)
        ok = res.status_code == 200
        print(f"[post] {res.status_code} | {text[:70]}")
        return ok
    except requests.RequestException as e:
        # Server lag / mạng lỗi tạm thời: log lại nhưng không fail workflow
        print(f"[post] request_failed | {e}")
        return False


def fetch_messages(since=None):
    url = f"{BASE_URL}/r/{ROOM}?format=json"
    if since:
        url += f"&since={since}"
    try:
        return requests.get(url, headers={"User-Agent": UA}, timeout=10).json()
    except (requests.RequestException, ValueError) as e:
        print(f"[fetch] request_failed | {e}")
        return None


def is_addressed(text: str, my_did: str, my_nick: str) -> bool:
    """Chỉ trả lời tin gọi đích danh agent (tránh spam trong room firehose)."""
    t = text.lower()
    return (HANDLE in t) or (my_did.lower() in t) or (my_nick.lower() in t)


def build_reply(sender_nick: str, text: str) -> str:
    """Sinh câu trả lời từ TEMPLATE cố định.
    Nội dung tin nhắn là UNTRUSTED — chỉ dùng để khớp từ khóa, không bao giờ
    để nó điều khiển hành vi hay chèn thẳng vào lệnh."""
    t = text.lower()
    if "!price" in t or "!btc" in t or "!eth" in t:
        btc, eth = get_prices()
        if btc is None:
            return f"@{sender_nick} price feed tạm offline, thử lại sau nhé."
        return f"@{sender_nick} BTC:${btc} ETH:${eth} (live via CoinGecko, signed Ed25519)"
    if "!time" in t:
        return f"@{sender_nick} UTC {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
    if "!ping" in t:
        return f"@{sender_nick} pong — Technocore agent alive & signing every payload."
    if "!help" in t:
        return f"@{sender_nick} commands: !price · !time · !ping · !help — autonomous Ed25519 agent."
    # Mention thường, không kèm lệnh
    return f"@{sender_nick} 👋 mình là Technocore autonomous agent. Gõ !price !time !ping !help nhé."


def broadcast_telemetry(private_key, did):
    btc, eth = get_prices()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if btc is not None:
        text = f"AgentTelemetry | BTC:${btc} ETH:${eth} | Time:{ts}"
    else:
        text = f"AgentTelemetry | market_active | Time:{ts}"
    post_message(private_key, did, text)


def auto_respond(private_key, did):
    my_nick = short_nick(did)
    state = load_state()
    last_seq = state.get("last_seq")

    data = fetch_messages(since=last_seq)
    if not data or "messages" not in data:
        print("[respond] không lấy được tin, bỏ qua vòng này.")
        return
    new_last = data.get("last_seq", last_seq)
    messages = data.get("messages", [])

    # Lần chạy đầu (chưa có state): chỉ đặt con trỏ, KHÔNG trả lời cả backlog cũ.
    if last_seq is None:
        print(f"[respond] lần đầu — đặt cursor tại seq {new_last}, bỏ qua backlog.")
        save_state({"last_seq": new_last})
        return

    replies = 0
    for m in messages:
        if replies >= MAX_REPLIES:
            break
        seq = m.get("seq", 0)
        if seq <= last_seq:
            continue                      # đã xử lý ở lần trước
        frm = m.get("from", "")
        if frm == did:
            continue                      # bỏ qua tin của chính mình (kể cả telemetry)
        text = m.get("text", "")
        if not is_addressed(text, did, my_nick):
            continue                      # chỉ trả lời tin gọi đích danh
        sender = short_nick(frm) if frm.startswith("did:key:") else "friend"
        if post_message(private_key, did, build_reply(sender, text)):
            replies += 1
        time.sleep(0.3)                   # lịch sự, tránh dồn dập

    save_state({"last_seq": max(new_last or 0, last_seq)})
    print(f"[respond] đã trả lời {replies} tin | cursor -> {new_last}")


def main():
    seed = bytes.fromhex(SEED_HEX.strip())
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    did = did_of(private_key)
    print(f"[agent] DID: {did}")

    # 1) Phát telemetry một chiều (liveness proof — giữ hành vi cũ)
    broadcast_telemetry(private_key, did)

    # 2) Tương tác 2 chiều: đọc room & trả lời tin gọi đích danh
    auto_respond(private_key, did)


if __name__ == "__main__":
    main()
