#!/usr/bin/env python3
"""One-click setup of the NewsBot dashboard Worker (run by .github/workflows/setup-dashboard.yml).

  prepare : find or create the KV namespace and write its id into dashboard/wrangler.toml (only in the runner's copy)
  secrets : store the Worker's secrets (password, session secret, GitHub token) with wrangler, then check the Worker answers
  note    : every run stores a NEW session secret. That signs everybody out and also resets two-step login (its secret in KV was
            encrypted with the old session secret and can no longer be read), which is the recovery path for a lost authenticator.

The log is public: status words only, never the address, a token or a secret. Secrets reach the Worker through a 0600 temp file
that is deleted at once. Re-running is safe.
Usage: python scripts/setup_dashboard.py prepare|secrets|check"""
from __future__ import annotations

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

WORKER = "newsbot-dashboard"
KV_TITLE = "newsbot-dashboard-kv"
NEEDED = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "DASHBOARD_PASSWORD", "DASHBOARD_GH_TOKEN", "BRAIN_REPO")


def http(method: str, url: str, headers: dict | None = None, body: dict | None = None, timeout: int = 20) -> tuple[int, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "newsbot-setup", **(headers or {}), **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, code = r.read().decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode("utf-8", "replace"), e.code
    except Exception:
        return 0, ""
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw


def check_inputs(env: dict) -> list[str]:
    bad = [f"missing {k}" for k in NEEDED if not env.get(k)]
    if env.get("DASHBOARD_PASSWORD") and len(env["DASHBOARD_PASSWORD"]) < 14:
        bad.append("DASHBOARD_PASSWORD must be at least 14 characters")
    if env.get("DASHBOARD_PASSWORD") and (env["DASHBOARD_PASSWORD"] != env["DASHBOARD_PASSWORD"].strip() or "\n" in env["DASHBOARD_PASSWORD"]):
        bad.append("DASHBOARD_PASSWORD has spaces at the ends or a line break")
    if env.get("BRAIN_REPO") and not re.fullmatch(r"[\w.-]+/[\w.-]+", env["BRAIN_REPO"]):
        bad.append("BRAIN_REPO should look like owner/name")
    return bad


def prepare(env: dict, root: Path, http=http) -> list[tuple[str, str]]:
    """Creates the KV namespace if needed and fills wrangler.toml (the runner's copy only)."""
    out: list[tuple[str, str]] = []
    bad = check_inputs(env)
    out.append(("inputs", "ok" if not bad else "fail:" + "; ".join(bad)))
    if bad:
        return out
    acc, h = env["CLOUDFLARE_ACCOUNT_ID"], {"Authorization": f"Bearer {env['CLOUDFLARE_API_TOKEN']}"}
    base = f"https://api.cloudflare.com/client/v4/accounts/{acc}/storage/kv/namespaces"
    code, js = http("GET", base + "?per_page=100", h)
    if code != 200 or not isinstance(js, dict):
        out.append(("kv_namespace", f"fail:cloudflare http {code} (the token needs Workers KV Storage: Edit)"))
        return out
    found = next((n for n in js.get("result", []) if n.get("title") == KV_TITLE), None)
    if not found:
        code, js = http("POST", base, h, {"title": KV_TITLE})
        found = js.get("result") if code in (200, 201) and isinstance(js, dict) else None
    if not found or not re.fullmatch(r"[0-9a-f]{32}", str(found.get("id", ""))):
        out.append(("kv_namespace", f"fail:could not create the KV namespace (http {code})"))
        return out
    toml = root / "dashboard" / "wrangler.toml"
    text = toml.read_text(encoding="utf-8")
    if "__KV_ID__" in text:
        toml.write_text(text.replace("__KV_ID__", found["id"]), encoding="utf-8")
    elif found["id"] not in text:
        out.append(("kv_namespace", "fail:wrangler.toml has a different KV id; remove it or the namespace"))
        return out
    out.append(("kv_namespace", "ok"))
    return out


def secrets_step(env: dict, root: Path, http=http, run=subprocess.run, sleep=time.sleep) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    bad = check_inputs(env)
    if bad:
        return [("inputs", "fail:" + "; ".join(bad))]
    acc, cf = env["CLOUDFLARE_ACCOUNT_ID"], {"Authorization": f"Bearer {env['CLOUDFLARE_API_TOKEN']}"}
    code, js = http("GET", f"https://api.cloudflare.com/client/v4/accounts/{acc}/workers/subdomain", cf)
    sub = ((js or {}).get("result") or {}).get("subdomain") if isinstance(js, dict) else None
    why = (f"cloudflare http {code} (the token needs Workers Scripts: Edit)" if code != 200 else "no workers.dev subdomain yet: open Workers & Pages once in the Cloudflare dashboard")
    if not (sub and re.fullmatch(r"[a-z0-9-]+", str(sub))):
        return [("workers_subdomain", "fail:" + why)]
    out.append(("workers_subdomain", "ok"))
    payload = {"DASHBOARD_PASSWORD": env["DASHBOARD_PASSWORD"], "GH_TOKEN": env["DASHBOARD_GH_TOKEN"], "SESSION_SECRET": secrets.token_urlsafe(48)}
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.chmod(path, 0o600)
        r = run(["npx", "--yes", "wrangler@4", "secret", "bulk", path, "--name", WORKER], cwd=str(root / "dashboard"), env={**os.environ, **env}, capture_output=True, text=True, timeout=300)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    out.append(("worker_secrets", "ok" if r.returncode == 0 else f"fail:wrangler exit {r.returncode}"))
    if r.returncode != 0:
        return out
    base = f"https://{WORKER}.{sub}.workers.dev"
    ok = False
    for _ in range(8):
        code, body = http("GET", base + "/api/session")
        if code == 200 and isinstance(body, dict) and body.get("authenticated") is False:
            ok = True
            break
        sleep(5)
    out.append(("worker_answers", "ok" if ok else "fail:no answer from the Worker yet (a new workers.dev address can take a minute)"))
    if not ok:
        return out
    tg = http("POST", f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage", None,
              {"chat_id": env["TELEGRAM_CHAT_ID"], "text": f"✅ ড্যাশবোর্ড চালু হয়েছে:\n{base}\n(পাসওয়ার্ড: GitHub সিক্রেট DASHBOARD_PASSWORD)"}) if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID") else (None, None)
    out.append(("telegram_message", "skipped" if tg[0] is None else ("ok" if tg[0] == 200 else f"fail:telegram http {tg[0]}")))
    return out


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    step = argv[0] if argv else "check"
    if step == "check":
        bad = check_inputs(env)
        print("inputs=" + ("ok" if not bad else "fail:" + "; ".join(bad)))
        return 1 if bad else 0
    results = {"prepare": prepare, "secrets": secrets_step}.get(step)
    if not results:
        print("unknown step")
        return 2
    res = results(env, root)
    for name, status in res:
        print(f"{name}={status}")
    return 0 if res and all(s in ("ok", "skipped") for _, s in res) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
