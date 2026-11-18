"""dashboard/src/sealedbox.js must produce what libsodium's crypto_box_seal_open can read (GitHub Actions secrets use that format).
Skipped when libsodium or node is not installed."""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = """
import { sealToBase64 } from './dashboard/src/sealedbox.js';
import { blake2b } from './dashboard/src/blake2b.js';
const hex = (u) => Buffer.from(u).toString('hex');
const cases = [];
for (const len of [0, 1, 63, 64, 65, 127, 128, 129, 300]) {
  const d = Uint8Array.from({ length: len }, (_, i) => (i * 31 + 7) & 255);
  for (const n of [24, 64]) cases.push({ data: hex(d), n, digest: hex(blake2b(d, n)) });
}
console.log(JSON.stringify({ cases, sealed: sealToBase64(process.argv[2], process.argv[1]) }));
"""
SECRET = "ghp_Example/Secret+value=ünïcode ✓"


def test_sealed_box_opens_with_libsodium_and_blake2b_matches_hashlib():
    lib = ctypes.util.find_library("sodium")
    if not lib or not shutil.which("node"):
        return
    s = ctypes.CDLL(lib)
    if s.sodium_init() < 0:
        return
    pk, sk = ctypes.create_string_buffer(32), ctypes.create_string_buffer(32)
    assert s.crypto_box_keypair(pk, sk) == 0
    r = subprocess.run(["node", "--input-type=module", "-e", JS, base64.b64encode(pk.raw).decode(), SECRET], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    for c in d["cases"]:
        assert hashlib.blake2b(bytes.fromhex(c["data"]), digest_size=c["n"]).hexdigest() == c["digest"]
    sealed = base64.b64decode(d["sealed"])
    out = ctypes.create_string_buffer(len(sealed) - 48)
    assert s.crypto_box_seal_open(out, sealed, ctypes.c_ulonglong(len(sealed)), pk, sk) == 0
    assert out.raw.decode("utf-8") == SECRET
