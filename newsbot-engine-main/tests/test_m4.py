"""M4 tests: writers, QA gates, sensitive topics, outputs, eval tool. Offline, zero-arg (also run under pytest)."""
import copy
import tempfile
from pathlib import Path

from newsbot.articles import ArticleResult, load_recent_articles, produce_articles, write_outputs
from newsbot.evaluate import format_table, load_stories, run_eval
from newsbot.llm import LLM, BudgetExhausted, SchemaError, build_llm, load_llm_settings
from newsbot.llm.dryrun import dry_run_provider
from newsbot.llm.mock import MockProvider
from newsbot.qa import bn_fluency_flags, run_gates, validate_qa_check
from newsbot.sensitive import DEFAULT_KEYWORDS, detect
from newsbot.write import sanitize_markdown, slugify, to_markdown_file, validate_article, write_article
from tests.helpers import NOW, TEMPLATE, make_cfg

CFG = make_cfg()
STORIES = {fs.story_id: fs for fs in load_stories()}


def dry_llm():
    return build_llm(load_llm_settings(TEMPLATE), None, dry_run=True, mock=dry_run_provider())


def base(story="ev-metro", lang="en"):
    art, _ = write_article(dry_llm(), None, CFG, STORIES[story], lang)
    return art


def gates(art, story="ev-metro", lang="en", cfg=CFG, **kw):
    kw.setdefault("run_llm_check", False)
    return run_gates(art, STORIES[story], lang, cfg, recent=kw.pop("recent", []), **kw)


def with_body(art, extra_para):
    """Append a paragraph to the end of the first ('what happened') section, in either language."""
    a = copy.deepcopy(art)
    a["body_markdown"] = a["body_markdown"].replace("\n\n## ", f"\n\n{extra_para}\n\n## ", 1)
    assert extra_para in a["body_markdown"]
    return a


# ---------------- writer ----------------

def test_validate_article_schema():
    good = base()
    assert validate_article(good)["slug"] == good["slug"]
    for k in ("title", "body_markdown", "timeline", "sources"):
        bad = {x: y for x, y in good.items() if x != k}
        try:
            validate_article(bad)
        except ValueError:
            continue
        raise AssertionError(f"missing {k} accepted")
    try:
        validate_article({**good, "confirmed": "not a list"})
        raise AssertionError("bad list accepted")
    except ValueError:
        pass


def test_sanitize_markdown_strips_html_and_foreign_links():
    out, n = sanitize_markdown('Hi <script>alert(1)</script><b>bold</b> [x](http://evil.test) [ok](https://a.com/p) '
                               'see http://evil.test/z and https://a.com/p ![i](http://evil.test/i.png)', {"https://a.com/p"})
    assert "evil" not in out and "<" not in out and "alert" not in out and "[ok](https://a.com/p)" in out and n >= 5


def test_writer_overwrites_source_metadata_from_fact_sheet():
    fs = STORIES["ev-metro"]
    mock = MockProvider({"write_en": lambda m: {**dry_run_provider().responses["write_en"](m),
                                                "sources": [{"id": "S1", "name": "Fake", "tier": 1, "url": "https://evil.test"}]}})
    s = load_llm_settings(TEMPLATE)
    art, _ = write_article(build_llm(s, None, dry_run=True, mock=mock), None, CFG, fs, "en")
    assert art["sources"] == [{k: fs.sources[0][k] for k in ("id", "name", "tier", "url", "published_at")}]


def test_slugify_is_ascii_and_falls_back_to_story_id():
    assert slugify("Hello, World!", "st-1") == "hello-world"
    assert slugify("ঢাকা মেট্রো", "st-1") == "st-1"


def test_writer_gets_untrusted_blocks_and_only_own_language_sources():
    mock = dry_run_provider()
    seen = []
    orig = mock.chat
    mock.chat = lambda model, messages, **kw: (seen.append(messages), orig(model, messages, **kw))[1]
    write_article(build_llm(load_llm_settings(TEMPLATE), None, dry_run=True, mock=mock), None, CFG, STORIES["ev-metro"], "bn")
    user = seen[0][-1]["content"]
    assert user.count("<fact_sheet>") == 1 and user.count("<source_data") == 1       # only the bn source (S3)
    assert 'id="S3"' in user and "কমলাপুর" in user and "adds two stations and is expected" not in user      # English source text is not shown to the bn writer
    assert "bn" in seen[0][0]["content"] and "Bangladesh" in seen[0][0]["content"]


# ---------------- gates: a good article passes ----------------

def test_dry_run_article_passes_all_hard_gates_en_and_bn():
    for lang in ("en", "bn"):
        r = gates(base("ev-metro", lang), lang=lang)
        assert r.failures == [] and r.review == [] and r.disposition == "publish", (lang, r.failures, r.review)


def test_full_gates_with_llm_check_publish():
    llm = dry_llm()
    r = run_gates(base(), STORIES["ev-metro"], "en", CFG, recent=[], llm=llm, brain=None)
    assert r.disposition == "publish" and any(c["task"] == "qa_check" for c in llm.providers["mock"].calls)


def test_one_short_quote_per_source_is_allowed():
    q = 'Officials said the section "adds two stations and is expected to carry about 60,000 more riders" [S1].'
    assert gates(with_body(base(), q)).failures == []


# ---------------- gates: broken articles fail ----------------

def test_missing_and_unresolved_markers_fail():
    a = copy.deepcopy(base())
    a["body_markdown"] = a["body_markdown"].replace("[S1][S2][S3]", "", 1)
    assert "missing_marker" in gates(a).failures
    assert "unresolved_marker" in gates(with_body(base(), "Another fact [S9].")).failures


def test_invented_number_and_name_fail():
    assert "unsupported_number" in gates(with_body(base(), "A total of 777 riders were counted [S1].")).failures
    assert "unsupported_name" in gates(with_body(base(), "The minister Zorblax Quimby said so [S1].")).failures


def test_bengali_digits_normalised_for_number_check():
    a = with_body(base("ev-metro", "bn"), "নতুন অংশটি ৩.৯ কিলোমিটার দীর্ঘ [S3]।")
    assert gates(a, lang="bn").failures == []
    assert "unsupported_number" in gates(with_body(base("ev-metro", "bn"), "মোট ৭৭৭ জন [S3]।"), lang="bn").failures


def test_length_title_slug_category_gates():
    a = copy.deepcopy(base())
    a["body_markdown"] = "## What happened\n\nShort text [S1].\n\n## Context and impact\n\nMore."
    assert "min_words" in gates(a).failures
    a = {**base(), "title": "Too short"}
    assert "title_length" in gates(a).failures
    assert "slug" in gates({**base(), "slug": "Bad Slug!"}).failures
    assert "category" in gates({**base(), "category": "astrology"}).failures


def test_sources_must_have_been_fetched():
    a = copy.deepcopy(base())
    a["sources"][0]["url"] = "https://example.test/never-fetched"
    assert "source_not_fetched" in gates(a).failures
    a = copy.deepcopy(base())
    a["sources"].append({"id": "S8", "name": "X", "tier": 1, "url": "https://x.test", "published_at": ""})
    assert "source_not_fetched" in gates(a).failures


def test_copying_source_sentences_fails_ngram_gate():
    copied = "The new 3.9 km section adds two stations and is expected to carry about 60,000 more riders a day [S1]."
    strict = make_cfg(qa={"max_ngram_overlap": 0.02})
    assert "ngram_overlap" in gates(with_body(base(), copied), cfg=strict).failures
    assert gates(base(), cfg=strict).failures == []


def test_quote_rules():
    long = 'It said "The new 3.9 km section adds two stations and is expected to carry about 60,000 more riders a day" [S1].'
    assert "quote_too_long" in gates(with_body(base(), long)).failures
    assert "quote_not_in_sources" in gates(with_body(base(), 'He said "a completely invented sentence appears here" [S1].')).failures
    two = 'Quote one "adds two stations and is expected" [S1]. Quote two "Fares on the extension start at 20 taka" [S1].'
    assert "quote_repeat_source" in gates(with_body(base(), two)).failures


def test_boilerplate_html_and_links_fail():
    assert "banned_phrase" in gates(with_body(base(), "In conclusion, the line opened [S1].")).failures
    assert "raw_html" in gates(with_body(base(), "The line <b>opened</b> [S1].")).failures
    assert "unlisted_link" in gates(with_body(base(), "More at https://evil.test/x [S1].")).failures
    assert "placeholder" in gates(with_body(base(), "The line opened [insert detail] [S1].")).failures


def test_status_consistency_gates():
    a = {**base(), "confirmed": ["About 60,000 more riders a day are expected [S1]."]}     # 60,000 is only 'reported'
    assert "confirmed_unsupported_number" in gates(a).failures
    nc = copy.deepcopy(STORIES["ev-metro"])
    assert "disputed_omitted" in run_gates({**base("ev-wage"), "disputed": []}, STORIES["ev-wage"], "en", CFG, run_llm_check=False).failures
    assert gates(base("ev-wage"), "ev-wage").failures == [] and base("ev-wage")["disputed"]
    assert nc is not None


def test_duplicates_against_recent_articles():
    a = base()
    assert "duplicate_title" in gates(a, recent=[{"story_id": "other", "title": a["title"], "slug": "zzz"}]).failures
    assert "duplicate_slug" in gates(a, recent=[{"story_id": "other", "title": "Totally different words here", "slug": a["slug"]}]).failures
    assert gates(a, recent=[{"story_id": "ev-metro", "title": a["title"], "slug": a["slug"]}]).failures == []   # update of same story
    same = lambda texts: [[1.0, 0.0] for _ in texts]                                                            # noqa: E731
    r = gates(a, recent=[{"story_id": "o", "title": "unrelated headline words", "slug": "q"}], embedder=same)
    assert "duplicate_embedding" in r.failures


def test_llm_qa_check_hits_fail_and_outages_go_to_review():
    hit = LLM({"tasks": {"qa_check": "t"}, "tiers": {"t": [{"provider": "mock", "model": "m"}]}},
              {"mock": MockProvider({"qa_check": {"unsupported": [{"claim": "Riders doubled", "reason": "not in sheet"}]}})})
    r = run_gates(base(), STORIES["ev-metro"], "en", CFG, recent=[], llm=hit)
    assert "unsupported_claims" in r.failures and r.details["unsupported_claims"]["unsupported"] == ["Riders doubled"]
    assert r.disposition == "pending"

    class Out:
        def complete(self, *a, **k):
            raise BudgetExhausted("x")
    r = run_gates(base(), STORIES["ev-metro"], "en", CFG, recent=[], llm=Out())
    assert r.failures == [] and r.review == ["qa_check_unavailable"] and r.disposition == "pending"
    assert run_gates(base(), STORIES["ev-metro"], "en", CFG, recent=[]).review == ["qa_check_not_run"]
    assert validate_qa_check({"unsupported": ["a", {"claim": "b"}]}) == ["a", "b"]


def test_schema_failure_short_circuits():
    r = run_gates({"title": "x"}, STORIES["ev-metro"], "en", CFG)
    assert r.failures == ["schema"] and r.disposition == "pending"


# ---------------- review (sensitive / low confidence) ----------------

def test_sensitive_detection_en_bn_and_config_override():
    assert detect("Twelve people died after the storm") == ["death"]
    assert detect("ঘূর্ণিঝড়ে ১৫ জনের মৃত্যু") == ["death"]
    assert "crime" in detect("Police arrested two suspects") and "election" in detect("The ballot count was disputed")
    assert detect("The metro extension opened on Thursday") == []
    assert detect("diet and cured meats") == []                       # whole-word matching: 'die' != 'diet'
    assert detect("rice prices", {"food": ["rice"]}) == ["food"]


def test_default_keywords_match_the_brain_template():
    import yaml
    d = yaml.safe_load((TEMPLATE / "config" / "newsbot.yaml").read_text(encoding="utf-8"))
    assert d["sensitive"]["keywords"] == DEFAULT_KEYWORDS


def test_sensitive_article_goes_to_pending_not_failure():
    cfg = make_cfg(sensitive={"keywords": {"transport": ["metro"]}})
    r = gates(base(), cfg=cfg)
    assert r.failures == [] and r.review == ["sensitive:transport"] and r.disposition == "pending"


def test_low_confidence_goes_to_pending():
    cfg = make_cfg(qa={"min_confirmed_facts": 5})
    r = gates(base(), cfg=cfg)
    assert r.failures == [] and "low_confidence" in r.review


# ---------------- orchestration and outputs ----------------

def test_dry_run_pipeline_writes_article_and_parks_sensitive_story():
    from newsbot.cli import run_pipeline
    with tempfile.TemporaryDirectory() as d:
        r = run_pipeline(make_cfg(), "dry-run", Path(d), now=NOW)
        (sid, res), = r["articles"].items()
        a = res["en"]
        assert a.qa.failures == [] and a.qa.review == ["sensitive:death"] and a.status == "pending"   # 12 people died
        assert "en:pending" in next(x for x in r["decisions"] if x.story_id == sid).reason
        assert not (Path(d) / "pending").exists()          # dry-run writes nothing


def test_outputs_publish_to_content_and_pending_with_reasons_then_duplicate_blocked():
    fs = STORIES["ev-metro"]
    with tempfile.TemporaryDirectory() as d:
        res = produce_articles(fs, ["en", "bn"], CFG, None, dry_llm(), recent_by_lang={"en": [], "bn": []})
        assert {l: r.status for l, r in res.items()} == {"en": "publish", "bn": "publish"}
        paths = write_outputs(d, fs, res, NOW)
        assert sorted(paths) == sorted(f"content/{l}/{res[l].article['slug']}.md" for l in ("en", "bn"))
        text = (Path(d) / paths[0]).read_text(encoding="utf-8")
        assert text.startswith("---\n") and "translationKey: ev-metro" in text and "ai_assisted: true" in text
        recent = load_recent_articles(d, "en")
        assert recent and recent[0]["story_id"] == "ev-metro"
        res2 = produce_articles(STORIES["ev-policy-rate"], ["en"], CFG, None, dry_llm(), recent_by_lang={"en": recent})
        assert res2["en"].status in ("publish", "pending")      # different story is not a duplicate of the metro one
        # a bad draft is parked in pending/ with its reasons
        bad = ArticleResult("en", "pending", {**res["en"].article, "slug": "bad-draft"}, gates(with_body(base(), "Total 777 [S1].")))
        write_outputs(d, fs, {"en": bad}, NOW)
        p = (Path(d) / "pending" / "en" / "bad-draft.md").read_text(encoding="utf-8")
        assert "unsupported_number" in p and "failures:" in p


def test_budget_and_failures_are_per_language():
    class NoBudget:
        def can_run(self, task, est):
            return task != "write_bn"
        def complete(self, task, msgs, schema=None, **kw):
            return dry_run_provider().responses[task](msgs) if schema is validate_article else {"unsupported": []}
    res = produce_articles(STORIES["ev-metro"], ["bn", "en"], CFG, None, NoBudget(), recent_by_lang={"en": [], "bn": []})
    assert res["bn"].status == "queued_budget" and res["bn"].article is None and res["en"].status in ("publish", "pending")

    class Broken(NoBudget):
        def can_run(self, *a):
            return True
        def complete(self, *a, **k):
            raise SchemaError("bad")
    assert produce_articles(STORIES["ev-metro"], ["en"], CFG, None, Broken())["en"].status == "write_failed"


def test_markdown_file_front_matter_is_yaml_with_hugo_keys():
    import yaml
    text = to_markdown_file(base(), STORIES["ev-metro"], "en", NOW)
    fm = yaml.safe_load(text.split("---")[1])
    for k in ("title", "slug", "translationKey", "date", "lastmod", "categories", "timeline", "confirmed",
              "unclear", "disputed", "sources", "corrections", "ai_assisted"):
        assert k in fm
    assert fm["translationKey"] == "ev-metro" and fm["lang"] == "en"


# ---------------- eval tool ----------------

def test_eval_tool_dry_run_table():
    rows = run_eval(CFG, None, list(STORIES.values()), [("write_bn", "mock", "m"), ("write_en", "mock", "m")],
                    {"mock": dry_run_provider()})
    assert [r.stories for r in rows] == [10, 10] and all(r.passed == 10 for r in rows)
    table = format_table(rows)
    assert "gates" in table and "100%" in table and "tok/article" in table
    assert rows[0].flagged == 10 and rows[1].flagged == 0         # the mock's Bengali mixes English, so it is flagged
    assert bn_fluency_flags("এটি একটি সুন্দর বাক্য। আরেকটি বাক্য এখানে আছে।") == []
    assert "latin_text" in bn_fluency_flags("This is mostly English text without any Bengali at all।")
