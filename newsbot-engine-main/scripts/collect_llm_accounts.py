#!/usr/bin/env python3
"""Prepare the Cloudflare LLM accounts for the steps that call the LLM.

The dashboard keeps the accounts and pushes them as ONE ordinary GitHub secret, CFLLM_ACCOUNTS, whose value is
{"<LABEL>": {"a": "<account id>", "t": "<token>"}, ...}. This step receives it in the environment variable CFLLM_ACCOUNTS
(`${{ secrets.CFLLM_ACCOUNTS }}`, the same pattern as every other secret; the whole secrets context is never read), checks it, writes
an owner-only file and exports only that file's PATH as NEWSBOT_CF_ACCOUNTS_FILE for the later steps. The engine reads the file.
Prints counts and labels only, never a value. No secret yet is fine: the engine then falls back to the deploy account.

usage: collect_llm_accounts.py            (inside GitHub Actions; reads CFLLM_ACCOUNTS, GITHUB_ENV, RUNNER_TEMP)"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

LABEL = re.compile(r"^[A-Z0-9_]{1,32}$")
ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")


def collect(raw) -> tuple[list[dict], list[str]]:
    """raw: the parsed secret (a dict label -> {a, t}). -> (accounts, problems). A problem names the label only."""
    accounts, problems = [], []
    items = raw.items() if isinstance(raw, dict) else []
    for label, v in sorted(items):
        try:
            acct, tok = v["a"], v["t"]
        except (KeyError, TypeError):
            problems.append(f"{label}: not the expected shape")
            continue
        if not LABEL.match(str(label)) or not isinstance(acct, str) or not ACCOUNT_ID.match(acct) or not isinstance(tok, str) or not tok.strip():
            problems.append(f"{label}: malformed")
            continue
        accounts.append({"label": label, "account_id": acct, "token": tok.strip()})
    return accounts, problems


def main(env=os.environ) -> int:
    text = (env.get("CFLLM_ACCOUNTS") or "").strip()
    try:
        raw = json.loads(text) if text else {}
    except ValueError:
        print("collect: CFLLM_ACCOUNTS is not JSON")
        return 1
    accounts, problems = collect(raw)
    out_dir = Path(env.get("RUNNER_TEMP") or tempfile.gettempdir())
    path = out_dir / "newsbot_llm_accounts.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)    # owner-only
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(accounts, f)
    gh_env = env.get("GITHUB_ENV")
    if gh_env:
        with open(gh_env, "a", encoding="utf-8") as f:
            f.write(f"NEWSBOT_CF_ACCOUNTS_FILE={path}\n")
    print(f"collect: llm accounts={len(accounts)} labels={','.join(a['label'] for a in accounts) or '-'}")
    for p in problems:
        print(f"collect: skipped {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
