"""Two failures seen in the real hourly logs (2026-10-31): (1) the verify answer was cut off at max_tokens (invalid JSON),
was re-asked with the SAME limit plus the broken answer, was cut off again and the story failed; (2) nothing ended a run
that was slow, and the hourly job is killed at 30 minutes (then no commit at all)."""
from newsbot.llm import LLM, BudgetExhausted, RateLimited, SchemaError
from newsbot.llm.base import MAX_RETRY_TOKENS, Response

MSG = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x" * 350}]
S = {"tasks": {"verify": "strong"}, "tiers": {"strong": [{"provider": "cf_a", "model": "big"}]},
     "retry": {"max_retries": 2, "base_delay_s": 10, "max_delay_s": 60}}


class Scripted:
    """Answers from a script of (text, finish_reason) or exceptions; records the max_tokens and message count of every call."""
    name = "cf_a"
    pool = "workers_ai"

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def chat(self, model, messages, *, json_mode=False, max_tokens=1024, task=""):
        self.calls.append((max_tokens, len(messages)))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        text, fin = item
        return Response(text, 100, 50, model, self.name, fin)

    def list_models(self):
        return ["big"]


def ok(x):
    if not isinstance(x, dict) or "facts" not in x:
        raise ValueError("no facts")
    return x


def test_a_cut_off_answer_is_asked_again_with_more_room_and_without_the_broken_answer():
    p = Scripted([('{"facts": [{"statement": "a long sta', "length"), ('{"facts": []}', "stop")])
    llm = LLM(S, {"cf_a": p})
    assert llm.complete("verify", MSG, schema=ok, max_tokens=5000) == {"facts": []}
    assert p.calls == [(5000, 2), (9000, 2)]                      # more room, and still only the 2 original messages


def test_the_extra_room_is_capped_and_a_second_cut_off_fails_cleanly():
    p = Scripted([('{"facts": [', "length"), ('{"facts": [', "length"), ('{"facts": [', "length")])
    llm = LLM(S, {"cf_a": p})
    try:
        llm.complete("verify", MSG, schema=ok, max_tokens=12000)
        raise AssertionError("expected SchemaError")
    except SchemaError:
        pass
    assert p.calls == [(12000, 2), (MAX_RETRY_TOKENS, 2)]          # capped at 14000, then gives up (no endless growth)
    p = Scripted([('{"facts": [', "length")])
    try:
        LLM(S, {"cf_a": p}).complete("verify", MSG, schema=ok, max_tokens=MAX_RETRY_TOKENS)
        raise AssertionError("expected SchemaError")
    except SchemaError:
        pass
    assert p.calls == [(MAX_RETRY_TOKENS, 2)]                      # already at the cap: no second call


def test_an_invalid_answer_that_was_not_cut_off_still_gets_the_old_repair_message():
    p = Scripted([("not json", "stop"), ('{"facts": []}', "stop")])
    LLM(S, {"cf_a": p}).complete("verify", MSG, schema=ok, max_tokens=5000)
    assert p.calls == [(5000, 2), (5000, 4)]                       # same limit, broken answer + correction appended


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_the_run_deadline_queues_instead_of_starting_or_waiting():
    c = Clock()
    p = Scripted([RateLimited(None), RateLimited(None), ('{"facts": []}', "stop")])
    llm = LLM(S, {"cf_a": p}, sleep=c.sleep, clock=c)
    llm.set_deadline(c.t + 50)                                      # 50 s left: a 10 s wait + 45 s reserve does not fit
    try:
        llm.complete("verify", MSG, schema=ok)
        raise AssertionError("expected BudgetExhausted")
    except BudgetExhausted:
        pass
    assert len(p.calls) == 1 and c.t == 1000.0                      # one try, no sleeping past the limit
    llm.set_deadline(c.t + 30)
    n = len(p.calls)
    try:
        llm.complete("verify", MSG, schema=ok)                      # < 45 s left: not even started
        raise AssertionError("expected BudgetExhausted")
    except BudgetExhausted:
        pass
    assert len(p.calls) == n and not llm.has_time(60)
    llm.set_deadline(None)
    assert llm.has_time(10 ** 9)


def test_verify_asks_for_room_in_proportion_to_the_claims():
    import tempfile
    from newsbot.models import Claim
    from newsbot.verify import build_fact_sheet
    seen = []

    class Fake:
        def complete(self, task, msgs, schema=None, *, max_tokens=1024):
            seen.append(max_tokens)
            return []
    claims = lambda n: [Claim(f"S1.C{i}", "S1", "what", f"claim number {i}") for i in range(n)]   # noqa: E731
    with tempfile.TemporaryDirectory() as t:
        for n in (10, 200, 1000):
            try:
                build_fact_sheet(Fake(), t, "st-1", [], claims(n))
            except Exception:
                pass                                                # only the request matters here
    assert seen == [4950, 13500, 14000]                             # 4500 + 45/claim, capped at 14000


def test_long_answers_get_a_long_enough_http_timeout():
    """Real log 2026-10-31: verify (max_tokens ~9000) failed on BOTH accounts with ReadTimeout after exactly 60 s."""
    from newsbot.llm.workers_ai import WorkersAIProvider, chat_timeout
    assert chat_timeout(1024) < chat_timeout(5000) <= chat_timeout(9200) == 240.0 == chat_timeout(14000)
    assert chat_timeout(1024) >= 80                                  # short calls keep a comfortable margin too
    seen = []

    def transport(method, url, *, headers=None, json=None, timeout=60):
        seen.append(timeout)
        return 200, {}, {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    p = WorkersAIProvider("a" * 32, "tok", transport=transport, name="cf_a")
    p.chat("m", [{"role": "user", "content": "hi"}], max_tokens=9200)
    p.chat("m", [{"role": "user", "content": "hi"}], max_tokens=1000)
    assert seen == [240.0, 85.0]


# ---- repair edits the rejected draft instead of rewriting from scratch; thin fact sheets are not written ----
def test_repair_request_carries_the_rejected_draft_and_asks_for_minimal_changes():
    import copy
    from newsbot.llm import build_llm, load_llm_settings
    from newsbot.llm.dryrun import dry_run_provider
    from newsbot.articles import produce_articles
    from tests.helpers import TEMPLATE, make_cfg
    from tests.test_m4 import STORIES
    mock = dry_run_provider()
    good = mock.responses["write_en"]
    seen = []

    def write(msgs):
        art = copy.deepcopy(good(msgs))
        seen.append([m["role"] for m in msgs])
        if len(seen) == 1:
            art["body_markdown"] = art["body_markdown"].replace("\n\n## ", "\n\nZorblax Quimby opened it in 2014 [S1].\n\n## ", 1)
        return art
    mock.responses["write_en"] = write
    llm = build_llm(load_llm_settings(TEMPLATE), None, dry_run=True, mock=mock)
    r = produce_articles(STORIES["ev-metro"], ["en"], make_cfg(), None, llm, recent_by_lang={"en": []})["en"]
    assert r.qa.failures == [] and "Zorblax" not in r.article["body_markdown"]
    assert seen[0][-1] == "user" and "assistant" not in seen[0]                    # first write: no draft
    assert seen[1][-2:] == ["assistant", "user"]                                    # repair: draft, then the correction note


def test_write_article_adds_the_draft_only_when_repairing():
    from newsbot.write import write_article
    from tests.helpers import make_cfg
    from tests.test_m4 import STORIES, base
    got = []

    class Spy:
        def complete(self, task, msgs, schema=None, *, max_tokens=1024):
            got.append(msgs)
            return base()
    cfg, fs = make_cfg(), STORIES["ev-metro"]
    write_article(Spy(), None, cfg, fs, "en")
    write_article(Spy(), None, cfg, fs, "en", fix="QA REJECTED", draft={"title": "Old draft title"})
    assert all(m["role"] != "assistant" for m in got[0])
    assert got[1][-2]["role"] == "assistant" and "Old draft title" in got[1][-2]["content"]
    assert got[1][-1]["content"].startswith("QA REJECTED") and "keep every other sentence" in got[1][-1]["content"]


def test_a_story_with_too_few_facts_is_not_written_and_waits_for_more_sources():
    import shutil
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace as NS
    from newsbot.cli import FIXTURES, run_pipeline
    from newsbot.discover import fixture_fetcher
    from newsbot.fetch import fixture_article_fetcher
    from tests.helpers import NOW, TEMPLATE, make_cfg
    from tests.test_publish import _llm
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        brain = Path(d) / "brain"
        shutil.copytree(TEMPLATE, brain)
        kw = dict(embedder=None, article_fetcher=fixture_article_fetcher(FIXTURES / "articles"),
                  feed_fetcher=fixture_fetcher(FIXTURES / "feeds", cfg.sources))
        ok = run_pipeline(cfg, "review", brain, now=NOW, llm=_llm(), **kw)
        assert ok["articles"]                                                       # the fixtures normally produce articles
        shutil.rmtree(brain)
        shutil.copytree(TEMPLATE, brain)
        cfg.research = NS(**{**vars(cfg.research), "min_facts_to_write": 10_000}) if hasattr(cfg, "research") else NS(min_facts_to_write=10_000)
        thin = run_pipeline(cfg, "review", brain, now=NOW, llm=_llm(), **kw)
        assert not thin["articles"]                                                 # nothing was written, no LLM write cost
        assert any("facts:insufficient_sources" in x.reason for x in thin["decisions"])
