#!/usr/bin/env python3
"""Pin every `uses: owner/repo@tag` in .github/workflows to the commit SHA that tag points to right now.

    python scripts/pin_actions.py            # dry run: prints what would change
    python scripts/pin_actions.py --write    # rewrites the workflow files (keeps the tag in a comment)

Needs the GitHub CLI (`gh`) logged in (GH_TOKEN works). The SHA is looked up live, nothing is guessed. Annotated tags
are followed to the commit. Re-run after you deliberately move to a newer tag. The .github/workflows/pin-actions.yml
workflow runs this for you from the Actions tab (it needs the WORKFLOW_PAT secret, like unzip.yml)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

USES = re.compile(r"^(?P<pre>\s*-?\s*uses:\s*)(?P<repo>[\w.-]+/[\w.-]+(?:/[\w./-]+)?)@(?P<ref>[^\s#]+)(?P<rest>.*)$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def gh_api(path: str) -> dict:
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path} failed")
    return json.loads(out.stdout)


def resolve(repo: str, ref: str, api=gh_api) -> str:
    owner_repo = "/".join(repo.split("/")[:2])
    obj = api(f"repos/{owner_repo}/git/ref/tags/{ref}")["object"]
    for _ in range(3):                                   # annotated tag -> tag object -> commit
        if obj["type"] == "commit":
            return obj["sha"]
        obj = api(f"repos/{owner_repo}/git/tags/{obj['sha']}")["object"]
    raise RuntimeError("tag did not resolve to a commit")


def pin_text(text: str, api=gh_api, cache: dict | None = None) -> tuple[str, list[str]]:
    cache = {} if cache is None else cache
    changes, out = [], []
    for line in text.splitlines(keepends=True):
        m = USES.match(line.rstrip("\n"))
        if not m or m["repo"].startswith("./") or SHA.match(m["ref"]):
            out.append(line)
            continue
        key = (m["repo"], m["ref"])
        if key not in cache:
            cache[key] = resolve(*key, api=api)
        sha = cache[key]
        rest = re.sub(r"\s*#.*$", "", m["rest"])          # drop the old "# VERIFY ..." note, keep the tag readable
        out.append(f"{m['pre']}{m['repo']}@{sha}{rest}  # {m['ref']}\n")
        changes.append(f"{m['repo']}@{m['ref']} -> {sha[:10]}")
    return "".join(out), changes


def main(argv: list[str]) -> int:
    write = "--write" in argv
    root = Path(argv[argv.index("--dir") + 1]) if "--dir" in argv else Path(".github/workflows")
    cache: dict = {}
    total = 0
    for f in sorted(root.glob("*.yml")):
        new, changes = pin_text(f.read_text(encoding="utf-8"), cache=cache)
        for c in changes:
            print(f"{f.name}: {c}")
        total += len(changes)
        if write and changes:
            f.write_text(new, encoding="utf-8")
    print(f"{total} reference(s) {'pinned' if write else 'would be pinned (use --write)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
