#!/usr/bin/env python3
"""Turn on Cloudflare Web Analytics (free, cookieless) for every separate site and save each site's token to site/data/analytics.json,
so the build puts the visitor-counting snippet into the pages. Best effort: any problem (for example an API token without the
'Account Settings: Write' permission) only means "no counter yet"; the run never fails because of it. Prints language codes and
ok/skip only. Needs CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.cloudflare.com/client/v4"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_site_config", ROOT / "scripts" / "gen_site_config.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _call(method: str, url: str, token: str, body: dict | None = None) -> dict:
    import urllib.request
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:           # noqa: S310 - fixed https API host
        return json.loads(r.read().decode("utf-8"))


def ensure(hosts: dict[str, str], account: str, token: str, call=_call) -> dict[str, str]:
    """host per language -> {lang: site token}. Reuses a site that already exists for the host, else creates one."""
    existing: dict[str, str] = {}
    page = 1
    while page <= 10:
        r = call("GET", f"{API}/accounts/{account}/rum/site_info/list?per_page=100&page={page}", token)
        rows = r.get("result") or []
        for s in rows:
            h = (s.get("ruleset") or {}).get("zone_name") or s.get("host") or ""
            if h and s.get("site_token"):
                existing[h] = s["site_token"]
        if len(rows) < 100:
            break
        page += 1
    out = {}
    for lang, host in hosts.items():
        tok = existing.get(host)
        if not tok:
            r = call("POST", f"{API}/accounts/{account}/rum/site_info", token, {"host": host, "auto_install": False})
            tok = ((r.get("result") or {}).get("site_token")) or ""
        if tok:
            out[lang] = tok
    return out


def main(argv=None, call=_call) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", required=True)
    ap.add_argument("--site", default=str(ROOT / "site"))
    a = ap.parse_args(argv)
    data = _gen().load_site_cfg(Path(a.brain))
    out_path = Path(a.site) / "data" / "analytics.json"
    token, account = os.environ.get("CLOUDFLARE_API_TOKEN", ""), os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not data["sites"] or not token or not account:
        print("analytics skip (no separate sites or no Cloudflare credentials)")
        return 0
    hosts = {l: urlparse(t["base_url"]).hostname or "" for l, t in data["sites"].items() if not t.get("analytics_token")}
    try:
        got = ensure(hosts, account, token, call)
    except Exception as e:                                       # noqa: BLE001 - optional feature, never fail the run
        print(f"analytics skip error={type(e).__name__} (the API token needs 'Account Settings: Write' for automatic visitor counting)")
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(got), encoding="utf-8")
    for l in hosts:
        print(f"analytics lang={l} status={'ok' if l in got else 'skip'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
