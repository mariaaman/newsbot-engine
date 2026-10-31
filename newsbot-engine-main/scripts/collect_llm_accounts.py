#!/usr/bin/env python3
"""Collect every Cloudflare LLM account the dashboard stored, for the steps that call the LLM.

The dashboard saves one GitHub secret per account, named CFLLM_<LABEL>, whose value is {"a": "<account id>", "t": "<token>"}.
GitHub cannot list secrets inside a workflow, so ONE small step receives the secrets context as JSON in the environment
variable ALL_SECRETS_JSON, keeps only CFLLM_*, writes them to a private file and exports the file's PATH (not the values)
as NEWSBOT_CF_ACCOUNTS_FILE. The engine reads that file. Prints counts and labels only, never a value.

usage: collect_llm_accounts.py            (inside GitHub Actions; reads ALL_SECRETS_JSON, GITHUB_ENV, RUNNER_TEMP)"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

PREFIX = "CFLLM_"
LABEL = re.compile(r"^[A-Z0-9_]{1,32}$")
ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")


def collect(secrets: dict) -> tuple[list[dict], list[str]]:
    """-> (accounts, problems). A problem names the secret only."""
    accounts, problems = [], []
    for name in sorted(secrets):
        if not name.startswith(PREFIX):
            continue
        label = name[len(PREFIX):]
        try:
            v = json.loads(secrets[name])
            acct, tok = v["a"], v["t"]
        except (ValueError, KeyError, TypeError):
            problems.append(f"{name}: not the expected JSON")
            continue
        if not LABEL.match(label) or not isinstance(acct, str) or not ACCOUNT_ID.match(acct) or not isinstance(tok, str) or not tok.strip():
            problems.append(f"{name}: malformed")
            continue
        accounts.append({"label": label, "account_id": acct, "token": tok.strip()})
    return accounts, problems


def main(env=os.environ) -> int:
    try:
        secrets = json.loads(env.get("ALL_SECRETS_JSON") or "{}")
    except ValueError:
        print("collect: ALL_SECRETS_JSON is not JSON")
        return 1
    accounts, problems = collect(secrets if isinstance(secrets, dict) else {})
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
