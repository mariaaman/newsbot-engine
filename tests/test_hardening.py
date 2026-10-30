"""Pre-production hardening: crawler identity, safe redirects, symmetric duplicate check, pending triage."""
import os
from types import SimpleNamespace

from newsbot import netsafe
from tests.helpers import make_cfg
from tests.test_m4 import base, gates


def test_agent_uses_site_url_when_contact_not_set():
    old = os.environ.pop("NEWSBOT_CONTACT_URL", None)
    try:
        assert "example.invalid" in netsafe.user_agent()
        netsafe.set_contact_from_config(SimpleNamespace(site=SimpleNamespace(base_url="https://news.example.org")))
        assert "example.invalid" in netsafe.user_agent()                      # placeholder-looking URLs are ignored
        netsafe.set_contact_from_config(SimpleNamespace(site=SimpleNamespace(base_url="https://mynews.org/")))
        assert netsafe.user_agent().endswith("(+https://mynews.org/)")
        netsafe.set_contact_from_config(SimpleNamespace(site=SimpleNamespace(base_url="https://other.org")))
        assert "mynews.org" in netsafe.user_agent()                           # an existing value is never overwritten
    finally:
        os.environ.pop("NEWSBOT_CONTACT_URL", None)
        if old:
            os.environ["NEWSBOT_CONTACT_URL"] = old


def test_blocked_schemes_and_private_hosts():
    for bad in ("file:///etc/passwd", "ftp://a.org/x", "http://localhost/x", "http://127.0.0.1/x",
                "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/x", "http://[::1]/x", "https:///nohost"):
        try:
            netsafe.check_url(bad)
        except OSError:
            continue
        raise AssertionError(f"{bad} was allowed")
    netsafe.check_url("https://news.example.org/a", resolver=lambda h: True)      # public name passes


class _Resp:
    def __init__(self, status, headers=None, chunks=(b"",)):
        self.status_code, self.headers, self._c = status, headers or {}, chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        return iter(self._c)


class _Session:
    def __init__(self, plan):
        self.plan, self.seen = plan, []

    def get(self, url, **kw):
        self.seen.append(url)
        assert kw["allow_redirects"] is False and "NewsBotEngine" in kw["headers"]["User-Agent"]
        return self.plan[url]


def test_redirects_are_checked_per_hop_and_bodies_capped():
    ok = lambda host: host != "evil.internal"                                      # noqa: E731
    s = _Session({"https://a.org/x": _Resp(302, {"location": "/y"}),
                  "https://a.org/y": _Resp(200, {"content-type": "text/html"}, (b"x" * 70000, b"y" * 70000))})
    status, ctype, body = netsafe.safe_get("https://a.org/x", 5, 100000, session=s, resolver=ok)
    assert (status, ctype, len(body)) == (200, "text/html", 100000) and s.seen == ["https://a.org/x", "https://a.org/y"]
    s = _Session({"https://a.org/x": _Resp(301, {"location": "http://evil.internal/secret"})})
    try:
        netsafe.safe_get("https://a.org/x", 5, 1000, session=s, resolver=ok)
        raise AssertionError("redirect into a blocked host was followed")
    except OSError as e:
        assert str(e) == "blocked_host"
    loop = _Session({"https://a.org/x": _Resp(302, {"location": "https://a.org/x"})})
    try:
        netsafe.safe_get("https://a.org/x", 5, 1000, session=loop, resolver=ok)
        raise AssertionError("redirect loop not stopped")
    except OSError as e:
        assert str(e) == "too_many_redirects"


def test_duplicate_embedding_compares_title_and_summary_on_both_sides():
    a = base()
    seen = []

    def emb(texts):
        seen.extend(texts)
        return [[1.0, 0.0] for _ in texts]
    r = gates(a, recent=[{"story_id": "o", "title": "unrelated headline words", "slug": "q",
                          "summary": "Another summary sentence."}], embedder=emb)
    assert "duplicate_embedding" in r.failures
    assert seen[1] == "unrelated headline words. Another summary sentence."      # not the bare title
    seen.clear()
    gates(a, recent=[{"story_id": "o", "title": "old entry without summary", "slug": "q"}], embedder=emb)
    assert seen[1] == "old entry without summary"                                  # old entries still work


def test_recent_articles_carry_the_summary():
    import tempfile
    from pathlib import Path
    from newsbot.articles import load_recent_articles
    with tempfile.TemporaryDirectory() as t:
        d = Path(t) / "content" / "en"
        d.mkdir(parents=True)
        (d / "a.md").write_text("---\ntitle: A title\nsummary: A summary.\nslug: a\nstory_id: st-1\n---\nbody\n", encoding="utf-8")
        got = load_recent_articles(t, "en")
        assert got and got[0]["summary"] == "A summary."


def test_category_synonyms_map_only_to_site_categories():
    from newsbot.write import map_category
    site = ["bangladesh", "world", "politics", "economy", "technology", "sports"]
    assert map_category("Business", site) == "economy" and map_category("sport", site) == "sports"
    assert map_category("world", site) == "world"                      # already valid: untouched
    assert map_category("legal", site) == "legal" and map_category("national", site) == "national"   # still fails the gate
    assert map_category("business", ["world"]) == "business"           # target not offered by this site


# ---- multi-attempt repair (names, years, ages, length ...) -> gates again -> publish when clean
def _writer(bad_calls, bad):
    """Mock writer that damages its first `bad_calls` answers with `bad(art)` and then answers cleanly."""
    import copy
    from newsbot.llm.dryrun import dry_run_provider
    calls = []
    good = dry_run_provider().responses["write_en"]

    def write(msgs):
        art = copy.deepcopy(good(msgs))
        calls.append(any("QA REJECTED" in m["content"] for m in msgs if m["role"] == "user"))
        return bad(art) if len(calls) <= bad_calls else art
    return write, calls


def _produce_with(writer, **qa):
    from newsbot.llm import build_llm, load_llm_settings
    from newsbot.llm.dryrun import dry_run_provider
    from newsbot.articles import produce_articles
    from tests.helpers import TEMPLATE
    from tests.test_m4 import STORIES
    mock = dry_run_provider()
    mock.responses["write_en"] = writer
    llm = build_llm(load_llm_settings(TEMPLATE), None, dry_run=True, mock=mock)
    return produce_articles(STORIES["ev-metro"], ["en"], make_cfg(qa=qa), None, llm, recent_by_lang={"en": []})["en"]


def test_second_repair_attempt_cures_a_wrong_name_and_a_wrong_year():
    def bad(art):
        art["body_markdown"] = art["body_markdown"].replace("\n\n## ", "\n\nZorblax Quimby opened it in 2014 [S1].\n\n## ", 1)
        return art
    w, calls = _writer(2, bad)                      # first write and first repair are still wrong
    r = _produce_with(w)
    assert calls == [False, True, True]
    assert r.qa.failures == [] and "Zorblax" not in r.article["body_markdown"] and "2014" not in r.article["body_markdown"]


def test_one_attempt_is_not_enough_when_the_first_repair_is_still_wrong():
    def bad(art):
        art["body_markdown"] = art["body_markdown"].replace("\n\n## ", "\n\nZorblax Quimby opened it [S1].\n\n## ", 1)
        return art
    w, calls = _writer(2, bad)
    r = _produce_with(w, max_repair_attempts=1)
    assert calls == [False, True] and "unsupported_name" in r.qa.failures and r.status == "pending"


def test_a_too_short_draft_is_rewritten_longer():
    def short(art):
        art["body_markdown"] = "## Short\n\nThe line opened [S1]."
        return art
    w, calls = _writer(1, short)
    r = _produce_with(w)
    assert calls == [False, True] and "min_words" not in r.qa.failures


def test_repair_note_names_every_failed_gate_and_the_limits():
    from types import SimpleNamespace
    from newsbot.articles import repair_note
    from newsbot.qa import QAResult
    from tests.test_m4 import STORIES, base
    cfg = make_cfg()
    qa = QAResult(failures=["min_words", "title_length", "category", "quote_not_in_sources", "missing_marker",
                            "ngram_overlap", "unsupported_claims"],
                  details={"min_words": {"words": 120}, "unsupported_claims": {"unsupported": [{"claim": "Metro closed"}]}})
    note = repair_note(qa.failures, base(), STORIES["ev-metro"], "en", cfg, qa)
    assert note.startswith("QA REJECTED YOUR DRAFT") and "120 words" in note and "at least 500 words" in note
    assert "20 to 100 characters" in note and "one of: bangladesh" in note and "Metro closed" in note
    assert "citation marker" in note and "Rephrase" in note and "age, date and year" in note
    assert SimpleNamespace  # (import kept for clarity of the test's intent)
