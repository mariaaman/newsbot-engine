#!/usr/bin/env python3
"""One-click setup of the Telegram approve Worker (run by .github/workflows/setup-approve.yml after `wrangler deploy`).

  1. find your workers.dev address (Cloudflare API)           4. register the Telegram webhook with that secret
  2. store the Worker's secrets (wrangler secret bulk)        5. switch on approve_buttons + edit_url in the brain config
  3. check the Worker answers                                 6. tell you in Telegram

The log is public: it prints status words only, never the Worker address, a token or any secret. Secrets reach the Worker
through a temporary 0600 file that is deleted at once. Re-running is safe (it rotates the webhook secret and repeats the rest).
Usage: python scripts/setup_approve_worker.py [--check]"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

WORKER = "newsbot-approve"
NEEDED = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "APPROVE_GH_TOKEN",
          "BRAIN_REPO_TOKEN", "BRAIN_REPO")


def http(method: str, url: str, headers: dict | None = None, body: dict | None = None, timeout: int = 20) -> tuple[int, object]:
    """(status, parsed JSON or text). Never raises, never logs the URL (it may hold a token)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"User-Agent": "newsbot-setup", **(headers or {}), **({"Content-Type": "application/json"} if data else {})}
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, code = r.read().decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode("utf-8", "replace"), e.code
    except Exception:                                                  # network down, timeout, TLS ...
        return 0, ""
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw


def set_notify_keys(text: str, edit_url: str) -> str:
    """Turns on notify.telegram.approve_buttons and sets edit_url in the brain's newsbot.yaml, touching nothing else
    (comments, order and spacing stay). Existing keys are updated in place, missing ones are added to the telegram block."""
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]+)?", edit_url):
        raise ValueError("edit_url must look like https://name.workers.dev")
    lines = text.split("\n")
    try:
        n = next(i for i, l in enumerate(lines) if re.match(r"^notify:\s*(#.*)?$", l))
        t = next(i for i in range(n + 1, len(lines)) if re.match(r"^  telegram:\s*(#.*)?$", lines[i]))
    except StopIteration:
        raise ValueError("notify.telegram block not found in newsbot.yaml") from None
    last, i = t, t + 1
    while i < len(lines):
        if lines[i].strip() == "":
            i += 1
        elif re.match(r"^ {4,}\S", lines[i]):
            last, i = i, i + 1
        else:
            break
    if last == t:
        raise ValueError("notify.telegram block is empty or not a mapping")
    for key, val in (("approve_buttons", "true"), ("edit_url", json.dumps(edit_url))):
        for j in range(t + 1, last + 1):
            m = re.match(rf'^( {{4}}{key}:[ \t]*)("[^"]*"|\'[^\']*\'|[^#\s][^#]*?)?([ \t]*#.*)?$', lines[j])
            if m:
                lines[j] = f"{m.group(1)}{val}{m.group(3) or ''}"
                break
        else:
            lines.insert(last + 1, f"    {key}: {val}")
            last += 1
    return "\n".join(lines)


def run_setup(env: dict, root: Path = Path("."), http=http, run=subprocess.run, sleep=time.sleep) -> list[tuple[str, str]]:
    """Returns [(step, 'ok' | 'fail:<reason>')]; stops at the first failure."""
    out: list[tuple[str, str]] = []

    def step(name: str, ok: bool, why: str = "") -> bool:
        out.append((name, "ok" if ok else f"fail:{why or 'error'}"))
        return ok

    missing = [k for k in NEEDED if not env.get(k)]
    if not step("inputs", not missing, "missing " + ",".join(missing)):
        return out
    acc, cf = env["CLOUDFLARE_ACCOUNT_ID"], {"Authorization": f"Bearer {env['CLOUDFLARE_API_TOKEN']}"}
    brain, branch = env["BRAIN_REPO"], env.get("BRAIN_BRANCH") or "main"
    engine = env.get("GITHUB_REPOSITORY", "")
    if not step("repos", bool(re.fullmatch(r"[\w.-]+/[\w.-]+", brain)) and bool(re.fullmatch(r"[\w.-]+/[\w.-]+", engine)), "repo names"):
        return out

    # 1. address
    code, js = http("GET", f"https://api.cloudflare.com/client/v4/accounts/{acc}/workers/subdomain", cf)
    sub = ((js or {}).get("result") or {}).get("subdomain") if isinstance(js, dict) else None
    why = (f"cloudflare http {code} (the token needs Workers Scripts: Edit)" if code != 200
           else "no workers.dev subdomain yet: open Workers & Pages once in the Cloudflare dashboard")
    if not step("workers_subdomain", bool(sub) and re.fullmatch(r"[a-z0-9-]+", str(sub)) is not None, why):
        return out
    base = f"https://{WORKER}.{sub}.workers.dev"
    hook_secret = secrets.token_urlsafe(32)

    # 2. secrets (a temp file, 0600, removed at once)
    payload = {"GH_TOKEN": env["APPROVE_GH_TOKEN"], "TELEGRAM_BOT_TOKEN": env["TELEGRAM_BOT_TOKEN"],
               "TELEGRAM_CHAT_ID": env["TELEGRAM_CHAT_ID"], "WEBHOOK_SECRET": hook_secret}
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.chmod(path, 0o600)
        r = run(["npx", "--yes", "wrangler@4", "secret", "bulk", path, "--name", WORKER], cwd=str(root / "worker"),
                env={**os.environ, **env}, capture_output=True, text=True, timeout=300)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if not step("worker_secrets", r.returncode == 0, f"wrangler exit {r.returncode}"):
        return out

    # 3. the Worker answers
    ok = False
    for _ in range(8):
        code, body = http("GET", base + "/")
        if code == 200 and str(body).strip() == "ok":
            ok = True
            break
        sleep(5)
    if not step("worker_answers", ok, "no answer from the Worker (a new workers.dev address can take a minute)"):
        return out

    # 4. webhook
    api = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}"
    code, js = http("POST", f"{api}/setWebhook", body={"url": base + "/telegram", "secret_token": hook_secret,
                                                       "allowed_updates": ["callback_query"], "max_connections": 5})
    if not step("telegram_webhook", code == 200 and isinstance(js, dict) and js.get("ok") is True, f"telegram http {code}"):
        return out

    # 5. brain config
    gh = {"Authorization": f"Bearer {env['BRAIN_REPO_TOKEN']}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    url = f"https://api.github.com/repos/{brain}/contents/config/newsbot.yaml"
    code, js = http("GET", f"{url}?ref={branch}", gh)
    if not step("brain_config_read", code == 200 and isinstance(js, dict) and "content" in js, f"github http {code}"):
        return out
    old = base64.b64decode(js["content"]).decode("utf-8")
    try:
        new = set_notify_keys(old, base)
    except ValueError as e:
        step("brain_config_edit", False, str(e))
        return out
    if new == old:
        step("brain_config_edit", True)
    else:
        code, _ = http("PUT", url, gh, {"message": "chore: switch on Telegram approve buttons", "branch": branch, "sha": js["sha"],
                                        "content": base64.b64encode(new.encode("utf-8")).decode("ascii")})
        if not step("brain_config_edit", code in (200, 201), f"github http {code} (BRAIN_REPO_TOKEN needs Contents: write)"):
            return out

    # 6. tell the owner (private chat, the only place the address is shown)
    code, _ = http("POST", f"{api}/sendMessage", body={"chat_id": env["TELEGRAM_CHAT_ID"], "text":
                   "✅ টেলিগ্রাম অনুমোদন চালু হয়েছে। এখন থেকে সংবেদনশীল খসড়ায় অনুমোদন/বাতিল বাটন ও সম্পাদনার লিংক আসবে।\n"
                   f"Worker: {base}"})
    step("telegram_message", code == 200, f"telegram http {code}")
    return out


def main(argv: list[str]) -> int:
    env = dict(os.environ)
    if "--check" in argv:
        missing = [k for k in NEEDED if not env.get(k)]
        print("inputs=" + ("ok" if not missing else "missing " + ",".join(missing)))
        return 1 if missing else 0
    results = run_setup(env, Path(__file__).resolve().parent.parent)
    for name, status in results:
        print(f"{name}={status}")
    return 0 if results and all(s == "ok" for _, s in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
