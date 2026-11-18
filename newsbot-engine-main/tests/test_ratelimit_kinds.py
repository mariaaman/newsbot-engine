"""Real log 2026-11-01: both Cloudflare accounts answered 429 to every call (12 of 12) and each failed call waited ~35 s on
each account, but nothing said WHY. Now: the 429 is classified (daily allowance spent / platform busy / per-minute rate), a spent
account is skipped for the rest of the day, and after two calls in a row where every account was rate limited the run stops
asking for 5 minutes (the story is queued, the run still commits)."""
from newsbot.llm import LLM, BudgetExhausted, ProviderError, RateLimited
from newsbot.llm.base import Response
from newsbot.llm.budget import Budget
from newsbot.llm.workers_ai import WorkersAIProvider, error_info

MSG = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x" * 350}]
S = {"tasks": {"extract": "small"},
     "tiers": {"small": [{"provider": "cf_a", "model": "m"}, {"provider": "cf_b", "model": "m"}]},
     "retry": {"max_retries": 3, "base_delay_s": 5, "max_delay_s": 60, "fast_retries": 1}}

DAILY = {"success": False, "errors": [{"code": 3036, "message": "AiError: 4006: you have used up your daily free allocation of 10,000 neurons"}]}


def test_error_info_classifies_without_returning_any_text():
    assert error_info(DAILY) == ("daily", 3036)
    assert error_info({"errors": [{"code": 3040, "message": "Capacity temporarily exceeded"}]}) == ("capacity", 3040)
    assert error_info({"errors": [{"code": 2001, "message": "Too many requests"}]}) == ("rate", 2001)
    assert error_info({}) == ("rate", None) and error_info(None) == ("rate", None) and error_info("oops") == ("rate", None)


def test_the_provider_raises_a_classified_rate_limit():
    def tr(status, body):
        return lambda *a, **k: (status, {"retry-after": "7"}, body)
    for body, kind, code in ((DAILY, "daily", 3036), ({}, "rate", None)):
        p = WorkersAIProvider("a" * 32, "tok", transport=tr(429, body), name="cf_a")
        try:
            p.chat("m", [{"role": "user", "content": "hi"}], max_tokens=100)
            raise AssertionError("expected RateLimited")
        except RateLimited as e:
            assert (e.kind, e.code, e.retry_after) == (kind, code, 7.0)


class Prov:
    pool = "workers_ai"

    def __init__(self, name, script):
        self.name, self.script, self.n = name, list(script), 0

    def chat(self, model, messages, *, json_mode=False, max_tokens=1024, task=""):
        self.n += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return Response('{"ok": 1}', 10, 5, model, self.name, "stop")

    def list_models(self):
        return ["m"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_a_spent_daily_allowance_is_not_retried_and_the_account_is_skipped_for_the_day():
    a, b = Prov("cf_a", [RateLimited(None, kind="daily", code=3036)]), Prov("cf_b", ["ok"])
    budget = Budget(None, neuron_limit=10000)
    c = Clock()
    llm = LLM(S, {"cf_a": a, "cf_b": b}, budget=budget, sleep=c.sleep, clock=c)
    assert llm.complete("extract", MSG, schema=lambda x: x) == {"ok": 1}
    assert a.n == 1 and c.t == 1000.0                                  # no retry, no waiting
    assert budget.errors["cf_a"]["reason"] == "daily_allowance_spent"
    assert not budget.can_spend("cf_a", "m", 100) and budget.can_spend("cf_b", "m", 100)
    for _ in range(3):
        llm.complete("extract", MSG, schema=lambda x: x)
    assert a.n == 1 and b.n == 4                                       # later calls never touch the spent account


def test_two_calls_with_every_account_rate_limited_stop_the_asking_for_five_minutes():
    a, b = Prov("cf_a", [RateLimited(None)]), Prov("cf_b", [RateLimited(None)])
    c = Clock()
    llm = LLM(S, {"cf_a": a, "cf_b": b}, sleep=c.sleep, clock=c)
    for _ in range(2):
        try:
            llm.complete("extract", MSG, schema=lambda x: x)
            raise AssertionError("expected ProviderError")
        except ProviderError:
            pass
    calls = a.n + b.n
    try:
        llm.complete("extract", MSG, schema=lambda x: x)               # blocked: nothing is sent
        raise AssertionError("expected BudgetExhausted")
    except BudgetExhausted as e:
        assert "rate limited" in str(e)
    assert a.n + b.n == calls
    c.t += 301                                                         # the block is over: it asks again
    try:
        llm.complete("extract", MSG, schema=lambda x: x)
    except ProviderError:
        pass
    assert a.n + b.n > calls


def test_one_success_resets_the_streak():
    a = Prov("cf_a", [RateLimited(None), "ok", RateLimited(None), RateLimited(None)])
    S1 = {**S, "tiers": {"small": [{"provider": "cf_a", "model": "m"}]}, "retry": {"max_retries": 0}}
    c = Clock()
    llm = LLM(S1, {"cf_a": a}, sleep=c.sleep, clock=c)
    def call():                                                         # noqa: E306
        try:
            llm.complete("extract", MSG, schema=lambda x: x)
        except ProviderError:
            pass
    call(); call(); call()                                              # fail, ok, fail: the streak never reaches two
    assert llm._rl_block_until == 0.0
