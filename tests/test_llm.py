"""M2 tests. Zero-arg functions (tempfile, no fixtures) so they run under pytest AND the offline runner."""
import json
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsbot.llm import (LLM, Budget, BudgetExhausted, HealthCheckError, MockProvider, ProviderError,
                         RateLimited, SchemaError, build_embedder, build_llm, check_models,
                         load_llm_settings)
from newsbot.llm.base import Response, parse_json
from newsbot.llm.gemini import GeminiProvider
from newsbot.llm.groq import GroqProvider
from newsbot.llm.workers_ai import WorkersAIEmbedder, WorkersAIProvider

TEMPLATE = Path(__file__).resolve().parent.parent / "brain-template"
MSG = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x" * 350}]  # ~100 tokens est


def settings(**kw):
    s = {"tasks": {"extract": "small", "write_en": "strong"},
         "tiers": {"small": [{"provider": "mock", "model": "m-small"}],
                   "strong": [{"provider": "mock", "model": "m-strong"}]},
         "retry": {"max_retries": 3, "base_delay_s": 2, "max_delay_s": 60}}
    s.update(kw)
    return s


class Flaky:
    """Provider that raises the queued exceptions, then returns text."""
    name = "flaky"

    def __init__(self, script, text='{"ok": 1}'):
        self.script, self.text, self.n = list(script), text, 0

    def chat(self, model, messages, *, json_mode=False, max_tokens=1024, task=""):
        self.n += 1
        if self.script:
            raise self.script.pop(0)
        return Response(self.text, 10, 10, model, self.name)

    def list_models(self):
        return ["m"]


def test_budget_stops_work_at_the_limit():
    with tempfile.TemporaryDirectory() as d:
        mock = MockProvider({"extract": {"a": 1}})
        b = Budget(Path(d) / "usage.json", token_limits={"mock": 1000}, stop_fraction=0.9)
        llm = LLM(settings(), {"mock": mock}, b)
        ok = 0
        try:
            for _ in range(50):
                llm.complete("extract", MSG, schema=lambda x: x, max_tokens=100)
                ok += 1
        except BudgetExhausted:
            pass
        assert 1 <= ok < 50 and len(mock.calls) == ok          # stopped, and made no call after the stop
        assert b._used("mock", "tokens") <= 900 + 300          # last call may overshoot only by its own size
        assert not llm.can_run("extract", 500)


def test_budget_persists_and_rolls_over_at_day_change():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "usage.json"
        t = [datetime(2026, 10, 14, 10, tzinfo=timezone.utc)]
        b = Budget(p, token_limits={"groq": 1000}, now_fn=lambda: t[0])
        b.record("groq", "m1", 400)
        b2 = Budget(p, token_limits={"groq": 1000}, now_fn=lambda: t[0])
        assert b2.counters["groq/m1"] == {"tokens": 400, "requests": 1}
        assert not b2.can_spend("groq", "m1", 600)             # 400+600 > 900
        t[0] += timedelta(days=1)
        assert b2.can_spend("groq", "m1", 600) and b2.counters == {}


def test_model_level_limit_wins_over_provider_limit():
    b = Budget(None, token_limits={"groq": 10_000, "groq/big": 100}, stop_fraction=1.0)
    assert b.can_spend("groq", "small", 5_000) and not b.can_spend("groq", "big", 101)
    b.record("groq", "small", 9_000)
    assert not b.can_spend("groq", "small", 2_000)             # provider-level sum counted across models
    assert b.can_spend("other", "x", 10**9)                    # no limit configured = unlimited


def test_request_limit_stops_work():
    b = Budget(None, request_limits={"mock": 2}, stop_fraction=1.0)
    llm = LLM(settings(), {"mock": MockProvider({"extract": {}})}, b)
    llm.complete("extract", MSG, schema=lambda x: x)
    llm.complete("extract", MSG, schema=lambda x: x)
    try:
        llm.complete("extract", MSG, schema=lambda x: x)
        assert False, "third request should be blocked"
    except BudgetExhausted:
        pass


def test_429_backoff_honours_retry_after_then_exponential():
    sleeps = []
    prov = Flaky([RateLimited(7), RateLimited(None), RateLimited(None)])
    llm = LLM(settings(tiers={"small": [{"provider": "flaky", "model": "m"}]}), {"flaky": prov},
              sleep=sleeps.append)
    assert llm.complete("extract", MSG, schema=lambda x: x) == {"ok": 1}
    assert sleeps == [7, 4, 8] and prov.n == 4      # retry-after wins; otherwise base*2**attempt


def test_429_gives_up_after_max_retries_and_falls_through_chain():
    sleeps = []
    flaky = Flaky([RateLimited(1)] * 10)
    s = settings(tiers={"small": [{"provider": "flaky", "model": "m"}, {"provider": "mock", "model": "m2"}]})
    llm = LLM(s, {"flaky": flaky, "mock": MockProvider({"extract": {"from": "mock"}})}, sleep=sleeps.append)
    assert llm.complete("extract", MSG, schema=lambda x: x) == {"from": "mock"}
    assert flaky.n == 4 and len(sleeps) == 3                   # 1 try + 3 retries
    only = LLM(settings(tiers={"small": [{"provider": "flaky", "model": "m"}]}),
               {"flaky": Flaky([RateLimited(1)] * 10)}, sleep=lambda s: None)
    try:
        only.complete("extract", MSG, schema=lambda x: x)
        assert False
    except ProviderError:
        pass


def test_json_repair_retry_once():
    seen = []
    def handler(messages):
        seen.append(len(messages))
        return "not json at all" if len(seen) == 1 else "```json\n{\"title\": \"ok\"}\n```"
    mock = MockProvider({"extract": handler})
    llm = LLM(settings(), {"mock": mock})
    out = llm.complete("extract", MSG, schema=lambda d: d if "title" in d else (_ for _ in ()).throw(ValueError("no title")))
    assert out == {"title": "ok"} and seen == [2, 4]            # repair call carries output + error message


def test_schema_failure_after_repair_raises_schema_error_for_that_call_only():
    mock = MockProvider({"extract": {"wrong": 1}})
    llm = LLM(settings(), {"mock": mock})
    def need_title(d):
        if "title" not in d:
            raise ValueError("missing title")
        return d
    try:
        llm.complete("extract", MSG, schema=need_title)
        assert False
    except SchemaError:
        pass
    assert len(mock.calls) == 2                                  # first try + exactly one repair
    assert llm.complete("extract", MSG, schema=lambda d: d) == {"wrong": 1}   # next call is unaffected


def test_text_mode_returns_raw_text_without_schema():
    llm = LLM(settings(), {"mock": MockProvider({"write_en": "hello"})})
    assert llm.complete("write_en", MSG) == "hello"


def test_unknown_task_and_missing_provider_fail_clearly():
    llm = LLM(settings(), {"mock": MockProvider()})
    for task in ("nope",):
        try:
            llm.complete(task, MSG)
            assert False
        except Exception as e:
            assert "unknown task" in str(e)
    llm2 = LLM(settings(tiers={"small": [{"provider": "groq", "model": "x"}]}), {})
    try:
        llm2.chain("extract")
        assert False
    except Exception as e:
        assert "no available provider" in str(e)


def test_parse_json_variants():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('Sure! Here:\n```json\n{"a": [1, 2]}\n```\nDone') == {"a": [1, 2]}
    assert parse_json('[{"a": 1}]') == [{"a": 1}]
    for bad in ("", "no json", '{"a": '):
        try:
            parse_json(bad)
            assert False, bad
        except ValueError:
            pass


def test_logs_never_contain_prompts():
    class H(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []
        def emit(self, r):
            self.lines.append(r.getMessage())
    h = H()
    lg = logging.getLogger("newsbot")
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        secret = [{"role": "user", "content": "SECRET-DRAFT-TEXT " * 20}]
        llm = LLM(settings(), {"mock": MockProvider({"extract": {"k": "SECRET-DRAFT-TEXT"}})})
        llm.complete("extract", secret, schema=lambda d: d)
    finally:
        lg.removeHandler(h)
    assert h.lines and not any("SECRET-DRAFT-TEXT" in x for x in h.lines)


# ---------- providers (fake transport, no network) ----------

def fake_transport(status=200, headers=None, body=None, record=None):
    def t(method, url, *, headers=None, json=None, timeout=60):
        if record is not None:
            record.append({"method": method, "url": url, "headers": headers, "json": json})
        return status, headers_out, body
    headers_out = headers or {}
    return t


def test_groq_parses_response_sends_json_mode_and_key_header_only():
    rec = []
    body = {"choices": [{"message": {"content": '{"a":1}'}}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
    g = GroqProvider("gsk_TESTKEY123456", fake_transport(body=body, record=rec))
    r = g.chat("openai/gpt-oss-20b", MSG, json_mode=True, max_tokens=50)
    assert r.text == '{"a":1}' and r.total_tokens == 12
    assert rec[0]["json"]["response_format"] == {"type": "json_object"} and rec[0]["json"]["model"] == "openai/gpt-oss-20b"
    assert "gsk_" not in rec[0]["url"]


def test_groq_429_reads_retry_after_and_4xx_is_not_retryable():
    try:
        GroqProvider("k" * 10, fake_transport(429, {"retry-after": "7"})).chat("m", MSG)
        assert False
    except RateLimited as e:
        assert e.retry_after == 7.0
    try:
        GroqProvider("k" * 10, fake_transport(401)).chat("m", MSG)
        assert False
    except ProviderError as e:
        assert "401" in str(e)
    try:
        GroqProvider("k" * 10, fake_transport(503)).chat("m", MSG)
        assert False
    except RateLimited:
        pass                                                   # 5xx treated as transient


def test_groq_and_gemini_list_models():
    g = GroqProvider("k" * 10, fake_transport(body={"data": [{"id": "a"}, {"id": "b"}]}))
    assert g.list_models() == ["a", "b"]
    gm = GeminiProvider("k" * 10, fake_transport(body={"models": [{"name": "models/gemini-x"}]}))
    assert gm.list_models() == ["gemini-x"]


def test_gemini_keeps_key_out_of_url_and_maps_roles():
    rec = []
    body = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}}
    g = GeminiProvider("AIza-secret-key", fake_transport(body=body, record=rec))
    r = g.chat("gemini-x", MSG + [{"role": "assistant", "content": "a"}], json_mode=True)
    assert r.text == "hi" and r.total_tokens == 4
    assert "AIza" not in rec[0]["url"] and rec[0]["headers"]["x-goog-api-key"] == "AIza-secret-key"
    assert rec[0]["json"]["contents"][-1]["role"] == "model" and "systemInstruction" in rec[0]["json"]


def test_workers_ai_chat_and_embedder_batches():
    calls = []
    def t(method, url, *, headers=None, json=None, timeout=60):
        calls.append(json)
        if "text" in json:
            return 200, {}, {"success": True, "result": {"data": [[1.0, 0.0] for _ in json["text"]]}}
        return 200, {}, {"success": True, "result": {"response": "ok", "usage": {"prompt_tokens": 2, "completion_tokens": 1}}}
    p = WorkersAIProvider("acct", "tok123456", t)
    assert p.chat("@cf/x", MSG).text == "ok"
    emb = WorkersAIEmbedder(p, "@cf/baai/bge-m3", batch=2)
    vecs = emb(["a", "b", "c", "d", "e"])
    assert len(vecs) == 5 and [len(c["text"]) for c in calls[1:]] == [2, 2, 1]


def test_workers_ai_embedder_rejects_wrong_shape():
    p = WorkersAIProvider("a", "tok123456", lambda *a, **k: (200, {}, {"success": True, "result": {"data": [[1.0]]}}))
    try:
        WorkersAIEmbedder(p, "m")(["a", "b"])
        assert False
    except ProviderError:
        pass


# ---------- health check, factory, config ----------

def test_health_fails_loudly_on_unknown_model_and_hides_secrets():
    s = settings(tiers={"small": [{"provider": "mock", "model": "ghost"}], "strong": [{"provider": "mock", "model": "m-strong"}]})
    try:
        check_models(s, {"mock": MockProvider(models=["m-strong"])})
        assert False
    except HealthCheckError as e:
        assert "mock:ghost=MISSING" in str(e)
    res = check_models(s, {"mock": MockProvider(models=["m-strong", "ghost"])})
    assert all(r["status"] == "OK" for r in res)


def test_health_skips_providers_without_keys_but_requires_each_task_to_have_one_usable():
    s = {"tasks": {"extract": "small"}, "tiers": {"small": [{"provider": "groq", "model": "a"},
                                                            {"provider": "mock", "model": "b"}]}}
    res = check_models(s, {"mock": MockProvider()})
    assert [r["status"] for r in res] == ["SKIP", "OK"]
    try:
        check_models(s, {})
        assert False
    except HealthCheckError as e:
        assert "no usable provider" in str(e)


def test_health_error_when_models_endpoint_unreachable():
    class Down:
        name = "groq"
        def list_models(self):
            raise ProviderError("network error: ConnectionError")
    s = {"tasks": {"extract": "small"}, "tiers": {"small": [{"provider": "groq", "model": "a"}]}}
    try:
        check_models(s, {"groq": Down()})
        assert False
    except HealthCheckError as e:
        assert "ERROR" in str(e)


def test_template_llm_yaml_is_consistent():
    s = load_llm_settings(TEMPLATE)
    assert set(s["tasks"].values()) <= set(s["tiers"])
    assert s["tiers"]["small"][0]["provider"] == "groq" and s["embedding"]["provider"] == "workers_ai"
    assert s["budget"]["stop_fraction"] == 0.9 and s["budget"]["tokens_per_day"]["groq/openai/gpt-oss-120b"] == 180000
    assert all(e["model"] for tier in s["tiers"].values() for e in tier)


def test_build_llm_dry_run_uses_mock_and_no_state():
    s = load_llm_settings(TEMPLATE)
    mock = MockProvider({"extract": {"claims": []}})
    llm = build_llm(s, None, dry_run=True, mock=mock)
    assert llm.complete("extract", MSG, schema=lambda d: d) == {"claims": []}
    assert build_embedder(s, providers={}) is None             # no Cloudflare creds -> lexical fallback


def test_build_llm_real_chain_skips_providers_without_keys():
    s = load_llm_settings(TEMPLATE)
    with tempfile.TemporaryDirectory() as d:
        mock_groq = MockProvider({"extract": {"ok": 1}})
        mock_groq.name = "groq"
        llm = build_llm(s, d, providers={"groq": mock_groq})
        assert [p for p, _ in llm.chain("extract")] == ["groq"]
        llm.complete("extract", MSG, schema=lambda x: x)
        usage = json.loads((Path(d) / "usage.json").read_text())
        assert "groq/openai/gpt-oss-20b" in usage["counters"]


def test_health_cmd_fails_loudly_without_keys():
    from newsbot.cli import health_cmd
    import os
    saved = {k: os.environ.pop(k, None) for k in ("GROQ_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "GEMINI_API_KEY")}
    try:
        assert health_cmd(TEMPLATE) == 3
    finally:
        os.environ.update({k: v for k, v in saved.items() if v})
