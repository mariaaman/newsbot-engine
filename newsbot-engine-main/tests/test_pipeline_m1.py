"""M1 tests: discovery, clustering, scoring, gate (NEW / UPDATE / SKIP and caps). Offline, no LLM."""
import tempfile
from datetime import timedelta

from newsbot.cluster import cluster_items, tokenize, top_keywords
from newsbot.discover import discover, fixture_fetcher, parse_trends
from newsbot.gate import decide
from newsbot.models import Story, iso
from newsbot.score import score_all
from newsbot.state import (append_decisions, load_stories, local_day_start, new_today,
                           save_stories)
from tests.helpers import FEEDS, NOW, make_cfg, mock_embedder


def run(cfg, known=None, embedder=None, new_count=0):
    known = known or {}
    items, trends, stats = discover(cfg, fixture_fetcher(FEEDS, cfg.sources))
    clusters = cluster_items(items, cfg, embedder, known)
    scored = score_all(clusters, trends, cfg, NOW)
    day = local_day_start(NOW, cfg.site.timezone_offset_hours)
    return scored, decide(scored, known, cfg, NOW, day, new_count), stats, clusters


def by_title(decisions, scored, needle):
    for s, d in zip(scored, decisions):
        if any(needle in i.title for i in s.cluster.items):
            return d, s
    raise AssertionError(needle)


# ---- discovery
def test_discover_parses_all_feeds_and_google_news_domains():
    cfg = make_cfg()
    items, trends, stats = discover(cfg, fixture_fetcher(FEEDS, cfg.sources))
    assert stats["feeds_failed"] == 0 and stats["items"] == len(items) == 16
    gn = [i for i in items if i.kind == "google_news" and "প্রথম আলো" not in i.title]
    jug = next(i for i in items if i.domain == "jugantor.com")
    assert jug.tier == 3                      # unknown publisher stays untrusted
    pa = next(i for i in items if i.kind == "google_news" and i.domain == "prothomalo.com")
    assert pa.tier == 2                       # tiered from the trusted feed list
    assert "প্রথম আলো" not in pa.title        # publisher suffix stripped
    assert [t.keyword for t in trends][:3] == ["cyclone barun", "asia cup", "bangladesh bank rate"]
    assert trends[0].traffic == 500_000 and gn


def test_discover_tolerates_a_failing_feed():
    cfg = make_cfg()
    good = fixture_fetcher(FEEDS, cfg.sources)

    def flaky(url):
        if "aljazeera" in url:
            raise OSError("boom")
        return good(url)
    items, _, stats = discover(cfg, flaky)
    assert stats["feeds_failed"] == 1 and stats["feeds_ok"] == len(cfg.sources) - 1 and items


def test_parse_trends_handles_empty():
    assert parse_trends(b'<rss version="2.0"><channel></channel></rss>') == []


# ---- tokenizer / cluster
def test_bengali_tokens_and_digits():
    assert "120" in tokenize("বেগে ১২০ কিমি") and "বেগে" in tokenize("বেগে ১২০ কিমি")


def test_lexical_clusters_within_language():
    cfg = make_cfg()
    *_, clusters = run(cfg)
    sizes = sorted(len(c.items) for c in clusters)
    langs_in_big = [{i.lang for i in c.items} for c in clusters if len(c.items) > 2]
    assert all(len(l) == 1 for l in langs_in_big)           # no cross-language link without embeddings
    assert sizes.count(1) >= 2


def test_embeddings_link_languages():
    cfg = make_cfg()
    *_, clusters = run(cfg, embedder=mock_embedder)
    cyc = [c for c in clusters if any("Barun" in i.title or "বরুণ" in i.title for i in c.items)]
    assert len(cyc) == 1 and {i.lang for i in cyc[0].items} == {"bn", "en"}


# ---- scoring + gate
def test_gate_new_skip_and_blocked():
    cfg = make_cfg()
    scored, dec, stats, _ = run(cfg)
    cyc, _ = by_title(dec, scored, "Cyclone Barun makes landfall")
    bank, _ = by_title(dec, scored, "raises policy rate")
    gossip, _ = by_title(dec, scored, "gossip")
    school, _ = by_title(dec, scored, "স্কুলগুলো")
    bn_cyc, _ = by_title(dec, scored, "বরুণ")
    assert (cyc.action, cyc.route) == ("NEW", ["en"])
    assert (bank.action, bank.route) == ("NEW", ["en"])
    assert cyc.score > bank.score >= cfg.scoring.threshold
    assert gossip.reason == "blocked_topic" and school.reason == "too_few_trusted_sources"
    assert bn_cyc.action == "SKIP" and bn_cyc.reason == "too_few_trusted_sources"
    assert all(set(d.components) >= {"velocity", "breadth", "trends", "recency"} for d in dec)


def test_embedding_routes_story_to_both_languages():
    cfg = make_cfg()
    scored, dec, *_ = run(cfg, embedder=mock_embedder)
    cyc, _ = by_title(dec, scored, "Cyclone Barun makes landfall")
    assert cyc.action == "NEW" and cyc.route == ["bn", "en"]


def test_daily_cap_queues_overflow_and_keeps_best():
    cfg = make_cfg(limits={"max_new_per_day": 1})
    scored, dec, *_ = run(cfg)
    news = [d for d in dec if d.action == "NEW"]
    queued = [d for d in dec if d.queued]
    assert len(news) == 1 and len(queued) == 1 and news[0].score > queued[0].score
    assert queued[0].reason == "daily_cap_reached"
    _, dec0, *_ = run(make_cfg(), new_count=24)
    assert not [d for d in dec0 if d.action == "NEW"]


def test_below_threshold_skips():
    cfg = make_cfg(scoring={"threshold": 5.0})
    _, dec, *_ = run(cfg)
    assert {d.reason for d in dec if d.reason != "blocked_topic"} <= {"below_threshold",
            "too_few_trusted_sources"} and not [d for d in dec if d.action == "NEW"]


def _bank_story(cfg, last_updated_hours_ago, updates=(), urls=None, status="developing"):
    items, *_ = discover(cfg, fixture_fetcher(FEEDS, cfg.sources))
    bank = [i for i in items if "policy rate" in i.title or "interest rate" in i.title]
    ds = next(i for i in bank if i.domain == "thedailystar.net")
    sid = cluster_items(bank, cfg)[0].story_id
    return Story(story_id=sid, status=status, first_seen=iso(NOW - timedelta(hours=30)),
                 last_updated=iso(NOW - timedelta(hours=last_updated_hours_ago)),
                 versions={"en": {"url": "/en/x", "published_at": iso(NOW - timedelta(hours=30))}},
                 urls=urls if urls is not None else [ds.url], keywords=top_keywords(bank),
                 updates=list(updates))


def test_update_rules():
    cfg = make_cfg()

    def action(story):
        scored, dec, *_ = run(cfg, known={story.story_id: story})
        d, _ = by_title(dec, scored, "raises policy rate")
        return d

    d = action(_bank_story(cfg, 4))
    assert d.action == "UPDATE" and d.route == ["en"]
    assert action(_bank_story(cfg, 1)).reason == "too_soon_since_update"
    ups = [iso(NOW.replace(hour=h)) for h in (2, 3, 4)]
    assert action(_bank_story(cfg, 8, updates=ups)).reason == "update_cap_reached"
    all_urls = ["https://www.thedailystar.net/business/d2", "https://www.aljazeera.com/economy/a2",
                "https://www.bbc.com/news/business-2"]
    assert action(_bank_story(cfg, 8, urls=all_urls)).reason == "no_new_confirmed_fact"
    assert action(_bank_story(cfg, 8, status="closed")).reason == "story_closed"


# ---- state
def test_state_roundtrip_and_new_today():
    cfg = make_cfg()
    st = _bank_story(cfg, 4)
    day = local_day_start(NOW, 6)
    assert day == NOW.replace(day=12, hour=18)
    st2 = Story(story_id="st-today", first_seen=iso(NOW - timedelta(hours=1)),
                versions={"en": {"published_at": iso(NOW - timedelta(minutes=30))}})
    with tempfile.TemporaryDirectory() as d:
        save_stories(d, {st.story_id: st, st2.story_id: st2})
        loaded = load_stories(d)
        assert loaded[st.story_id] == st and new_today(loaded, day) == 1
        append_decisions(d, NOW, [{"story_id": "x", "action": "SKIP"}])
        assert "SKIP" in open(d + "/decisions.jsonl", encoding="utf-8").read()
