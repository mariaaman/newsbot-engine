"""scripts/setup_dashboard.py with every network and wrangler call faked."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("setup_dashboard", ROOT / "scripts" / "setup_dashboard.py")
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)

KV = "a" * 32
ENV = {"CLOUDFLARE_API_TOKEN": "cf-tok", "CLOUDFLARE_ACCOUNT_ID": "acc1", "DASHBOARD_PASSWORD": "a long enough pass", "DASHBOARD_GH_TOKEN": "gh-dash",
       "BRAIN_REPO": "me/newsbot-brain", "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"}


def project():
    d = Path(tempfile.mkdtemp())
    (d / "dashboard").mkdir()
    shutil.copy(ROOT / "dashboard" / "wrangler.toml", d / "dashboard" / "wrangler.toml")
    return d


def test_input_checks():
    assert sd.check_inputs(ENV) == []
    assert "missing DASHBOARD_GH_TOKEN" in sd.check_inputs({k: v for k, v in ENV.items() if k != "DASHBOARD_GH_TOKEN"})
    assert any("14 characters" in m for m in sd.check_inputs({**ENV, "DASHBOARD_PASSWORD": "short"}))
    assert any("spaces" in m for m in sd.check_inputs({**ENV, "DASHBOARD_PASSWORD": " leading space pass"}))
    assert any("owner/name" in m for m in sd.check_inputs({**ENV, "BRAIN_REPO": "nope"}))


def test_prepare_creates_or_finds_the_kv_namespace_and_fills_the_toml():
    calls = []

    def http(method, url, headers=None, body=None, timeout=20):
        calls.append((method, url.split("?")[0], body))
        if method == "GET":
            return 200, {"result": []}
        return 200, {"result": {"id": KV, "title": sd.KV_TITLE}}
    root = project()
    assert sd.prepare(ENV, root, http=http) == [("inputs", "ok"), ("kv_namespace", "ok")]
    assert f'id = "{KV}"' in (root / "dashboard" / "wrangler.toml").read_text() and "__KV_ID__" not in (root / "dashboard" / "wrangler.toml").read_text()
    assert [c[0] for c in calls] == ["GET", "POST"] and calls[1][2] == {"title": sd.KV_TITLE}
    root = project()                                                                    # an existing namespace is reused
    res = sd.prepare(ENV, root, http=lambda m, u, h=None, b=None, t=20: (200, {"result": [{"title": sd.KV_TITLE, "id": KV}]}))
    assert res[-1] == ("kv_namespace", "ok")
    res = sd.prepare(ENV, project(), http=lambda *a, **k: (403, {}))
    assert res[-1][1].startswith("fail:cloudflare http 403") and "Workers KV Storage" in res[-1][1]
    assert sd.prepare({**ENV, "DASHBOARD_PASSWORD": "x"}, project())[0][1].startswith("fail:")


def test_secrets_step_stores_three_secrets_privately_and_checks_the_worker():
    seen = {}

    def http(method, url, headers=None, body=None, timeout=20):
        if "workers/subdomain" in url:
            return 200, {"result": {"subdomain": "me"}}
        if url.endswith("/api/session"):
            return 200, {"authenticated": False, "totp": False}
        if "sendMessage" in url:
            seen["msg"] = body
            return 200, {}
        raise AssertionError(url)

    def run(cmd, cwd=None, env=None, capture_output=True, text=True, timeout=0):
        path = cmd[cmd.index("bulk") + 1]
        seen["cmd"], seen["payload"], seen["mode"], seen["path"] = cmd, json.loads(Path(path).read_text()), stat.S_IMODE(os.stat(path).st_mode), path
        return SimpleNamespace(returncode=0)
    res = sd.secrets_step(ENV, project(), http=http, run=run, sleep=lambda s: None)
    assert [s for _, s in res] == ["ok"] * len(res) and res[-1][0] == "telegram_message"
    p = seen["payload"]
    assert set(p) == {"DASHBOARD_PASSWORD", "GH_TOKEN", "SESSION_SECRET"} and p["GH_TOKEN"] == "gh-dash" and len(p["SESSION_SECRET"]) >= 48
    assert seen["mode"] == 0o600 and not os.path.exists(seen["path"]) and seen["cmd"][-2:] == ["--name", "newsbot-dashboard"]
    assert "workers.dev" in seen["msg"]["text"] and "a long enough pass" not in seen["msg"]["text"]
    assert not any(secret in " ".join(f"{n}={s}" for n, s in res) for secret in ("cf-tok", "gh-dash", "123:abc", "workers.dev"))


def test_failures_stop_early():
    ok = lambda m, u, h=None, b=None, t=20: (200, {"result": {"subdomain": "me"}})
    assert sd.secrets_step(ENV, project(), http=lambda *a, **k: (403, {}), run=None)[-1][1].startswith("fail:cloudflare http 403")
    assert "open Workers & Pages" in sd.secrets_step(ENV, project(), http=lambda *a, **k: (200, {"result": {}}), run=None)[-1][1]
    res = sd.secrets_step(ENV, project(), http=ok, run=lambda *a, **k: SimpleNamespace(returncode=1), sleep=lambda s: None)
    assert res[-1] == ("worker_secrets", "fail:wrangler exit 1")
    res = sd.secrets_step(ENV, project(), http=lambda m, u, h=None, b=None, t=20: ok(m, u) if "subdomain" in u else (404, ""), run=lambda *a, **k: SimpleNamespace(returncode=0), sleep=lambda s: None)
    assert res[-1][0] == "worker_answers" and res[-1][1].startswith("fail:")
