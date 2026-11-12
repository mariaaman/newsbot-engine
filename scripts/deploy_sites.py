#!/usr/bin/env python3
"""Deploy every site listed in the manifest written by scripts/build_sites.py to Cloudflare Pages (wrangler).
A failed site does not stop the others; the exit code is 1 if any failed, so the workflow does not record the deploy as done.
Needs CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in the environment. Prints language codes and ok/fail only."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def deploy(manifest: list[dict], run=subprocess.run) -> list[tuple[str, bool]]:
    results = []
    for m in manifest:
        ok = False
        if m.get("project") and Path(m["public"]).is_dir():
            cmd = ["npx", "--yes", "wrangler@4", "pages", "deploy", m["public"], "--project-name", m["project"],
                   "--branch", "main", "--commit-dirty=true"]
            r = run(cmd, check=False)
            if getattr(r, "returncode", 1) != 0:        # most likely the project does not exist yet: create it once and try again
                run(["npx", "--yes", "wrangler@4", "pages", "project", "create", m["project"], "--production-branch", "main"], check=False)
                r = run(cmd, check=False)
            ok = getattr(r, "returncode", 1) == 0
        results.append((m["lang"], ok))
        print(f"deploy lang={m['lang']} status={'ok' if ok else 'fail'}")
    return results


def main(argv=None, run=subprocess.run) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(ROOT / "site" / ".build-manifest.json"))
    a = ap.parse_args(argv)
    manifest = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    return 0 if all(ok for _l, ok in deploy(manifest, run)) else 1


if __name__ == "__main__":
    sys.exit(main())
