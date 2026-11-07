"""M7: update flow (merge, significance, same URL/date, timeline, corrections, review state), stable/closed
transitions, language routing. Offline, zero-arg (also run under pytest). Fixture story: 'ev-metro'."""
import copy
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import yaml

from newsbot.cluster import top_keywords
from newsbot.evaluate import load_stories as load_fact_sheets
from newsbot.facts import load_fact_sheet, save_fact_sheet
from newsbot.fetch import FetchResult
from newsbot.gate import Decision, decide
from newsbot.llm import build_llm, load_llm_settings
from newsbot.llm.dryrun import _write, dry_run_provider
from newsbot.models import Cluster, Fact, FactSheet, Item, Story, iso
from newsbot.publish import reconcile
from newsbot.qa import run_gates
from newsbot.state import load_stories, local_day_start, save_stories
from newsbot.stories import refresh_statuses
from newsbot.update import (finalize_update, load_published, merge_fact_sheets, previous_article, run_updates,
                            fact_sheet_from_article)
from tests.helpers import NOW, TEMPLATE, make_cfg
from tests.test_publish import _record

CFG = make_cfg()
FS = {fs.story_id: fs for fs in load_fact_sheets()}["ev-metro"]
LATER = NOW + timedelta(hours=5)

A1 = "The Kamalapur metro extension carried 61,000 passengers on its first day of service, officials said."
B1 = "Metro operators reported that 61,000 passengers used the Kamalapur extension on its first day of service."
A2 = "Operators announced that late night weekend trains will start next month across both new stations."
B2 = "Officials announced late night weekend trains will begin next month at the two new stations."
PAD = " The report was filed from Dhaka and the transport ministry gave no further comment on Thursday evening."


def mk_llm(**writers):
    mock = dry_run_provider()
    mock.responses.update(writers)
    return build_llm(load_llm_settings(TEMPLATE), None, dry_run=True, mock=mock), mock


def writer(lang="en", **patch):
    base = _write(lang)

    def respond(messages):
        art = base(messages)
        art.update(patch(art) if callable(patch) else patch)
        return art
    return respond


def fetcher(pages: dict):
    def fetch(url):
        if url.endswith("/robots.txt"):
            return FetchResult(404, "text/plain", b"")
        if url not in pages:
            return FetchResult(404, "text/html", b"")
        return FetchResult(200, "text/html", f"<html><body><article><p>{pages[url]}</p></article></body></html>".encode())
    return fetch


def item(domain, url, lang="en", tier=2):
    return Item(title=f"{domain} metro", url=url, domain=domain, published=LATER, lang=lang, source_name=domain,
                tier=tier)


def scored_for(items):
    old = [item(s["domain"], s["url"], s["lang"], s["tier"]) for s in FS.sources]
    return [SimpleNamespace(cluster=Cluster(FS.story_id, old + items, True))]


def brain_with_story(d, route=("en", "bn")):
    _record(d, "auto", route)
    save_fact_sheet(Path(d) / "state", FS)
    return Path(d)


def update_decision(route=("en", "bn")):
    return Decision(FS.story_id, "UPDATE", "new_trusted_facts", list(route), 1.0)


def do_update(d, pages, items, llm, route=("en", "bn"), mode="auto", now=LATER, cfg=CFG):
    dec = update_decision(route)
    out = run_updates([dec], scored_for(items), cfg, d, mode, llm, fetcher(pages), None, now)
    return dec, out


NEW_PAGES = {"https://www.aljazeera.com/n1": A1 + " " + A2 + PAD, "https://www.dhakatribune.com/n2": B1 + " " + B2 + PAD}
NEW_ITEMS = [item("aljazeera.com", "https://www.aljazeera.com/n1"), item("dhakatribune.com", "https://www.dhakatribune.com/n2")]


# ---------------- merge + significance ----------------

def _new_sheet(statements, sources, version=2):
    facts = [Fact(f"F{i}", "what", st, status, [], ids, "") for i, (st, status, ids) in enumerate(statements, 1)]
    return FactSheet(FS.story_id, version, facts, sources)


def _src(i, domain, tier=2):
    return {"id": f"S{i}", "name": domain, "tier": tier, "url": f"https://{domain}/{i}", "domain": domain, "lang": "en",
            "published_at": "2026-10-14T08:00:00+00:00"}


def test_merge_keeps_old_ids_renumbers_new_sources_and_counts_only_real_news():
    new = _new_sheet([
        ("The metro rail extension to Kamalapur opened to passengers on Thursday.", "confirmed", ["S1", "S2"]),   # rephrased old fact
        ("Metro trains carried 61,000 passengers on the first day of service.", "confirmed", ["S1", "S2"]),       # real news
        ("Fares were raised for tourists by order.", "reported", ["S1"]),                                         # single source
        ("Fares up.", "confirmed", ["S1", "S2"]),                                                                 # tiny
    ], [_src(1, "aljazeera.com"), _src(2, "dhakatribune.com")])
    merged, sig = merge_fact_sheets(FS, new, 5)
    assert merged.version == 2 and [s["id"] for s in merged.sources][:3] == ["S1", "S2", "S3"]
    assert {s["id"] for s in merged.sources} >= {"S4", "S5"} and merged.facts[0].id == "F1"
    assert len(merged.facts) == len(FS.facts) + 3             # the rephrased fact merged, 3 appended
    assert sig == ["F5"]                                      # only the confirmed, long-enough, genuinely new fact
    f1 = merged.facts[0]
    assert f1.statement == FS.facts[0].statement and set(f1.source_ids) >= {"S1", "S2", "S3", "S4", "S5"}
    assert FS.facts[0].source_ids == ["S1", "S2", "S3"]       # the old sheet object is not mutated


def test_two_orgs_upgrade_a_reported_fact_and_figures_that_change_are_flagged():
    new = _new_sheet([("The extension is expected to carry about 60,000 more riders a day.", "reported", ["S1"]),
                      ("The extension is expected to carry about 75,000 more riders a day.", "confirmed", ["S1", "S2"])],
                     [_src(1, "dhakatribune.com"), _src(2, "aljazeera.com")])
    merged, sig = merge_fact_sheets(FS, new, 5)
    f3 = next(f for f in merged.facts if f.id == "F3")
    assert f3.status == "confirmed" and f3.source_ids == ["S1", "S4"]       # BBC alone -> BBC + a second organisation
    assert "different figures (F5)" in f3.note
    changed = [f for f in merged.facts if "differ from F3" in f.note]
    assert [f.id for f in changed] == ["F5"] and "75,000" in changed[0].statement
    assert sig == ["F3", "F5"]                                              # upgraded to confirmed + a changed confirmed figure


def test_merge_without_old_ids_gap_and_fallback_sheet_from_article():
    fm = {"lang": "en", "fact_sheet_version": 3, "sources": [{"id": "S2", "name": "X", "tier": 1, "url": "https://x.com/a"}],
          "confirmed": ["Fact one [S2]."], "unclear": ["Maybe [S2][S9]."], "disputed": []}
    fs = fact_sheet_from_article("st-1", fm)
    assert fs.version == 3 and [f.status for f in fs.facts] == ["confirmed", "reported"] and fs.facts[1].source_ids == ["S2", "S9"]
    merged, _ = merge_fact_sheets(fs, _new_sheet([], []), 5)
    assert merged.version == 4


# ---------------- end-to-end update ----------------

def test_update_in_place_same_slug_date_kept_timeline_grows_state_recorded():
    with tempfile.TemporaryDirectory() as t:
        d = brain_with_story(t)
        before = {l: load_published(d, l, load_stories(d / "state")[FS.story_id].versions[l]["slug"]) for l in ("en", "bn")}
        old_fm, old_body = before["en"]
        llm, mock = mk_llm(write_en=writer("en", slug="a-completely-different-slug", category="sports"))
        dec, out = do_update(d, NEW_PAGES, NEW_ITEMS, llm, route=("en",))
        oc = out[FS.story_id]
        assert oc.status == "ok" and oc.updated == ["en"] and oc.added == []
        assert "|facts:ok|en:publish" in dec.reason
        st = load_stories(d / "state")[FS.story_id]
        slug = st.versions["en"]["slug"]
        assert slug == old_fm["slug"] and not (d / "content" / "en" / "a-completely-different-slug.md").exists()
        fm, body = load_published(d, "en", slug)
        assert fm["date"] == old_fm["date"] and fm["lastmod"] != old_fm["lastmod"] and fm["categories"] == old_fm["categories"]
        old_tl = [e["text"] for e in old_fm["timeline"]]
        assert [e["text"] for e in fm["timeline"]][:len(old_tl)] == old_tl and len(fm["timeline"]) > len(old_tl)
        assert all(e["time"] for e in fm["timeline"][len(old_tl):])           # new entries are dated
        assert fm["fact_sheet_version"] == 2 and fm["corrections"] == []
        assert any("61" in x for x in fm["confirmed"])                         # the new confirmed fact reached the article
        assert any("late night" in x.lower() for x in fm["unclear"])           # the single-source status change, as reported
        # story state
        assert st.fact_sheet_version == 2 and st.last_updated == iso(LATER) and st.updates == [iso(LATER)]
        assert st.versions["en"]["last_updated"] == iso(LATER) and "update_state" not in st.versions["en"]
        assert "https://www.aljazeera.com/n1" in st.urls and st.timeline[-1]["version"] == 2 and st.timeline[-1]["text"]
        assert st.status == "developing"
        assert load_fact_sheet(d / "state", FS.story_id).version == 2
        # bn was not routed: untouched
        assert load_published(d, "bn", st.versions["bn"]["slug"])[0]["lastmod"] == before["bn"][0]["lastmod"]


def test_no_update_for_rephrased_old_facts_or_single_source_or_tiny_changes_and_no_rewrite_cost():
    cases = {
        "rephrased": ({"https://www.aljazeera.com/n1": "Dhaka's metro rail extension to Kamalapur opened to passengers on Thursday, officials said. "
                       "The new section is 3.9 km long and has two stations." + PAD,
                       "https://www.dhakatribune.com/n2": "The metro rail extension to Kamalapur opened to passengers on Thursday morning. "
                       "The new section is 3.9 km long and has two stations, officials said." + PAD}, NEW_ITEMS),
        "single_source": ({"https://www.aljazeera.com/n1": A1 + PAD + PAD}, NEW_ITEMS[:1]),
    }
    for name, (pages, items) in cases.items():
        with tempfile.TemporaryDirectory() as t:
            d = brain_with_story(t)
            llm, mock = mk_llm()
            dec, out = do_update(d, pages, items, llm)
            assert out[FS.story_id].status == "no_new_fact", name
            assert not [c for c in mock.calls if c["task"].startswith("write") or c["task"] == "qa_check"], name   # nothing written
            st = load_stories(d / "state")[FS.story_id]
            assert st.updates == [] and st.fact_sheet_version == 1 and st.last_updated == iso(NOW)
            assert {i.url for i in items} <= set(st.urls), name        # examined: the gate will not fire on them again
    with tempfile.TemporaryDirectory() as t:                              # tiny change: confirmed, but shorter than the limit
        d = brain_with_story(t)
        pages = {"https://www.aljazeera.com/n1": A1 + PAD, "https://www.dhakatribune.com/n2": B1 + PAD}
        tiny = make_cfg(limits={"min_update_fact_words": 20})
        assert do_update(d, pages, NEW_ITEMS, mk_llm()[0], cfg=tiny)[1][FS.story_id].status == "no_new_fact"
        d2 = brain_with_story(tempfile.mkdtemp())
        assert do_update(d2, pages, NEW_ITEMS, mk_llm()[0])[1][FS.story_id].status == "ok"   # same news, default limit


def test_budget_exhaustion_queues_the_update_and_records_nothing():
    with tempfile.TemporaryDirectory() as t:
        d = brain_with_story(t)
        llm, _ = mk_llm()
        llm.can_run = lambda task, est: False
        dec, out = do_update(d, NEW_PAGES, NEW_ITEMS, llm)
        assert dec.queued and out[FS.story_id].status == "queued_budget"
        st = load_stories(d / "state")[FS.story_id]
        assert st.updates == [] and "https://www.aljazeera.com/n1" not in st.urls


# ---------------- corrections ----------------

def _correction(**kw):
    c = {"said": "about 60,000 more riders a day", "correct": "61,000 passengers used it on the first day"}
    c.update(kw)
    return {"corrections": [c]}


def test_corrections_are_dated_kept_and_never_deleted():
    with tempfile.TemporaryDirectory() as t:
        d = brain_with_story(t)
        llm, _ = mk_llm(write_en=writer("en", **_correction()))
        do_update(d, NEW_PAGES, NEW_ITEMS, llm, route=("en",))
        st = load_stories(d / "state")[FS.story_id]
        fm, _ = load_published(d, "en", st.versions["en"]["slug"])
        assert len(fm["corrections"]) == 1 and fm["corrections"][0]["date"] and "61,000" in fm["corrections"][0]["correct"]
        # a later update whose writer forgets the old correction still keeps it, and adds nothing new
        pages2 = {"https://www.reuters.com/n3": "Rail officials announced that the metro extension will add a third station in 2027, the ministry said." + PAD + PAD,
                  "https://www.thedailystar.net/n4": "The ministry announced a third metro station will be built in 2027, officials said." + PAD + PAD}
        items2 = [item("reuters.com", "https://www.reuters.com/n3"), item("thedailystar.net", "https://www.thedailystar.net/n4")]
        d2_llm, _ = mk_llm()
        scored = scored_for(NEW_ITEMS + items2)
        dec = update_decision(("en",))
        run_updates([dec], scored, CFG, d, "auto", d2_llm, fetcher({**NEW_PAGES, **pages2}), None, LATER + timedelta(hours=3))
        fm2, _ = load_published(d, "en", st.versions["en"]["slug"])
        assert fm2["corrections"] == fm["corrections"] and fm2["fact_sheet_version"] == 3
        assert fm2["date"] == fm["date"]


def test_correction_must_be_real_or_the_update_is_parked():
    for bad in (_correction(said="a figure the article never contained: 999"),
                _correction(correct="the real toll is 4242"),
                _correction(said="completely invented statement about something else entirely")):
        with tempfile.TemporaryDirectory() as t:
            d = brain_with_story(t)
            llm, _ = mk_llm(write_en=writer("en", **bad))
            dec, out = do_update(d, NEW_PAGES, NEW_ITEMS, llm, route=("en",))
            r = out[FS.story_id].results["en"]
            assert r.status == "pending" and "correction_unsupported" in r.qa.failures, bad
            st = load_stories(d / "state")[FS.story_id]
            assert st.versions["en"]["update_state"] == "pending"           # old article stays live; draft is in pending/
            assert (d / "pending" / "en" / f"{st.versions['en']['slug']}.md").is_file()
            assert load_published(d, "en", st.versions["en"]["slug"])[0]["fact_sheet_version"] == 1


def test_a_figure_that_vanishes_without_a_correction_needs_review_and_unchanged_text_fails():
    prev_fm, prev_body = load_published(*_old_en())
    prev = previous_article(prev_fm, prev_body)
    merged, _ = merge_fact_sheets(FS, _new_sheet([("Trains carried 61,000 passengers on the first day of service.", "confirmed", ["S1", "S2"])],
                                                  [_src(1, "aljazeera.com"), _src(2, "dhakatribune.com")]), 5)
    w = _write("en")
    import json
    msgs = [{"role": "user", "content": "<fact_sheet>\n" + json.dumps({"facts": [
        {"id": f.id, "kind": f.kind, "statement": f.statement, "status": f.status, "source_ids": f.source_ids, "note": ""}
        for f in merged.facts if "3.9" not in f.statement], "sources": merged.sources, "story_id": FS.story_id}) + "\n</fact_sheet>"}]
    art = finalize_update(w(msgs) | {"sources": [{"id": s["id"]} for s in merged.sources]}, prev, "en", LATER)
    from newsbot.write import normalise_article
    art, _ = normalise_article(art, merged)
    r = run_gates(art, merged, "en", CFG, recent=[], run_llm_check=False, previous=prev)
    assert "silent_change" in r.review
    same = dict(prev)
    same["sources"] = [{"id": s["id"], "name": s["name"], "tier": s["tier"], "url": s["url"], "published_at": s["published_at"]} for s in FS.sources]
    r2 = run_gates(same, FS, "en", CFG, recent=[], run_llm_check=False, previous=prev)
    assert "no_change" in r2.failures


def _old_en():
    d = tempfile.mkdtemp()
    brain_with_story(d)
    st = load_stories(Path(d) / "state")[FS.story_id]
    return Path(d), "en", st.versions["en"]["slug"]


# ---------------- review mode, reconcile ----------------

def test_review_mode_update_waits_for_merge_and_blocks_a_second_update():
    with tempfile.TemporaryDirectory() as t:
        d = brain_with_story(t)
        do_update(d, NEW_PAGES, NEW_ITEMS, mk_llm()[0], route=("en",), mode="review")
        st = load_stories(d / "state")[FS.story_id]
        v = st.versions["en"]
        assert v["state"] == "published" and v["update_state"] == "awaiting_review" and v["update_lastmod"] == iso(LATER)
        known = {FS.story_id: st}
        s = SimpleNamespace(cluster=scored_for([])[0].cluster, score=1.0, components={}, blocked=False,
                            trusted_domains={"a", "b", "c"}, trusted_by_lang={"en": {"a", "b"}})
        dd = decide([s], known, CFG, LATER + timedelta(hours=9), local_day_start(LATER + timedelta(hours=9), 6), 0)[0]
        assert dd.action == "SKIP" and dd.reason == "update_awaiting_review"
        assert reconcile(d, LATER + timedelta(hours=1))["updates_applied"] == 1     # the file on main carries lastmod >= update
        assert "update_state" not in load_stories(d / "state")[FS.story_id].versions["en"]


def test_unmerged_update_expires_and_a_deleted_pending_draft_is_rejected():
    with tempfile.TemporaryDirectory() as t:
        d = brain_with_story(t)
        stories = load_stories(d / "state")
        v = stories[FS.story_id].versions["en"]
        v["update_state"], v["update_lastmod"] = "awaiting_review", iso(LATER)       # main still has the older lastmod
        save_stories(d / "state", stories)
        assert reconcile(d, LATER + timedelta(hours=10))["updates_rejected"] == 0
        assert reconcile(d, LATER + timedelta(hours=73))["updates_rejected"] == 1
        stories = load_stories(d / "state")
        v = stories[FS.story_id].versions["bn"]
        v["update_state"], v["update_lastmod"] = "pending", iso(LATER)
        save_stories(d / "state", stories)                                            # no pending draft file exists
        assert reconcile(d, LATER)["updates_rejected"] == 1


# ---------------- statuses ----------------

def _story(hours_idle, status="developing", urls=()):
    return Story(story_id="st-x", status=status, first_seen=iso(NOW - timedelta(days=30)),
                 last_updated=iso(NOW - timedelta(hours=hours_idle)), versions={"en": {"slug": "x"}}, urls=list(urls))


def _sc(items):
    return SimpleNamespace(cluster=Cluster("st-x", items, True))


def test_status_stable_after_48h_closed_after_7_days_and_reopen_on_new_signal():
    st = {"st-x": _story(47)}
    refresh_statuses(st, [], CFG, NOW)
    assert st["st-x"].status == "developing"
    st = {"st-x": _story(49)}
    refresh_statuses(st, [], CFG, NOW)
    assert st["st-x"].status == "stable"
    st = {"st-x": _story(24 * 7 + 1, status="stable")}
    refresh_statuses(st, [], CFG, NOW)
    assert st["st-x"].status == "closed"
    # stable + two new independent trusted items -> developing again, clock reset
    st = {"st-x": _story(60, status="stable", urls=["https://old.test/1"])}
    refresh_statuses(st, [_sc([item("a.com", "https://a.com/1"), item("b.com", "https://b.com/1")])], CFG, NOW)
    assert st["st-x"].status == "developing" and st["st-x"].last_news == iso(NOW)
    # a single new item is news (resets the clock) but not a confirmed signal, so a stable story stays stable
    st = {"st-x": _story(60, status="stable")}
    refresh_statuses(st, [_sc([item("a.com", "https://a.com/1")])], CFG, NOW)
    assert st["st-x"].status == "stable" and st["st-x"].last_news == iso(NOW)
    # tier-3 items are not news; closed stories never reopen
    st = {"st-x": _story(60), "st-y": _story(24 * 9, status="closed")}
    st["st-y"].story_id = "st-y"
    refresh_statuses(st, [_sc([item("a.com", "https://a.com/9", tier=3)]), SimpleNamespace(cluster=Cluster("st-y", [item("a.com", "https://a.com/1"), item("b.com", "https://b.com/1")], True))], CFG, NOW)
    assert st["st-x"].status == "stable" and st["st-y"].status == "closed"


# ---------------- language routing ----------------

def _routed(domains_by_lang, story):
    return SimpleNamespace(cluster=Cluster(story.story_id, [], True), score=1.0, components={}, blocked=False,
                           trusted_domains={d for v in domains_by_lang.values() for d in v}, trusted_by_lang=domains_by_lang)


def test_gate_adds_a_missing_language_without_a_new_signal_and_respects_the_other_update_rules():
    story = _story(30)
    story.story_id = "st-x"
    day = local_day_start(NOW, 6)
    s = _routed({"en": {"a.com", "b.com"}, "bn": {"c.com", "d.com"}}, story)
    d = decide([s], {"st-x": story}, CFG, NOW, day, 0)[0]
    assert d.action == "UPDATE" and d.reason == "new_language_version" and d.route == ["bn"]
    assert decide([_routed({"en": {"a.com", "b.com"}, "bn": {"c.com"}}, story)], {"st-x": story}, CFG, NOW, day, 0)[0].reason == "no_new_confirmed_fact"
    recent = _story(1)
    assert decide([s], {"st-x": recent}, CFG, NOW, day, 0)[0].reason == "too_soon_since_update"       # < 2h
    capped = _story(30)
    capped.updates = [iso(NOW - timedelta(hours=h)) for h in (3, 4, 5)]
    assert decide([s], {"st-x": capped}, CFG, NOW, day, 0)[0].reason == "update_cap_reached"          # 3/day


def test_run_updates_writes_the_missing_language_and_leaves_the_published_one_alone():
    bn1 = "কমলাপুর পর্যন্ত মেট্রোরেলের নতুন অংশে যাত্রীদের ভিড় দেখা গেছে সকাল থেকেই। স্টেশনের বাইরে টিকিটের সারি অনেক দীর্ঘ ছিল।"
    bn2 = "নতুন অংশে সকাল থেকেই যাত্রীদের ভিড় ছিল এবং স্টেশনের বাইরে টিকিটের লম্বা সারি দেখা গেছে। যাত্রীরা সন্তোষ প্রকাশ করেছেন।"
    pad = " প্রতিবেদনটি ঢাকা থেকে পাঠানো হয়েছে এবং মন্ত্রণালয় বৃহস্পতিবার সন্ধ্যায় আর কোনো মন্তব্য করেনি।"
    pages = {"https://www.prothomalo.com/metro-9": bn1 + pad, "https://www.jugantor.com/metro-8": bn2 + pad}
    items = [item("prothomalo.com", "https://www.prothomalo.com/metro-9", "bn"), item("jugantor.com", "https://www.jugantor.com/metro-8", "bn")]
    with tempfile.TemporaryDirectory() as t:
        d = brain_with_story(t, route=("en",))
        st0 = load_stories(d / "state")[FS.story_id]
        assert list(st0.versions) == ["en"]
        en_before = (d / "content" / "en" / f"{st0.versions['en']['slug']}.md").read_text(encoding="utf-8")
        dec, out = do_update(d, pages, items, mk_llm()[0], route=("bn",))
        oc = out[FS.story_id]
        assert oc.added == ["bn"] and oc.updated == [], dec.reason
        st = load_stories(d / "state")[FS.story_id]
        assert st.route == "both" and st.versions["bn"]["state"] == "published" and st.versions["bn"]["url"].startswith("/bn/")
        assert (d / "content" / "bn" / f"{st.versions['bn']['slug']}.md").is_file()
        assert (d / "content" / "en" / f"{st0.versions['en']['slug']}.md").read_text(encoding="utf-8") == en_before
        fm, _ = load_published(d, "bn", st.versions["bn"]["slug"])
        assert fm["translationKey"] == FS.story_id and fm["lang"] == "bn"


def test_dry_run_never_writes_and_never_records():
    with tempfile.TemporaryDirectory() as t:
        d = brain_with_story(t)
        before = {p: p.read_bytes() for p in d.rglob("*") if p.is_file()}
        dec, out = do_update(d, NEW_PAGES, NEW_ITEMS, mk_llm()[0], route=("en",), mode="dry-run")
        assert out[FS.story_id].updated == ["en"]
        assert {p: p.read_bytes() for p in d.rglob("*") if p.is_file()} == before


# ---------------- run_pipeline wiring ----------------

def test_run_pipeline_update_path_end_to_end_with_status_clock():
    import json
    from newsbot.cli import FIXTURES, run_pipeline
    from newsbot.discover import fixture_fetcher
    from newsbot.fetch import fixture_article_fetcher
    from tests.helpers import FEEDS, mock_embedder
    kw = dict(embedder=mock_embedder, article_fetcher=fixture_article_fetcher(FIXTURES / "articles"),
              feed_fetcher=fixture_fetcher(FEEDS, CFG.sources))
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        llm, _ = mk_llm()
        r1 = run_pipeline(CFG, "auto", d, now=NOW, llm=llm, **kw)
        new_ids = [x.story_id for x in r1["decisions"] if x.action == "NEW"]
        assert new_ids and r1["UPDATE"] == 0
        stories = load_stories(d / "state")
        sid = next(i for i in new_ids if i in stories)
        assert {v["state"] for v in stories[sid].versions.values()} == {"pending"}      # cyclone: sensitive, parked
        for lang, v in stories[sid].versions.items():                                   # the owner approves both drafts
            src = d / "pending" / lang / f"{v['slug']}.md"
            _, fm, body = src.read_text(encoding="utf-8").split("---", 2)
            meta = yaml.safe_load(fm)
            meta.pop("pending")
            (d / "content" / lang).mkdir(parents=True, exist_ok=True)
            (d / "content" / lang / src.name).write_text("---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---" + body, encoding="utf-8")
            src.unlink()
        # same feeds an hour later: the story is known, its URLs are seen -> nothing to update, zero LLM write calls
        llm2, mock2 = mk_llm()
        r2 = run_pipeline(CFG, "auto", d, now=NOW + timedelta(hours=3), llm=llm2, **kw)
        assert r2["UPDATE"] == 0 and not mock2.calls
        assert {v["state"] for v in load_stories(d / "state")[sid].versions.values()} == {"published"}   # reconcile saw the files
        stories = load_stories(d / "state")
        # pretend only one source had been used so far and the rest of the cluster is now news
        st = stories[sid]
        fs_path = d / "state" / "factsheets" / f"{sid}.json"
        sheet = json.loads(fs_path.read_text(encoding="utf-8"))
        keep = sheet["sources"][0]["id"]
        sheet["sources"] = sheet["sources"][:1]
        sheet["facts"] = [dict(f, source_ids=[keep]) for f in sheet["facts"] if keep in f["source_ids"]]
        fs_path.write_text(json.dumps(sheet), encoding="utf-8")
        st.urls = []
        st.last_updated = iso(NOW - timedelta(hours=10))
        save_stories(d / "state", stories)
        llm3, mock3 = mk_llm()
        r3 = run_pipeline(CFG, "auto", d, now=NOW + timedelta(hours=4), llm=llm3, **kw)
        ups = [x for x in r3["decisions"] if x.story_id == sid]
        assert ups and ups[0].action == "UPDATE", [(x.action, x.reason) for x in ups]
        after = load_stories(d / "state")[sid]
        assert after.updates and after.fact_sheet_version == 2 and after.last_updated == iso(NOW + timedelta(hours=4))
        assert r3["updates"][sid].status == "ok" and any(c["task"].startswith("write") for c in mock3.calls)
        # still a sensitive story: the update is parked, the approved article stays live and unchanged
        assert {v.get("update_state") for v in after.versions.values()} == {"pending"}
        assert all(v["state"] == "published" for v in after.versions.values())
        # no news for 3 days -> stable; a week -> closed (real runs advance the clock)
        run_pipeline(CFG, "auto", d, now=NOW + timedelta(days=4), llm=mk_llm()[0], embedder=mock_embedder,
                     article_fetcher=kw["article_fetcher"], feed_fetcher=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
        assert load_stories(d / "state")[sid].status == "stable"
