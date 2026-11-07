"""The dashboard's YAML editor (dashboard/src/yamledit.js) must read the brain templates exactly like PyYAML does, and edits must round-trip."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "brain-template" / "config"
JS = """
import fs from 'node:fs';
import { scan, readLeaf } from './dashboard/src/yamledit.js';
const out = {};
for (const f of process.argv.slice(1)) {
  const sc = scan(fs.readFileSync(f, 'utf8'));
  out[f] = [...sc.leaves].map(([k, l]) => ({ path: JSON.parse(k), kind: l.kind, value: l.kind === 'flowmap' ? null : readLeaf(l) }));
}
console.log(JSON.stringify(out));
"""


def at(d, path):
    for p in path:
        d = d[int(p)] if isinstance(d, list) else d[p]
    return d


def test_every_leaf_matches_pyyaml():
    if not shutil.which("node"):
        return
    files = [str(CONFIG / "newsbot.yaml"), str(CONFIG / "llm.yaml")]
    r = subprocess.run(["node", "--input-type=module", "-e", JS, *files], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out, checked = json.loads(r.stdout), 0
    for f, leaves in out.items():
        data = yaml.safe_load(Path(f).read_text(encoding="utf-8"))
        for leaf in leaves:
            if leaf["kind"] == "flowmap":
                continue
            assert at(data, leaf["path"]) == leaf["value"], (f, leaf["path"])
            checked += 1
    assert checked > 60, checked
