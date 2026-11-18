#!/usr/bin/env python3
"""Removes files that older versions left behind (unzip never deletes anything). Prints only paths and counts.

  cleanup_repo.py engine <dir>              delete the obsolete engine files listed below
  cleanup_repo.py brain  <dir> [--delete-pending]
                                            brain repo: list (and with --delete-pending remove) the old pending/ drafts

Run from the `cleanup` workflow (Actions tab). Safe to run twice."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

OBSOLETE_ENGINE = [
    "worker",                                   # Telegram approve Worker (folder)
    ".github/workflows/setup-approve.yml",
    "scripts/setup_approve_worker.py",
    "scripts/pending_triage.py",
    "docs/TELEGRAM_APPROVE.md",
    "tests/test_approve_buttons.py",
    "tests/test_setup_approve.py",
    "tests/test_pending_triage.py",
    "brain-template/pending",
]


def clean_engine(root: Path) -> list[str]:
    gone = []
    for rel in OBSOLETE_ENGINE:
        p = root / rel
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        else:
            continue
        gone.append(rel)
    return gone


def clean_brain(root: Path, delete_pending: bool) -> tuple[int, bool]:
    pend = root / "pending"
    n = len(list(pend.rglob("*.md"))) if pend.is_dir() else 0
    if delete_pending and pend.is_dir():
        shutil.rmtree(pend)
    return n, delete_pending and n > 0


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("engine", "brain"):
        print(__doc__)
        return 2
    root = Path(argv[2])
    if argv[1] == "engine":
        gone = clean_engine(root)
        print("removed:", ", ".join(gone) if gone else "nothing (already clean)")
    else:
        n, removed = clean_brain(root, "--delete-pending" in argv)
        print(f"pending drafts found: {n}; {'deleted' if removed else 'kept'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
