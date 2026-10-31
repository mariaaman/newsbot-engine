"""scripts/setup_approve_worker.py: brain-config edit and the one-click setup flow (all network and wrangler calls faked)."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("setup_approve_worker", ROOT / "scripts" / "setup_approve_worker.py")
sw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sw)

URL = "https://newsbot-approve.me.workers.dev"
TEMPLATE_YAML = (ROOT / "brain-template" / "config" / "newsbot.yaml").read_text(encoding="utf-8")
OLD_YAML = TEMPLATE_YAML.replace("    approve_buttons: false    # true after the approve Worker is live (docs/TELEGRAM_APPROVE.md): Approve / Reject buttons on sensitive drafts\n", "") \
    .replace('    edit_url: ""             # https://newsbot-approve.<you>.workers.dev : pending drafts get a private link to read, edit, approve or reject\n', "")
ENV = {"CLOUDFLARE_API_TOKEN": "cf-tok", "CLOUDFLARE_ACCOUNT_ID": "acc1", "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42",
       "APPROVE_GH_TOKEN": "gh-approve", "BRAIN_REPO_TOKEN": "gh-brain", "BRAIN_REPO": "me/newsbot-brain", "GITHUB_REPOSITORY": "me/newsbot-engine"}


def tg(text: str) -> dict:
    return yaml.safe_load(text)["notify"]["telegram"]


def test_edit_adds_missing_keys_without_touching_anything_else():
    assert "approve_buttons" not in OLD_YAML and "edit_url" not in OLD_YAML
    new = sw.set_notify_keys(OLD_YAML, URL)
    t = tg(new)
    assert t["approve_buttons"] is True and t["edit_url"] == URL
    assert [l for l in new.split("\n") if l not in OLD_YAML.split("\n")] == ['    approve_buttons: true', f'    edit_url: "{URL}"']
    assert yaml.safe_load(new)["site"] == yaml.safe_load(OLD_YAML)["site"]


def test_edit_updates_existing_keys_in_place_keeps_comments_and_is_idempotent():
    new = sw.set_notify_keys(TEMPLATE_YAML, URL)
    assert tg(new)["approve_buttons"] is True and tg(new)["edit_url"] == URL
    assert "# true after the approve Worker is live" in new and new.count("approve_buttons:") == 1 and new.count("edit_url:") == 1
    assert sw.set_notify_keys(new, URL) == new
    other = sw.set_notify_keys(new, "https://other.workers.dev")
    assert tg(other)["edit_url"] == "https://other.workers.dev"


def test_edit_refuses_bad_input_and_odd_files():
    for bad in ("http://x.dev", "https://x.dev/path", "javascript:1", "https://a b"):
        try:
            sw.set_notify_keys(OLD_YAML, bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    for text in ("site:\n  name: x\n", "notify:\n  telegram: {}\n", "notify:\n  other:\n    a: 1\n"):
        try:
            sw.set_notify_keys(text, URL)
            raise AssertionError(text)
        except ValueError:
            pass


class Net:
    """Fake of the three HTTP APIs and of wrangler."""
    def __init__(self, cf=(200, {"result": {"subdomain": "me"}}), worker=(200, "ok"), hook=(200, {"ok": True}), put=200, wrangler_rc=0, yaml_text=OLD_YAML):
        self.cf, self.worker, self.hook, self.put, self.rc = cf, worker, hook, put, wrangler_rc
        self.calls, self.secrets_file, self.secrets_mode, self.wrangler_cmd, self.committed = [], None, None, None, None
        self.yaml_text = yaml_text

    def http(self, method, url, headers=None, body=None, timeout=20):
        self.calls.append((method, url.split("?")[0], body))
        if "workers/subdomain" in url: return self.cf
        if url == URL + "/": return self.worker
        if url.endswith("/setWebhook"): return self.hook
        if url.endswith("/sendMessage"): return 200, {"ok": True}
        if "api.github.com" in url and method == "GET":
            return 200, {"content": base64.b64encode(self.yaml_text.encode()).decode(), "sha": "s1"}
        if "api.github.com" in url and method == "PUT":
            if self.put in (200, 201): self.committed = base64.b64decode(body["content"]).decode()
            return self.put, {}
        raise AssertionError(url)

    def run(self, cmd, cwd=None, env=None, capture_output=True, text=True, timeout=0):
        self.wrangler_cmd = cmd
        path = cmd[cmd.index("bulk") + 1]
        self.secrets_file = json.loads(Path(path).read_text())
        self.secrets_mode = stat.S_IMODE(os.stat(path).st_mode)
        self.secrets_path = path
        return SimpleNamespace(returncode=self.rc, stdout="", stderr="")


def go(net, env=None):
    return sw.run_setup(env or ENV, ROOT, http=net.http, run=net.run, sleep=lambda s: None)


def test_full_setup_happy_path():
    net = Net()
    res = go(net)
    assert [s for _, s in res] == ["ok"] * len(res) and [n for n, _ in res][-1] == "telegram_message"
    assert net.wrangler_cmd[:5] == ["npx", "--yes", "wrangler@4", "secret", "bulk"] and net.wrangler_cmd[-2:] == ["--name", "newsbot-approve"]
    s = net.secrets_file
    assert set(s) == {"GH_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WEBHOOK_SECRET"}
    assert s["GH_TOKEN"] == "gh-approve" and s["TELEGRAM_BOT_TOKEN"] == "123:abc" and len(s["WEBHOOK_SECRET"]) >= 32
    assert net.secrets_mode == 0o600 and not os.path.exists(net.secrets_path)             # private while it existed, gone afterwards
    hook = next(b for m, u, b in net.calls if u.endswith("/setWebhook"))
    assert hook["url"] == URL + "/telegram" and hook["secret_token"] == s["WEBHOOK_SECRET"] and hook["allowed_updates"] == ["callback_query"]
    assert tg(net.committed)["approve_buttons"] is True and tg(net.committed)["edit_url"] == URL
    out = " ".join(f"{n}={st}" for n, st in res)
    for secret in ("cf-tok", "gh-approve", "gh-brain", "123:abc", s["WEBHOOK_SECRET"], "workers.dev", "me/newsbot"):
        assert secret not in out                                                          # the public log shows words only


def test_second_run_with_config_already_on_makes_no_commit():
    net = Net(yaml_text=sw.set_notify_keys(OLD_YAML, URL))
    assert all(s == "ok" for _, s in go(net)) and net.committed is None


def test_failures_stop_early_with_a_reason_and_no_secret():
    res = go(Net(), {k: v for k, v in ENV.items() if k != "APPROVE_GH_TOKEN"})
    assert res == [("inputs", "fail:missing APPROVE_GH_TOKEN")]
    res = go(Net(cf=(403, {})))
    assert res[-1][0] == "workers_subdomain" and "Workers Scripts: Edit" in res[-1][1]
    res = go(Net(cf=(200, {"result": {"subdomain": None}})))
    assert res[-1][0] == "workers_subdomain" and "open Workers & Pages" in res[-1][1]
    net = Net(wrangler_rc=1)
    res = go(net)
    assert res[-1][0] == "worker_secrets" and not os.path.exists(net.secrets_path)       # the secrets file is removed even on failure
    res = go(Net(worker=(404, "nope")))
    assert res[-1][0] == "worker_answers"
    net = Net(hook=(401, {}))
    res = go(net)
    assert res[-1] == ("telegram_webhook", "fail:telegram http 401") and net.committed is None
    net = Net(put=403)
    res = go(net)
    assert res[-1][0] == "brain_config_edit" and "Contents: write" in res[-1][1]
    res = go(Net(yaml_text="site:\n  name: x\n"))
    assert res[-1][0] == "brain_config_edit" and "not found" in res[-1][1]
