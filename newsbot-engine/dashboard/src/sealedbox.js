// libsodium's crypto_box_seal in pure JS (tweetnacl + BLAKE2b), the format GitHub wants for Actions secrets.
// Why not libsodium-wrappers: Cloudflare Workers refuse to compile WebAssembly at runtime, and that package does exactly that.
import nacl from "./vendor/nacl-fast.cjs";
import { blake2b } from "./blake2b.js";

export const b64decode = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
export const b64encode = (u8) => { let s = ""; for (const b of u8) s += String.fromCharCode(b); return btoa(s); };

/** @param {Uint8Array|string} message @param {string} publicKeyB64 base64 public key (GitHub's actions/secrets/public-key) @returns base64 */
export function sealToBase64(message, publicKeyB64) {
  const pk = b64decode(publicKeyB64);
  if (pk.length !== 32) throw new Error("bad public key");
  const msg = typeof message === "string" ? new TextEncoder().encode(message) : message;
  const eph = nacl.box.keyPair();
  const nonceInput = new Uint8Array(64);
  nonceInput.set(eph.publicKey, 0);
  nonceInput.set(pk, 32);
  const nonce = blake2b(nonceInput, 24);                       // crypto_box_NONCEBYTES
  const boxed = nacl.box(msg, nonce, pk, eph.secretKey);
  const out = new Uint8Array(32 + boxed.length);
  out.set(eph.publicKey, 0);
  out.set(boxed, 32);
  return b64encode(out);
}
