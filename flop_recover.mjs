// flop_recover.mjs — lấy lại escrow của kênh compute ĐỨNG TÊN VÍ AGENT khi CLI không tự khôi phục được.
//
// Chạy BÊN TRONG image CLI chính thức (ghim digest) bằng SDK của chính image:
//   docker run -w /app --entrypoint node -e FLOP_RECOVER_SECRET ... <image> --input-type=module -e "<nội dung file>"
// (cwd /app để `@flop-labs/sdk` và `@polkadot/*` của image được resolve.)
//
// Chỉ gọi hai lệnh hoàn tiền của pallet computeChannel, đúng như SDK tự gọi trong `session run`:
//   - PendingAck quá ackDeadline           -> expireForceOpen(id)  (hoàn đủ escrow, ai gọi cũng được)
//   - Open quá openedAt + sla.maxDuration -> timeout(id)          (hoàn escrow chưa dùng)
// Trạng thái đọc ở block ĐÃ CHỐT; kênh không đứng tên ví (agent khác ví) -> không đụng tới.
//
// Env: FLOP_RECOVER_RPC (wss), FLOP_RECOVER_CHANNELS (id 0x…64 hex, ngăn dấu phẩy), FLOP_RECOVER_ADDR (ví agent),
//      FLOP_RECOVER_SECRET (seed ed25519 0x…64 hex; không cần khi FLOP_RECOVER_DRY=1), FLOP_RECOVER_RPC_TIMEOUT_MS.
// Ra: mỗi kênh một dòng JSON {channel, state, action, ok?, tx?, refunded?, error?}; dòng cuối {"done": true}.
import { connectFlopApi, computeChannel } from "@flop-labs/sdk";
import { Keyring } from "@polkadot/keyring";
import { cryptoWaitReady, decodeAddress } from "@polkadot/util-crypto";

const env = process.env;
const out = (o) => console.log(JSON.stringify(o));
const hex = (u8) => Buffer.from(u8).toString("hex");
const same = (a, b) => { try { return hex(decodeAddress(a)) === hex(decodeAddress(b)); } catch { return false; } };
const ids = [...new Set((env.FLOP_RECOVER_CHANNELS || "").split(",").map((s) => s.trim().toLowerCase())
  .filter((s) => /^0x[0-9a-f]{64}$/.test(s)))].slice(0, 8);
const dry = env.FLOP_RECOVER_DRY === "1";
const want = env.FLOP_RECOVER_ADDR || "";

await cryptoWaitReady();
let pair = null;
if (!dry) {
  const seed = env.FLOP_RECOVER_SECRET || "";
  if (!/^0x[0-9a-fA-F]{64}$/.test(seed)) { out({ ok: false, error: "secret missing" }); process.exit(2); }
  pair = new Keyring({ type: "ed25519", ss58Format: 42 }).addFromSeed(Uint8Array.from(Buffer.from(seed.slice(2), "hex")));
  if (!want || !same(pair.address, want)) { out({ ok: false, error: "secret is not the agent wallet" }); process.exit(2); }
}
const api = await connectFlopApi(env.FLOP_RECOVER_RPC || "wss://testnet.flop.network", undefined,
  { timeoutMs: Number(env.FLOP_RECOVER_RPC_TIMEOUT_MS || 60000) });
try {
  const head = await api.rpc.chain.getFinalizedHead();
  const at = await api.at(head);
  const block = BigInt((await api.rpc.chain.getHeader(head)).number.toString());
  const client = pair ? new computeChannel.ComputeChannelChainClient(new computeChannel.FlopChainSubmitter(api, pair)) : null;
  for (const id of ids) {
    const r = { channel: id, block: block.toString() };
    try {
      const raw = await at.query.computeChannel.channels(id);
      if (!raw.isSome) { out({ ...r, state: "none", action: "none" }); continue; }
      const j = raw.unwrap().toJSON();
      const state = Object.keys(j.state || {})[0] || "";
      r.state = state;
      if (!want || !same(String(j.agent || ""), want)) { out({ ...r, action: "foreign" }); continue; }
      const openedAt = BigInt(j.openedAt), end = openedAt + BigInt(j.sla?.maxDuration ?? 0);
      let action = "wait";
      if (state === "pendingAck") {
        const ack = BigInt(j.state.pendingAck?.ackDeadline ?? end);
        r.deadline = ack.toString();
        if (block > ack) action = "expire";
      } else if (state === "open") {
        r.deadline = end.toString();
        if (block > end) action = "timeout";
      } else if (state === "closed") action = "closed";
      else action = "busy";                                   // đang settle / tranh chấp: để luồng finalize lo
      r.action = action;
      if (dry || (action !== "expire" && action !== "timeout")) { out(r); continue; }
      const res = action === "timeout" ? await client.timeout(id) : await client.expireForceOpen(id);
      out({ ...r, ok: true, tx: res.txHash, refunded: res.refunded.toString() });
    } catch (e) {
      out({ ...r, ok: false, error: String(e?.message || e).slice(0, 300) });
    }
  }
} finally {
  await api.disconnect().catch(() => {});
}
out({ done: true });
process.exit(0);
