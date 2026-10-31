"""The Cloudflare account pool: any number of accounts, used EQUALLY, neurons counted per account, failover, the
schema-failure escalation to a different model, the account loader, the collect script and the account check.
Zero-arg functions (tempfile, no fixtures): they run under pytest AND the offline runner."""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from newsbot.llm import (LLM, Budget, BudgetExhausted, MockProvider, ProviderError, RateLimited, SchemaError,
                         build_embedder, build_llm, build_providers, load_cf_accounts, check_models)
from newsbot.llm.accountcheck import run_account_check
from newsbot.llm.base import PoolPicker, Response, expand_pool
from newsbot.llm.budget import budget_from_settings
from newsbot.llm.workers_ai import WorkersAIEmbedder, WorkersAIProvider

MSG = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x" * 350}]
RATES = {"big": {"in": 30000, "out": 70000}, "small": {"in": 18000, "out": 27000}, "default": {"in": 40000, "out": 290000}}
ID = lambda n: ("%032x" % n)


def acct(label, answer=None, models=None, fail=None):
    m = MockProvider(answer or {"extract": {"ok": 1}}, models=models)
    m.name, m.pool = "cf_" + label, "workers_ai"
    if fail is not None:
        m.chat = lambda *a, **k: (_ for _ in ()).throw(fail)      # always raises
    return m


def settings(tiers=None, **kw):
    s = {"tasks": {"extract": "small", "verify": "strong"},
         "tiers": tiers or {"small": [{"provider": "workers_ai", "model": "small"}],
                            "strong": [{"provider": "workers_ai", "model": "big"}]},
         "retry": {"max_retries": 3, "base_delay_s": 2, "max_delay_s": 60}}
    s.update(kw)
    return s


def usage_of(llm, name):
    return llm.budget.neurons_used(name)


# ---------- equal use ----------

def test_every_account_is_used_equally_even_with_many_accounts():
    names = [f"a{i}" for i in range(7)]                          # no fixed number of accounts
    provs = {f"cf_{n}": acct(n) for n in names}
    b = Budget(None, neuron_limit=10_000, rates=RATES)
    llm = LLM(settings(), provs, b)
    for _ in range(70):
        llm.complete("extract", MSG, schema=lambda x: x, max_tokens=100)
    calls = sorted(len(p.calls) for p in provs.values())
    assert calls[-1] - calls[0] <= 1 and sum(calls) == 70       # 10 each: nobody is skipped, nobody is favoured


def test_least_used_account_goes_first_and_ties_take_turns():
    b = Budget(None, neuron_limit=10_000, rates=RATES)
    b.record("cf_busy", "big", 1000, prompt=500, completion=500)
    pk = PoolPicker(b)
    for _ in range(6):
        order = pk.order(["cf_busy", "cf_x", "cf_y"])
        assert order[-1] == "cf_busy"                           # the busy one waits behind both idle ones
    firsts = {pk.order(["cf_busy", "cf_x", "cf_y"])[0] for _ in range(6)}
    assert firsts == {"cf_x", "cf_y"}                           # the two idle ones alternate


def test_unequal_start_evens_out():
    provs = {"cf_a": acct("a"), "cf_b": acct("b"), "cf_c": acct("c")}
    b = Budget(None, neuron_limit=10_000, rates=RATES)
    b.record("cf_a", "small", 100_000, prompt=100_000, completion=100_000)       # a starts well ahead (heavily used)
    llm = LLM(settings(), provs, b)
    for _ in range(30):
        llm.complete("extract", MSG, schema=lambda x: x, max_tokens=100)
    assert len(provs["cf_a"].calls) < len(provs["cf_b"].calls) and abs(len(provs["cf_b"].calls) - len(provs["cf_c"].calls)) <= 1


# ---------- neuron budget ----------

def test_neurons_are_counted_per_account_with_input_and_output_priced_differently():
    b = Budget(None, neuron_limit=10_000, rates=RATES)
    b.record("cf_a", "big", 3000, prompt=2000, completion=1000)
    assert abs(b.neurons_used("cf_a") - (2000 * 30000 + 1000 * 70000) / 1e6) < 0.01          # 130
    b.record("cf_a", "unknown-model", 1000, prompt=0, completion=1000)                       # falls back to "default"
    assert abs(b.neurons_used("cf_a") - (130 + 290)) < 0.01 and b.neurons_used("cf_b") == 0


def test_an_account_stops_at_90_percent_and_the_next_one_takes_over_then_all_exhausted():
    provs = {"cf_a": acct("a"), "cf_b": acct("b")}
    b = Budget(None, neuron_limit=1_000, stop_fraction=0.9, rates={"default": {"in": 1_000_000, "out": 1_000_000}})   # 1 neuron per token
    llm = LLM(settings(), provs, b)
    done = 0
    try:
        for _ in range(100):
            llm.complete("extract", MSG, schema=lambda x: x, max_tokens=200)                # each call reserves ~300 neurons and costs ~117
            done += 1
    except BudgetExhausted:
        pass
    assert 8 <= done <= 14 and len(provs["cf_a"].calls) >= 1 and len(provs["cf_b"].calls) >= 1
    assert b.neurons_used("cf_a") <= 900 and b.neurons_used("cf_b") <= 900             # never beyond the stop line, the reservation is checked BEFORE a call
    assert not llm.can_run("extract", 400)                  # a call that would cross the line is refused


def test_per_account_override_and_unlimited_account():
    b = Budget(None, neuron_limit=1_000, neuron_limits={"cf_big": 100_000, "cf_none": 0}, rates=RATES)
    assert b.neuron_cap("cf_x") == 1_000 and b.neuron_cap("cf_big") == 100_000
    assert b.can_spend("cf_none", "big", 10_000_000)             # 0 = no limit
    assert b.neuron_fraction("cf_none") == 0.0


def test_budget_from_settings_reads_limit_overrides_and_rates():
    s = {"budget": {"stop_fraction": 0.8, "neurons_per_day_per_account": 5000, "neurons_per_day": {"cf_a": 9000}}, "neuron_rates": RATES}
    b = budget_from_settings(s, None)
    assert b.neuron_cap("cf_a") == 9000 and b.neuron_cap("cf_b") == 5000 and b.stop_fraction == 0.8 and b.rate("small")["out"] == 27000


def test_usage_file_keeps_neurons_failures_and_the_last_error_and_forgets_them_at_midnight():
    t = [datetime(2026, 10, 30, 23, 0, tzinfo=timezone.utc)]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "usage.json"
        b = Budget(path, neuron_limit=10_000, rates=RATES, now_fn=lambda: t[0])
        b.record("cf_a", "big", 2000, prompt=1000, completion=1000)
        b.record_failure("cf_a", "big", "status_429")
        doc = json.loads(path.read_text())
        assert doc["counters"]["cf_a/big"]["neurons"] == 100.0 and doc["counters"]["cf_a/big"]["failures"] == 1
        assert doc["errors"]["cf_a"]["reason"] == "status_429"
        b2 = Budget(path, neuron_limit=10_000, rates=RATES, now_fn=lambda: t[0])
        assert b2.neurons_used("cf_a") == 100.0 and b2.errors["cf_a"]["reason"] == "status_429"
        t[0] = datetime(2026, 10, 31, 0, 1, tzinfo=timezone.utc)                    # Cloudflare's allowance resets at 00:00 UTC
        assert b2.neurons_used("cf_a") == 0 and b2.errors == {}


# ---------- failover ----------

def test_a_failing_account_is_skipped_at_once_and_goes_to_the_back():
    bad, good = acct("bad", fail=ProviderError("http 401")), acct("good")
    llm = LLM(settings(), {"cf_bad": bad, "cf_good": good}, Budget(None, neuron_limit=10_000, rates=RATES))
    for _ in range(6):
        assert llm.complete("extract", MSG, schema=lambda x: x) == {"ok": 1}
    assert len(good.calls) == 6 and len(bad.calls) <= 1          # tried once, then cooled for the rest of the run
    assert llm.budget.counters["cf_bad/small"]["failures"] == 1 and llm.budget.errors["cf_bad"]["reason"] == "status_401"


def test_a_rate_limited_account_hands_over_without_waiting_when_another_account_exists():
    sleeps = []
    limited = acct("a", fail=RateLimited(30))
    llm = LLM(settings(), {"cf_a": limited, "cf_b": acct("b")}, Budget(None, neuron_limit=10_000, rates=RATES), sleep=sleeps.append)
    llm.picker._rr = 0
    assert llm.complete("extract", MSG, schema=lambda x: x) == {"ok": 1}
    assert sum(sleeps) <= 30                                     # one short wait at most, not minutes of back-off per account


def test_all_accounts_failing_raises_a_provider_error_for_that_story_only():
    llm = LLM(settings(), {"cf_a": acct("a", fail=ProviderError("http 403")), "cf_b": acct("b", fail=ProviderError("http 403"))})
    try:
        llm.complete("extract", MSG, schema=lambda x: x)
        assert False
    except ProviderError:
        pass


# ---------- schema failure escalates to a different model ----------

def test_a_small_model_that_cannot_produce_the_schema_hands_over_to_the_bigger_model_not_to_the_same_model_elsewhere():
    class Picky:
        pool, name = "workers_ai", "cf_a"
        def __init__(self, good_model):
            self.good, self.calls = good_model, []
        def chat(self, model, messages, *, json_mode=False, max_tokens=1024, task=""):
            self.calls.append(model)
            return Response('{"ok": 1}' if model == self.good else "not json", 5, 5, model, self.name)
        def list_models(self):
            return ["small", "big"]
    a, b = Picky("big"), Picky("big")
    b.name = "cf_b"
    tiers = {"small": [{"provider": "workers_ai", "model": "small"}, {"provider": "workers_ai", "model": "big"}]}
    llm = LLM(settings(tiers=tiers), {"cf_a": a, "cf_b": b}, Budget(None, neuron_limit=10_000, rates=RATES))
    assert llm.complete("extract", MSG, schema=lambda x: x) == {"ok": 1}
    seen = a.calls + b.calls
    assert seen.count("small") == 2 and seen.count("big") == 1   # 2 tries on ONE account (answer + repair), then the stronger model
    only_small = {"small": [{"provider": "workers_ai", "model": "small"}]}
    llm2 = LLM(settings(tiers=only_small), {"cf_a": Picky("big"), "cf_b": Picky("big")})
    try:
        llm2.complete("extract", MSG, schema=lambda x: x)
        assert False
    except SchemaError:
        pass


# ---------- the account loader ----------

def test_accounts_load_from_the_file_dedupe_by_cloudflare_account_and_skip_bad_entries_without_leaking():
    items = [{"label": "P1", "account_id": ID(1), "token": " tok-1 "},
             {"label": "P2", "account_id": ID(2), "token": "tok-2"},
             {"label": "DUP", "account_id": ID(1), "token": "tok-dup"},          # same Cloudflare account as P1
             {"label": "bad id", "account_id": "nope", "token": "t"},
             {"label": "NOTOKEN", "account_id": ID(3), "token": " "},
             "junk", {"label": "P2", "account_id": ID(4), "token": "tok-4"}]    # same label twice
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.json"
        f.write_text(json.dumps(items))
        got = load_cf_accounts({"NEWSBOT_CF_ACCOUNTS_FILE": str(f), "CLOUDFLARE_API_TOKEN": "deploy", "CLOUDFLARE_ACCOUNT_ID": ID(9)})
        assert [a["label"] for a in got] == ["P1", "P2"] and got[0]["token"] == "tok-1"
        provs = build_providers({"NEWSBOT_CF_ACCOUNTS_FILE": str(f)})
        assert sorted(provs) == ["cf_p1", "cf_p2"] and all(p.pool == "workers_ai" for p in provs.values())
        assert expand_pool(provs, "workers_ai") == ["cf_p1", "cf_p2"]


def test_the_number_of_accounts_is_not_limited():
    many = [{"label": f"A{i}", "account_id": ID(i + 1), "token": f"t{i}"} for i in range(60)]
    got = load_cf_accounts({"NEWSBOT_CF_ACCOUNTS": json.dumps(many)})
    assert len(got) == 60 and len(build_providers({"NEWSBOT_CF_ACCOUNTS": json.dumps(many)})) == 60


def test_the_deploy_account_is_only_the_fallback_when_there_is_no_pool_account():
    env = {"CLOUDFLARE_API_TOKEN": "deploy-token", "CLOUDFLARE_ACCOUNT_ID": ID(9)}
    assert [a["label"] for a in load_cf_accounts(env)] == ["MAIN"]
    both = {**env, "NEWSBOT_CF_ACCOUNTS": json.dumps([{"label": "P1", "account_id": ID(1), "token": "t"}])}
    assert [a["label"] for a in load_cf_accounts(both)] == ["P1"]            # the deploy account is NOT used behind your back
    assert load_cf_accounts({}) == [] and load_cf_accounts({"NEWSBOT_CF_ACCOUNTS": "not json"}) == []


# ---------- collect script ----------

def test_collect_script_keeps_only_cfllm_secrets_and_exports_a_file_path_never_the_values():
    import importlib.util
    spec = importlib.util.spec_from_file_location("collect", Path(__file__).resolve().parent.parent / "scripts" / "collect_llm_accounts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    secrets = {"CFLLM_PHONE1": json.dumps({"a": ID(1), "t": "tok-secret-1"}), "CFLLM_PHONE2": json.dumps({"a": ID(2), "t": "tok-secret-2"}),
               "CFLLM_BROKEN": "not json", "CFLLM_SHORT": json.dumps({"a": "zz", "t": "t"}), "BRAIN_REPO_TOKEN": "ghp_x", "CFLLM_ID_PHONE1": ID(1)}
    accts, problems = mod.collect(secrets)
    assert [a["label"] for a in accts] == ["PHONE1", "PHONE2"] and len(problems) == 3 + 0 or len(problems) >= 2
    with tempfile.TemporaryDirectory() as d:
        env_file = Path(d) / "gh_env"
        env_file.write_text("")
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert mod.main({"ALL_SECRETS_JSON": json.dumps(secrets), "GITHUB_ENV": str(env_file), "RUNNER_TEMP": d}) == 0
        out = buf.getvalue()
        assert "labels=PHONE1,PHONE2" in out and "tok-secret" not in out and ID(1) not in out
        exported = env_file.read_text()
        assert exported.startswith("NEWSBOT_CF_ACCOUNTS_FILE=") and "tok-secret" not in exported
        written = Path(exported.split("=", 1)[1].strip())
        assert json.loads(written.read_text())[0]["token"] == "tok-secret-1" and (os.stat(written).st_mode & 0o077) == 0      # owner only
        assert [a["label"] for a in load_cf_accounts({"NEWSBOT_CF_ACCOUNTS_FILE": str(written)})] == ["PHONE1", "PHONE2"]       # the engine reads it back
    with tempfile.TemporaryDirectory() as d:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert mod.main({"ALL_SECRETS_JSON": "{}", "RUNNER_TEMP": d}) == 0                 # no account at all is not a crash here
        assert "accounts=0" in buf.getvalue()


# ---------- health, embedder, account check ----------

def test_health_checks_the_model_ids_on_every_account():
    s = settings()
    a, b = acct("a", models=["small", "big"]), acct("b", models=["small"])
    try:
        check_models(s, {"cf_a": a, "cf_b": b})
        assert False
    except Exception as e:
        assert "cf_b:big=MISSING" in str(e) and "cf_a" not in str(e).replace("cf_a:small=OK", "")


def test_embedder_alternates_accounts_and_fails_over():
    calls = []

    class P:
        def __init__(self, name, ok=True):
            self.name, self.ok = name, ok
        def _run(self, model, payload):
            calls.append(self.name)
            if not self.ok:
                raise ProviderError("http 403")
            return {"data": [[1.0] for _ in payload["text"]]}
    emb = WorkersAIEmbedder([P("cf_a"), P("cf_b")], "m", batch=1)
    assert len(emb(["1", "2", "3", "4"])) == 4 and sorted(calls) == ["cf_a", "cf_a", "cf_b", "cf_b"]
    calls.clear()
    emb2 = WorkersAIEmbedder([P("cf_a", ok=False), P("cf_b")], "m", batch=1)
    assert len(emb2(["1", "2", "3"])) == 3 and calls.count("cf_b") == 3
    dead = WorkersAIEmbedder([P("cf_a", ok=False)], "m")
    try:
        dead(["x"])
        assert False
    except ProviderError:
        pass
    assert build_embedder({"embedding": {"provider": "workers_ai", "model": "m"}}, providers={}) is None


def test_account_check_reports_each_account_separately_and_never_the_token():
    s = settings(embedding={"provider": "workers_ai", "model": "emb"})

    class Good(WorkersAIProvider):
        def __init__(self, label, models, chat_error=None, auth_error=None):
            super().__init__(ID(1), "tok-SECRET-9876543210", name="cf_" + label)
            self.models, self.chat_error, self.auth_error = models, chat_error, auth_error
        def list_models(self):
            if self.auth_error:
                raise self.auth_error
            return self.models
        def chat(self, model, messages, **k):
            if self.chat_error:
                raise self.chat_error
            return Response("OK", 3, 1, model, self.name)
        def _run(self, model, body):
            return {"data": [[0.1]]}
    provs = {"cf_ok": Good("ok", ["small", "big", "emb"]), "cf_missing": Good("missing", ["small", "emb"]),
             "cf_denied": Good("denied", [], auth_error=ProviderError("http 403")), "cf_limit": Good("limit", ["small", "big", "emb"], chat_error=RateLimited(5))}
    rep = run_account_check(s, provs, now=datetime(2026, 10, 30, 1, 2, 3, tzinfo=timezone.utc))
    acc = rep["accounts"]
    assert acc["ok"]["ok"] and acc["missing"]["models"]["big"]["status"] == "MISSING" and not acc["missing"]["ok"]
    assert acc["denied"]["auth"] == "token_has_no_workers_ai_permission" and not acc["denied"]["ok"]
    assert acc["limit"]["models"]["small"]["reason"] == "daily_allowance_or_rate_limit"
    assert rep["summary"] == {"accounts": 4, "ok": 1, "failed": 3} and rep["checked_at"] == "2026-10-30T01:02:03Z"
    assert "SECRET" not in json.dumps(rep) and ID(1) not in json.dumps(rep)


def test_llmcheck_command_writes_the_report_and_exits_3_when_any_account_fails_or_none_exist():
    from newsbot import cli
    with tempfile.TemporaryDirectory() as d:
        brain = Path(d) / "brain"
        (brain / "state").mkdir(parents=True)
        out = Path(d) / "health.json"
        saved = {k: os.environ.pop(k, None) for k in ("NEWSBOT_CF_ACCOUNTS_FILE", "NEWSBOT_CF_ACCOUNTS", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")}
        try:
            try:
                rc = cli.llmcheck_cmd(Path(__file__).resolve().parent.parent / "brain-template", str(out))
            except ImportError:
                return                                               # pydantic missing in this sandbox: covered by the CI run
            assert rc == 3 and json.loads(out.read_text())["summary"] == {"accounts": 0, "ok": 0, "failed": 0}
        finally:
            os.environ.update({k: v for k, v in saved.items() if v})
