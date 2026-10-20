"""Offline tests for multi-language discovery and cross-language writing (no pytest or pydantic needed)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from newsbot.cluster import cluster_items, tokenize
from newsbot.discover import discover, parse_feed
from newsbot.extract import numbers_in, validate_claims
from newsbot.fetch import decode_body
from newsbot.gate import decide, route_for
from newsbot.models import Cluster, Item
from newsbot.research import expand_cluster
from newsbot.score import Scored
from newsbot.verify import org_key

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def item(url, domain, lang, title="t", tier=2, hours_ago=1, kind="rss"):
    return Item(title, url, domain, NOW - timedelta(hours=hours_ago), lang, domain, tier, kind)


def cfg(**limits):
    lim = dict(routing_mode="cross_language", routing_min_orgs=2, routing_min_domains_per_language=2,
               min_independent_sources=2, max_new_per_day=24, max_new_per_run=1, max_updates_per_story_per_day=3,
               min_hours_between_updates=2)
    lim.update(limits)
    return NS(limits=NS(**lim), site=NS(languages=["bn", "en"]), scoring=NS(threshold=0.4, per_language_threshold={}))


def scored(sid, doms, score=0.9):
    items = [item(f"https://{d}/{sid}", d, "ja") for d in doms]
    return Scored(Cluster(sid, items), score, {}, False, trusted_domains=set(doms),
                  trusted_orgs={org_key(d) for d in doms}, trusted_by_lang={"ja": set(doms)})


def test_tokenizer_handles_cjk_and_other_scripts():
    t = tokenize("東京で震度5の地震が発生")
    assert "震度" in t and "地震" in t and "5" in t
    assert tokenize("৫ জন, ٣ قتلى, ३ मरे").count("3") == 2 and "5" in tokenize("৫ জন")


def test_numbers_in_many_languages_and_the_crore_bug():
    assert numbers_in("৪ কোটি টাকা") == {"40000000"}
    assert numbers_in("5万人") == {"50000"} and numbers_in("3.5億円") == {"350000000"}
    assert numbers_in("5 करोड़ लोग") == {"50000000"} and numbers_in("٣ ملايين") == {"3000000"}
    assert numbers_in("12 тысяч") == {"12000"} and numbers_in("2 million") == {"2000000"} == numbers_in("2 millions")


def test_validate_claims_respects_the_limit():
    data = {"claims": [{"kind": "what", "text": f"c{i}"} for i in range(60)]}
    assert len(validate_claims(data)) == 40 and len(validate_claims(data, 12)) == 12


def test_org_key_japan_korea_and_aliases():
    assert org_key("www3.nhk.or.jp") == "nhk.or.jp" and org_key("www.kantei.go.jp") == "kantei.go.jp"
    assert org_key("yna.co.kr") == "yna.co.kr" and org_key("abc.net.au") == "abc.net.au"
    assert org_key("www.bbc.co.uk") == org_key("feeds.bbci.co.uk") == org_key("www.bbc.com") == "bbc.com"
    assert org_key("bangla.thedailystar.net") == org_key("www.thedailystar.net")


def test_cross_language_routing_needs_two_independent_orgs_in_any_language():
    c = cfg()
    assert route_for(scored("a", ["nhk.or.jp", "japantimes.co.jp"]), c) == ["bn", "en"]
    assert route_for(scored("b", ["nhk.or.jp"]), c) == []
    assert route_for(scored("c", ["www.bbc.com", "feeds.bbci.co.uk"]), c) == []        # one organisation
    old = cfg(routing_mode="same_language")
    assert route_for(scored("d", ["nhk.or.jp", "japantimes.co.jp"]), old) == []         # no bn/en trusted domains


def test_run_cap_queues_extra_new_stories():
    c = cfg()
    day = NOW - timedelta(hours=12)
    ds = decide([scored("a", ["x.com", "y.com"], 0.9), scored("b", ["p.com", "q.com"], 0.8)], {}, c, NOW, day, 0)
    assert [d.action for d in ds] == ["NEW", "SKIP"] and ds[1].reason == "run_cap_reached" and ds[1].queued


def test_decode_body_uses_the_pages_own_charset():
    sjis = "東京で地震".encode("shift_jis")
    assert decode_body(sjis, "text/html; charset=Shift_JIS") == "東京で地震"
    page = b'<html><head><meta charset="windows-1251"></head>' + "Москва".encode("cp1251")
    assert "Москва" in decode_body(page, "text/html")
    assert decode_body("বাংলা".encode("utf-8"), "text/html") == "বাংলা"


def test_rdf_feed_with_dc_date_is_parsed():
    xml = (b'<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
           b'xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">'
           b'<item><title>Test</title><link>https://www.asahi.com/a</link><dc:date>2026-10-20T10:00:00+09:00</dc:date></item></rdf:RDF>')
    got = parse_feed(xml, NS(kind="rss", tier=2, lang="ja", name="Asahi"))
    assert len(got) == 1 and got[0].domain == "asahi.com" and got[0].lang == "ja"


def test_discover_runs_feeds_in_parallel_keeps_order_caps_per_feed_and_drops_old_items():
    def rss(n, hours_ago):
        its = "".join(f"<item><title>T{i}</title><link>https://s.com/{n}/{i}</link><pubDate>"
                      f"{(NOW - timedelta(hours=hours_ago + i)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
                      for i in range(5))
        return f"<rss><channel>{its}</channel></rss>".encode()
    feeds = [NS(name="A", url="u/a", tier=2, lang="ja", kind="rss"), NS(name="B", url="u/b", tier=2, lang="hi", kind="rss"),
             NS(name="Dead", url="u/dead", tier=2, lang="ar", kind="rss")]
    bodies = {"u/a": rss("a", 1), "u/b": rss("b", 47)}

    def fetch(url):
        if url not in bodies:
            raise OSError("down")
        return bodies[url]
    c = NS(sources=feeds, limits=NS(max_items_per_feed=3, feed_workers=4, max_item_age_hours=48, max_items_per_run=100))
    items, _, stats = discover(c, fetch, NOW)
    assert stats["feeds_failed"] == 1 and stats["feeds_ok"] == 2
    assert len([i for i in items if i.lang == "ja"]) == 3               # per-feed cap
    assert all(NOW - i.published <= timedelta(hours=48) for i in items)   # 47h and 48h ok, 49h+ dropped
    assert {i.lang for i in items} == {"ja", "hi"}


def test_embeddings_link_languages_and_are_exported_for_research():
    items = [item("https://a/1", "nhk.or.jp", "ja", "地震"), item("https://b/1", "bbc.com", "en", "Earthquake"),
             item("https://c/1", "dw.com", "de", "Wetter")]
    vecs = {"地震": [1.0, 0.0, 0.0], "Earthquake": [0.9, 0.1, 0.0], "Wetter": [0.0, 0.0, 1.0]}
    out = {}
    c = NS(clustering=NS(lexical_threshold=0.2, embedding_threshold=0.78, story_match_threshold=0.25, min_shared_tokens=2))
    clusters = cluster_items(items, c, lambda ts: [vecs[t] for t in ts], {}, vec_out=out)
    sizes = sorted(len(k.items) for k in clusters)
    assert sizes == [1, 2] and set(out) == {i.url for i in items}


def test_research_attaches_close_singletons_only():
    base = [item("https://a/1", "nhk.or.jp", "ja"), item("https://b/1", "bbc.com", "en")]
    near, far, taken, untrusted = (item("https://c/1", "dw.com", "de"), item("https://d/1", "rfi.fr", "fr"),
                                   item("https://e/1", "elpais.com", "es"), item("https://f/1", "blog.x", "en", tier=3))
    vec = {"https://a/1": [1.0, 0.0], "https://b/1": [0.98, 0.2], "https://c/1": [0.9, 0.436],
           "https://d/1": [0.0, 1.0], "https://e/1": [0.95, 0.31], "https://f/1": [0.95, 0.31]}
    for k, v in vec.items():                                   # make unit vectors
        n = (v[0] ** 2 + v[1] ** 2) ** 0.5
        vec[k] = [v[0] / n, v[1] / n]
    c = NS(research=NS(attach_threshold=0.9, max_extra_items=5, window_hours=48))
    out, n = expand_cluster(Cluster("st-1", base), base + [near, far, taken, untrusted], vec,
                            {"https://e/1": 3}, c)
    urls = {i.url for i in out.items}
    assert n == 1 and "https://c/1" in urls                    # near: attached
    assert "https://d/1" not in urls                           # far: too different
    assert "https://e/1" not in urls                           # belongs to another story (cluster of 3)
    assert "https://f/1" not in urls                           # tier 3
    assert len(base) == 2                                      # input cluster untouched
    off = NS(research=NS(attach_threshold=0, max_extra_items=5, window_hours=48))
    assert expand_cluster(Cluster("st-1", base), base + [near], vec, {}, off)[1] == 0


# ---- a Japanese + Hindi + English story through fetch -> extract -> verify (fake model, fake pages) ----

JA = "東京都で22日朝、震度5強の地震が発生し、約5万人に避難指示が出された。気象庁によると震源の深さは10キロ。首相は被害の確認を急ぐよう指示した。" * 3
EN = "A magnitude 6.1 earthquake struck Tokyo on Tuesday morning and about 50,000 people were told to evacuate, officials said. " * 4
HI = "टोक्यो में मंगलवार सुबह 6.1 तीव्रता का भूकंप आया और करीब 50,000 लोगों को निकालने का आदेश दिया गया। " * 4


class FakeLLM:
    def can_run(self, task, est):
        return True

    def complete(self, task, msgs, schema=None, max_tokens=0):
        if task == "extract":
            body = msgs[-1]["content"]
            if "震度" in body:
                raw = {"claims": [{"kind": "number", "text": "約5万人に避難指示が出された"},
                                  {"kind": "what", "text": "東京都で22日朝、震度5強の地震が発生"},
                                  {"kind": "number", "text": "震源の深さは99キロ"}]}          # 99: not in the page -> dropped
            elif "टोक्यो" in body:
                raw = {"claims": [{"kind": "number", "text": "करीब 50,000 लोगों को निकालने का आदेश दिया गया"}]}
            else:
                raw = {"claims": [{"kind": "number", "text": "about 50,000 people were told to evacuate"},
                                  {"kind": "what", "text": "A magnitude 6.1 earthquake struck Tokyo on Tuesday morning"}]}
            return schema(raw)
        if task == "verify":
            import json
            import re
            ids = re.findall(r'"id": "(S\d+\.C\d+)", "source": "S\d+", "kind": "number"', msgs[-1]["content"])
            nums = [i for i in ids]
            return schema({"facts": [{"statement": "About 50,000 people were ordered to evacuate Tokyo.",
                                      "claim_ids": nums, "kind": "number"}]})
        raise AssertionError(task)


def test_japanese_hindi_english_story_becomes_a_confirmed_language_neutral_fact():
    from newsbot.facts import gather_facts
    from newsbot.fetch import FetchResult
    pages = {"https://www3.nhk.or.jp/a": JA.encode("shift_jis", "replace"),
             "https://www.bbc.com/news/x": EN.encode(), "https://www.bbc.com/hindi/x": HI.encode()}

    def fetcher(url):
        if url.endswith("/robots.txt"):
            return FetchResult(404, "text/plain", b"")
        ct = "text/html; charset=Shift_JIS" if "nhk" in url else "text/html; charset=utf-8"
        return FetchResult(200, ct, b"<html><body><p>" + pages[url] + b"</p></body></html>")

    its = [item("https://www3.nhk.or.jp/a", "www3.nhk.or.jp", "ja", tier=1), item("https://www.bbc.com/news/x", "www.bbc.com", "en", tier=1),
           item("https://www.bbc.com/hindi/x", "www.bbc.com", "hi", tier=1)]
    c = NS(limits=NS(max_sources_per_story=7), research=NS(source_chars=8000, claims_per_source=40))
    fs, status = gather_facts(Cluster("st-ja", its), c, None, FakeLLM(), fetcher)
    assert status == "ok" and fs is not None
    assert len(fs.sources) == 2                       # NHK + ONE BBC page (bbc.com in two languages is one organisation)
    fact = fs.facts[0]
    assert fact.status == "confirmed" and "50,000" in fact.statement      # 5万人 (ja) == 50,000 (en/hi), two organisations
    texts = " ".join(f.statement for f in fs.facts)
    assert "99" not in texts                          # the invented depth was dropped by the grounding check
    assert "地震" in fs.source_texts["S1"] or "地震" in fs.source_texts.get("S2", "")   # Shift_JIS page decoded correctly


def test_one_refused_source_is_retried_shorter_and_a_dead_one_is_skipped():
    from newsbot.facts import gather_facts
    from newsbot.fetch import FetchResult
    from newsbot.llm import ProviderError
    seen = {"calls": []}

    class Flaky(FakeLLM):
        def complete(self, task, msgs, schema=None, max_tokens=0):
            if task == "extract":
                body = msgs[-1]["content"]
                seen["calls"].append(len(body))
                if "震度" in body and len(body) > 2000:
                    raise ProviderError("http 413")                 # too large for the provider: retried shorter
                if "टोक्यो" in body:
                    raise ProviderError("http 401")                 # dead: skipped, the story goes on
            return super().complete(task, msgs, schema, max_tokens)

    big_ja = "東京都で22日朝、震度5強の地震が発生し、約5万人に避難指示が出された。" * 120
    pages = {"https://www3.nhk.or.jp/a": big_ja.encode(), "https://www.bbc.com/news/x": EN.encode(),
             "https://www.dw.com/hi/x": HI.encode()}

    def fetcher(url):
        if url.endswith("/robots.txt"):
            return FetchResult(404, "text/plain", b"")
        return FetchResult(200, "text/html; charset=utf-8", b"<html><body><p>" + pages[url] + b"</p></body></html>")
    its = [item("https://www3.nhk.or.jp/a", "www3.nhk.or.jp", "ja", tier=1), item("https://www.bbc.com/news/x", "www.bbc.com", "en", tier=1),
           item("https://www.dw.com/hi/x", "www.dw.com", "hi", tier=2)]
    c = NS(limits=NS(max_sources_per_story=7), research=NS(source_chars=8000, claims_per_source=40))
    fs, status = gather_facts(Cluster("st-ja", its), c, None, Flaky(), fetcher)
    assert status == "ok" and any(f.status == "confirmed" for f in fs.facts)
    assert len(seen["calls"]) >= 4 and min(seen["calls"]) < max(seen["calls"])     # the 413 source was re-sent shorter
    all_dead = type("Dead", (FakeLLM,), {"complete": lambda self, t, m, schema=None, max_tokens=0:
                                         (_ for _ in ()).throw(ProviderError("http 413"))})()
    assert gather_facts(Cluster("st-ja", its), c, None, all_dead, fetcher) == (None, "llm_failed")
