import test from "node:test";
import assert from "node:assert/strict";
import nacl from "../src/vendor/nacl-fast.cjs";
import { blake2b } from "../src/blake2b.js";
import { sealToBase64, b64decode } from "../src/sealedbox.js";
import { base32encode, base32decode, hotp, verifyTotp } from "../src/totp.js";

const hex = (u) => Buffer.from(u).toString("hex");

test("blake2b matches the RFC 7693 / reference vectors", () => {
  assert.equal(hex(blake2b(new Uint8Array(), 64)), "786a02f742015903c6c6fd852552d272912f4740e15847618a86e217f71f5419d25e1031afee585313896444934eb04b903a685b1448b755d56f701afe9be2ce");
  assert.equal(hex(blake2b(new TextEncoder().encode("abc"), 64)), "ba80a53f981c4d0d6a2797b69f12f6e94c212f14685ac4b74b12bb6fdbffa2d17d87c5392aab792dc252d5de4533cc9518d38aa8dbf1925ab92386edd4009923");
  assert.equal(blake2b(new Uint8Array(64), 24).length, 24);
});

test("sealed box opens with the matching secret key (libsodium layout: epk || box, nonce = blake2b(epk||pk))", () => {
  const kp = nacl.box.keyPair();
  const secret = "ghp_Secret/ü✓+=";
  const sealed = b64decode(sealToBase64(secret, Buffer.from(kp.publicKey).toString("base64")));
  assert.equal(sealed.length, 32 + 16 + new TextEncoder().encode(secret).length);
  const epk = sealed.slice(0, 32), n = new Uint8Array(64);
  n.set(epk); n.set(kp.publicKey, 32);
  const plain = nacl.box.open(sealed.slice(32), blake2b(n, 24), epk, kp.secretKey);
  assert.equal(new TextDecoder().decode(plain), secret);
  const again = b64decode(sealToBase64(secret, Buffer.from(kp.publicKey).toString("base64")));
  assert.notEqual(hex(sealed), hex(again));                                   // fresh ephemeral key every time
  assert.throws(() => sealToBase64("x", "AAAA"), /bad public key/);
});

test("TOTP follows RFC 6238 (SHA-1) and the window / format rules", async () => {
  const secret = new TextEncoder().encode("12345678901234567890");
  for (const [t, code8] of [[59, "94287082"], [1111111109, "07081804"], [1234567890, "89005924"], [2000000000, "69279037"]]) {
    assert.equal(await hotp(secret, Math.floor(t / 30), 8), code8);
  }
  const b32 = base32encode(secret);
  assert.equal(b32, "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ");
  assert.deepEqual(base32decode(b32.toLowerCase()), secret);
  const now = 1111111109 * 1000, step = Math.floor(now / 30000);
  assert.equal(await verifyTotp(b32, "081804", now), step);
  assert.equal(await verifyTotp(b32, "081804", now + 30000), step);          // one step late is accepted
  assert.equal(await verifyTotp(b32, "081804", now + 90000), -1);            // three steps is not
  assert.equal(await verifyTotp(b32, "12345", now), -1);
  assert.equal(await verifyTotp(b32, "abcdef", now), -1);
});
