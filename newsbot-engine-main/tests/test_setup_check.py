"""scripts/setup_check.py: prints only OK/FAIL/SKIP lines and never a secret value."""
import importlib.util
import tempfile
from pathlib import Path

from tests.helpers import ROOT, TEMPLATE

spec = importlib.util.spec_from_file_location("setup_check", ROOT / "scripts" / "setup_check.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

SECRET = "s3cr3t-value-123"
ENV = {"CLOUDFLARE_API_TOKEN": SECRET, "CLOUDFLARE_ACCOUNT_ID": "acct1",
       "BRAIN_REPO_TOKEN": SECRET + "b", "BRAIN_REPO": "me/newsbot-brain", "PAGES_PROJECT": "newsbot"}


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url))
        for (m, frag), resp in self.routes.items():
            if m == method and frag in url:
                return resp
        return 0, {}


GOOD = {("GET", "/repos/me/newsbot-brain"): (200, {"private": True, "permissions": {"push": True}}),
        ("GET", "/tokens/verify"): (200, {"success": True}),
        ("GET", "/pages/projects/newsbot"): (200, {}),
        ("POST", "/ai/run/"): (200, {"success": True})}


def test_env_check_flags_missing_and_bad_values_without_leaking():
    out = sc.check_env({**ENV, "BRAIN_REPO_TOKEN": "", "PUBLISH_MODE": "yolo"})
    assert ("FAIL", "secret BRAIN_REPO_TOKEN") in out and not any("GROQ" in n or "GEMINI" in n for _, n in out)
    assert ("FAIL", "variable PUBLISH_MODE is review or auto") in out
    assert all(SECRET not in n for _, n in sc.check_env(ENV))
    assert all(s == "OK" or "optional" in n for s, n in sc.check_env({**ENV, "PUBLISH_MODE": "auto"}) if s != "SKIP")


def test_brain_repo_checks_reachability_push_and_privacy():
    ok = sc.check_brain_repo(ENV, FakeHttp(GOOD))
    assert [s for s, _ in ok] == ["OK", "OK", "OK"]
    public = {**GOOD, ("GET", "/repos/me/newsbot-brain"): (200, {"private": False, "permissions": {"push": False}})}
    names = dict((n, s) for s, n in sc.check_brain_repo(ENV, FakeHttp(public)))
    assert names["brain token can push"] == "FAIL" and names["brain repo is private"] == "FAIL"
    assert sc.check_brain_repo(ENV, FakeHttp({}))[0][0] == "FAIL"


def test_cloudflare_creates_missing_pages_project_and_checks_workers_ai():
    routes = {**GOOD, ("GET", "/pages/projects/newsbot"): (404, {}), ("POST", "/pages/projects"): (200, {})}
    http = FakeHttp(routes)
    out = dict((n, s) for s, n in sc.check_cloudflare(ENV, http))
    assert out["pages project created"] == "OK" and out["cloudflare token valid"] == "OK" and "workers ai embeddings" not in out
    assert ("POST", "https://api.cloudflare.com/client/v4/accounts/acct1/pages/projects") in http.calls
    bad = dict((n, s) for s, n in sc.check_cloudflare(ENV, FakeHttp({})))
    assert bad["cloudflare token valid"] == "FAIL"


def test_every_llm_account_must_answer_an_embedding_call_and_none_is_a_failure():
    import json
    accts = [{"label": "P1", "account_id": "a" * 32, "token": "tok-one-" + "x" * 20}, {"label": "P2", "account_id": "b" * 32, "token": "tok-two-" + "y" * 20}]
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "accts.json"
        f.write_text(json.dumps(accts))
        env = {"NEWSBOT_CF_ACCOUNTS_FILE": str(f)}
        out = dict((n, s) for s, n in sc.check_llm_accounts(env, FakeHttp({("POST", "/accounts/" + "a" * 32 + "/ai/run/"): (200, {}), ("POST", "/accounts/" + "b" * 32 + "/ai/run/"): (403, {})})))
        assert out["llm accounts: 2"] == "OK" and out["llm account P1 workers ai embeddings"] == "OK" and out["llm account P2 workers ai embeddings"] == "FAIL"
        assert all("x" * 20 not in n and "a" * 32 not in n for n in out)                       # no token, no id in the output
    none = sc.check_llm_accounts({}, FakeHttp({}))
    assert none[0][0] == "FAIL" and "none" in none[0][1]
    legacy = sc.check_llm_accounts({"CLOUDFLARE_ACCOUNT_ID": "c" * 32, "CLOUDFLARE_API_TOKEN": "t" * 30}, FakeHttp({("POST", "/ai/run/"): (200, {})}))
    assert ("OK", "llm account MAIN workers ai embeddings") in legacy                          # the deploy account is the fallback


def test_brain_structure_against_the_template_and_an_empty_dir():
    assert all(s == "OK" for s, _ in sc.check_brain_structure(TEMPLATE))
    with tempfile.TemporaryDirectory() as d:
        assert all(s == "FAIL" for s, _ in sc.check_brain_structure(Path(d)))


def test_run_all_output_has_only_status_lines_and_no_secret_values():
    http = FakeHttp(GOOD)
    results = sc.run_all({**ENV, "PUBLISH_MODE": "review"}, TEMPLATE, http)
    text = "\n".join(f"{s} {n}" for s, n in results)
    assert not [r for r in results if r[0] == "FAIL"]
    assert SECRET not in text and "acct1" not in text and all(l.split()[0] in ("OK", "FAIL", "SKIP") for l in text.splitlines())
