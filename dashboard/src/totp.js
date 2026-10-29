// RFC 6238 TOTP (SHA-1, 30 s, 6 digits) on Web Crypto, plus base32 for authenticator apps.
const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

export function base32encode(u8) {
  let bits = 0, value = 0, out = "";
  for (const b of u8) {
    value = (value << 8) | b; bits += 8;
    while (bits >= 5) { out += B32[(value >>> (bits - 5)) & 31]; bits -= 5; }
  }
  if (bits > 0) out += B32[(value << (5 - bits)) & 31];
  return out;
}

export function base32decode(s) {
  const clean = String(s).toUpperCase().replace(/[\s=-]/g, "");
  let bits = 0, value = 0;
  const out = [];
  for (const ch of clean) {
    const i = B32.indexOf(ch);
    if (i < 0) throw new Error("bad base32");
    value = (value << 5) | i; bits += 5;
    if (bits >= 8) { out.push((value >>> (bits - 8)) & 255); bits -= 8; }
  }
  return Uint8Array.from(out);
}

export const newTotpSecret = () => base32encode(crypto.getRandomValues(new Uint8Array(20)));
export const otpauthUri = (secret, account, issuer) =>
  `otpauth://totp/${encodeURIComponent(issuer)}:${encodeURIComponent(account)}?secret=${secret}&issuer=${encodeURIComponent(issuer)}&algorithm=SHA1&digits=6&period=30`;

export async function hotp(secret, counter, digits = 6) {
  const key = await crypto.subtle.importKey("raw", secret, { name: "HMAC", hash: "SHA-1" }, false, ["sign"]);
  const msg = new Uint8Array(8);
  new DataView(msg.buffer).setBigUint64(0, BigInt(counter));
  const h = new Uint8Array(await crypto.subtle.sign("HMAC", key, msg));
  const o = h[19] & 15;
  const bin = ((h[o] & 0x7f) << 24) | (h[o + 1] << 16) | (h[o + 2] << 8) | h[o + 3];
  return String(bin % 10 ** digits).padStart(digits, "0");
}

/** @returns the matching time-step counter (so a used code can be refused a second time) or -1. Accepts one step either side. */
export async function verifyTotp(secretB32, code, nowMs = Date.now(), window = 1) {
  const c = String(code || "").replace(/\s/g, "");
  if (!/^\d{6}$/.test(c)) return -1;
  const secret = base32decode(secretB32), step = Math.floor(nowMs / 30000);
  let hit = -1;
  for (let w = -window; w <= window; w++) {
    const ok = (await hotp(secret, step + w)) === c;                      // no early exit: same work for every guess
    if (ok) hit = step + w;
  }
  return hit;
}
