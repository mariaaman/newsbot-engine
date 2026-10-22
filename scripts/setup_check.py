#!/usr/bin/env python3
"""One-click setup check for setup.yml. Prints ONLY lines like 'OK name' / 'FAIL name' / 'SKIP name' (never values,
never response bodies: the engine repo is public). Exit 1 if any required check fails.

Checks: required secrets and variables are present, the brain token reaches the brain repo, the Cloudflare token is
valid, the Pages project exists (created when missing), Workers AI embeddings answer, and the brain repo has the
files the engine needs. Optional: the Telegram bot token and chat (a test message is sent). Groq/Gemini model IDs are checked by `python -m newsbot health` in the same workflow."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

REQUIRED_SECRETS = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "GROQ_API_KEY", "BRAIN_REPO_TOKEN")
OPTIONAL_SECRETS = ("GEMINI_API_KEY", "OPENROUTER_API_KEY")
REQUIRED_VARS = ("PAGES_PROJECT", "BRAIN_REPO")
BRAIN_FILES = ("config/newsbot.yaml", "config/llm.yaml", "config/sources.bn.yaml", "config/sources.en.yaml",
               "prompts/extract.md", "prompts/verify.md", "prompts/write_bn.md", "prompts/write_en.md",
               "prompts/update.md", "prompts/qa_check.md")
BRAIN_DIRS = ("content/bn", "content/en", "pending", "state")
CF = "https://api.cloudflare.com/client/v4"       # VERIFY
GH = "https://api.github.com"
Result = tuple[str, str]                            # (OK|FAIL|SKIP, name)


def default_http(method: str, url: str, headers: dict, body: dict | None = None) -> tuple[int, dict]:
    try:
        r = requests.request(method, url, headers=headers, json=body, timeout=20)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}
    except requests.RequestException:
        return 0, {}


def check_env(env) -> list[Result]:
    out = [("OK" if env.get(n) else "FAIL", f"secret {n}") for n in REQUIRED_SECRETS]
    out += [("OK" if env.get(n) else "SKIP", f"secret {n} (optional)") for n in OPTIONAL_SECRETS]
    out += [("OK" if env.get(n) else "FAIL", f"variable {n}") for n in REQUIRED_VARS]
    mode = env.get("PUBLISH_MODE") or "review"
    out.append(("OK" if mode in ("review", "auto") else "FAIL", "variable PUBLISH_MODE is review or auto"))
    return out


def check_brain_repo(env, http) -> list[Result]:
    repo, tok = env.get("BRAIN_REPO", ""), env.get("BRAIN_REPO_TOKEN", "")
    if not repo or not tok:
        return [("FAIL", "brain repo reachable with BRAIN_REPO_TOKEN")]
    code, data = http("GET", f"{GH}/repos/{repo}", {"Authorization": f"Bearer {tok}",
                                                       "Accept": "application/vnd.github+json"})
    out = [("OK" if code == 200 else "FAIL", "brain repo reachable with BRAIN_REPO_TOKEN")]
    if code == 200:
        out.append(("OK" if (data.get("permissions") or {}).get("push") else "FAIL", "brain token can push"))
        out.append(("OK" if data.get("private") else "FAIL", "brain repo is private"))
    return out


def check_cloudflare(env, http) -> list[Result]:
    tok, acct, proj = env.get("CLOUDFLARE_API_TOKEN", ""), env.get("CLOUDFLARE_ACCOUNT_ID", ""), env.get("PAGES_PROJECT", "")
    if not (tok and acct):
        return [("FAIL", "cloudflare token valid")]
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    code, data = http("GET", f"{CF}/accounts/{acct}/tokens/verify", h)          # VERIFY: account-owned token endpoint
    if code != 200:
        code, data = http("GET", f"{CF}/user/tokens/verify", h)                 # VERIFY: user token endpoint
    out = [("OK" if code == 200 and data.get("success") else "FAIL", "cloudflare token valid")]
    if proj:
        code, _ = http("GET", f"{CF}/accounts/{acct}/pages/projects/{proj}", h)
        if code == 200:
            out.append(("OK", "pages project exists"))
        elif code == 404:
            code, _ = http("POST", f"{CF}/accounts/{acct}/pages/projects", h,
                           {"name": proj, "production_branch": "main"})
            out.append(("OK" if code == 200 else "FAIL", "pages project created"))
        else:
            out.append(("FAIL", "pages project check"))
    emb = embedding_model(Path(env.get("NEWSBOT_BRAIN_DIR", "brain")))
    code, _ = http("POST", f"{CF}/accounts/{acct}/ai/run/{emb}", h, {"text": ["test"]})   # VERIFY path and body
    out.append(("OK" if code == 200 else "FAIL", "workers ai embeddings"))
    return out


def embedding_model(brain: Path) -> str:
    try:
        from newsbot.llm import load_llm_settings
        return load_llm_settings(brain)["embedding"]["model"]
    except Exception:                                # settings unreadable: fall back to the documented default
        return "@cf/baai/bge-m3"


def check_telegram(env, http) -> list[Result]:
    """Optional. With both secrets set: the bot token must be valid and a test message must reach the chat."""
    tok, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    if not tok and not chat:
        return [("SKIP", "telegram (optional, secrets TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID not set)")]
    if not tok or not chat:
        return [("FAIL", "telegram needs both secrets TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")]
    base = f"https://api.telegram.org/bot{tok}"                # VERIFY; never printed
    code, data = http("POST", f"{base}/getMe", {}, None)
    out = [("OK" if code == 200 and data.get("ok") else "FAIL", "telegram bot token is valid")]
    if out[0][0] == "OK":
        code, data = http("POST", f"{base}/sendMessage", {}, {
            "chat_id": chat, "text": "\u2705 NewsBot: Telegram \u09b8\u0982\u09af\u09c1\u0995\u09cd\u09a4 \u09b9\u09af\u09bc\u09c7\u099b\u09c7 / connected."})
        out.append(("OK" if code == 200 and data.get("ok") else "FAIL",
                    "telegram test message reached the chat (send /start to the bot first and check TELEGRAM_CHAT_ID)"))
    return out


def check_brain_structure(brain: Path) -> list[Result]:
    out = [("OK" if (brain / f).is_file() else "FAIL", f"brain file {f}") for f in BRAIN_FILES]
    out += [("OK" if (brain / d).is_dir() else "FAIL", f"brain dir {d}/") for d in BRAIN_DIRS]
    return out


def run_all(env, brain: Path, http=default_http) -> list[Result]:
    return (check_env(env) + check_brain_repo(env, http) + check_cloudflare(env, http) + check_telegram(env, http)
            + check_brain_structure(brain))


def main(argv=None) -> int:
    brain = Path((argv or sys.argv[1:] or ["brain"])[0])
    results = run_all(os.environ, brain)
    for status, name in results:
        print(f"{status} {name}")
    bad = sum(s == "FAIL" for s, _ in results)
    print(f"setup-check failed={bad}" if bad else "setup-check all-required-OK")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
