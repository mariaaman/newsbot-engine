#!/usr/bin/env python3
"""Cloudflare Pages free plan: 20,000 files per site, 25 MiB per file. Warn above 12,000, fail above 16,000.
Usage: python scripts/check_file_count.py site/public [--warn N] [--fail N]. Exit 0 ok/warn, 1 fail."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

WARN, FAIL, MAX_FILE_BYTES = 12_000, 16_000, 25 * 1024 * 1024


def check(root: Path, warn: int = WARN, fail: int = FAIL) -> tuple[str, int, list[str]]:
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"build dir not found: {root}")
    files = [p for p in root.rglob("*") if p.is_file()]
    problems = [f"too large: {p.relative_to(root)}" for p in files if p.stat().st_size > MAX_FILE_BYTES]
    n = len(files)
    status = "fail" if (n > fail or problems) else "warn" if n > warn else "ok"
    return status, n, problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--warn", type=int, default=WARN)
    ap.add_argument("--fail", type=int, default=FAIL)
    a = ap.parse_args(argv)
    status, n, problems = check(Path(a.root), a.warn, a.fail)
    print(f"files={n} status={status} warn_at={a.warn} fail_at={a.fail}")
    for p in problems:
        print(p)
    if status == "warn":
        print("WARNING: approaching the Pages 20,000-file limit; see docs/SCALING.md (archive strategy)")
    return 1 if status == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
