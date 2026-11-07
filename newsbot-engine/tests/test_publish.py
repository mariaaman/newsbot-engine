"""M6 publish.py: story recording after write, review/pending reconciliation, deploy fingerprint, gate guard."""
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from newsbot.articles import ArticleResult, produce_articles, write_outputs
from newsbot.cluster import cluster_items
from newsbot.evaluate import load_stories as load_fact_sheets
from newsbot.gate import Decision
from newsbot.llm import build_llm, load_llm_settings
from newsbot.llm.dryrun import dry_run_provider
from newsbot.models import Item, Story, iso
from newsbot.publish import (content_fingerprint, deploy_needed, mark_deployed, reconcile, record_new_stories)
from newsbot.state import load_stories, local_day_start, new_today
from tests.helpers import NOW, TEMPLATE, make_cfg
from tests.test_pipeline_m1 import _bank_story, by_title, run

CFG = make_cfg()


def _c(**kw):
    return {"published": 0, "rejected": 0, "closed": 0, "updates_applied": 0, "updates_rejected": 0, **kw}
FS = {fs.story_id: fs for fs in load_fact_sheets()}["ev-metro"]


def _llm():
    return build_llm(load_llm_settings(TEMPLATE), None, dry_run=True, mock=dry_run_provider())


def _cluster(fs):
    items = [Item(title=f"{s['name']} {fs.story_id} metro rail electric bus", url=s["url"], domain=s["domain"],
                  published=NOW, lang=s["lang"], source_name=s["name"], tier=s["tier"]) for s in fs.sources]
    return cluster_items(items, CFG)[0]


def _record(d, mode, route=("en", "bn")):
    results = produce_articles(FS, list(route), CFG, None, _llm(), recent_by_lang={"en": [], "bn": []})
    write_outputs(d, FS, results, NOW)
    dec = Decision(FS.story_id, "NEW", "trending_and_verified_breadth", list(route), 1.0)
    cl = _cluster(FS)
    ids = record_new_stories(Path(d) / "state", [dec], {FS.story_id: cl}, {FS.story_id: FS},
                             {FS.story_id: results}, NOW, mode)
    return ids, results, cl


def test_record_auto_marks_published_and_counts_toward_the_daily_cap():
    with tempfile.TemporaryDirectory() as d:
        ids, results, cl = _record(d, "auto")
        assert ids == [FS.story_id]
        st = load_stories(Path(d) / "state")[FS.story_id]
        assert st.route == "both" and st.status == "developing" and st.fact_sheet_version == 1
        for lang in ("en", "bn"):
            v = st.versions[lang]
            assert v["state"] == "published" and v["published_at"] == iso(NOW)
            assert v["url"] == f"/{lang}/{results[lang].article['slug']}/"
        assert {i.url for i in cl.items} <= set(st.urls) and {s["url"] for s in FS.sources} <= set(st.urls)
        assert new_today(load_stories(Path(d) / "state"), local_day_start(NOW, 6)) == 1


def test_record_review_mode_is_awaiting_review_and_still_uses_the_cap():
    with tempfile.TemporaryDirectory() as d:
        _record(d, "review")
        stories = load_stories(Path(d) / "state")
        v = stories[FS.story_id].versions["en"]
        assert v["state"] == "awaiting_review" and v["published_at"] == ""
        assert new_today(stories, local_day_start(NOW, 6)) == 1      # unreviewed PRs cannot pile up without limit


def test_pending_articles_are_recorded_and_queued_or_failed_are_not():
    with tempfile.TemporaryDirectory() as d:
        s = Path(d) / "state"
        art = {"slug": "x-slug"}
        res = {"en": ArticleResult("en", "pending", art), "bn": ArticleResult("bn", "queued_budget")}
        dec = Decision("st-a", "NEW", "r", ["en", "bn"], 1.0)
        assert record_new_stories(s, [dec], {}, {}, {"st-a": res}, NOW, "review") == ["st-a"]
        st = load_stories(s)["st-a"]
        assert list(st.versions) == ["en"] and st.versions["en"]["state"] == "pending"
        none = {"en": ArticleResult("en", "queued_budget"), "bn": ArticleResult("bn", "write_failed")}
        dec2 = Decision("st-b", "NEW", "r", ["en", "bn"], 1.0)
        assert record_new_stories(s, [dec2], {}, {}, {"st-b": none}, NOW, "auto") == []   # retried next hour
        assert "st-b" not in load_stories(s)
        # a known story is never overwritten, and UPDATE decisions are ignored (M7)
        up = Decision("st-a", "UPDATE", "r", ["en"], 1.0)
        assert record_new_stories(s, [dec, up], {}, {}, {"st-a": res}, NOW, "auto") == []


def test_gate_does_not_update_a_story_that_has_nothing_live():
    cfg = make_cfg()
    st = _bank_story(cfg, 4)
    st.versions = {"en": {"url": "/en/x", "state": "awaiting_review", "published_at": ""}}
    scored, dec, *_ = run(cfg, known={st.story_id: st})
    d, _ = by_title(dec, scored, "raises policy rate")
    assert d.action == "SKIP" and d.reason == "awaiting_review"
    st.versions["en"]["state"] = "published"
    scored, dec, *_ = run(cfg, known={st.story_id: st})
    assert by_title(dec, scored, "raises policy rate")[0].action == "UPDATE"


def test_reconcile_publishes_merged_prs_and_rejects_stale_or_deleted():
    with tempfile.TemporaryDirectory() as d:
        brain, state = Path(d), Path(d) / "state"
        ids, results, _ = _record(d, "review")
        slug = results["en"].article["slug"]
        # PR merged for en (file is on main), bn still waiting and young
        (brain / "content" / "bn" / f"{results['bn'].article['slug']}.md").unlink()
        counts = reconcile(brain, NOW + timedelta(hours=1))
        st = load_stories(state)[FS.story_id]
        assert counts["published"] == 1 and st.versions["en"]["state"] == "published"
        assert st.versions["en"]["published_at"] and st.versions["bn"]["state"] == "awaiting_review"
        # 80 hours later the unmerged bn PR is treated as rejected; the story stays open (en is live)
        reconcile(brain, NOW + timedelta(hours=80))
        st = load_stories(state)[FS.story_id]
        assert st.versions["bn"]["state"] == "rejected" and st.status == "developing"
        assert (brain / "content" / "en" / f"{slug}.md").is_file()


def test_reconcile_closes_story_when_every_version_is_rejected_and_pending_follows_the_draft():
    with tempfile.TemporaryDirectory() as d:
        brain = Path(d)
        art = {"slug": "p-slug"}
        dec = Decision("st-p", "NEW", "r", ["en"], 1.0)
        record_new_stories(brain / "state", [dec], {}, {}, {"st-p": {"en": ArticleResult("en", "pending", art)}}, NOW, "review")
        (brain / "pending" / "en").mkdir(parents=True)
        (brain / "pending" / "en" / "p-slug.md").write_text("---\ntitle: t\n---\nbody\n", encoding="utf-8")
        assert reconcile(brain, NOW + timedelta(hours=200)) == _c(published=0, rejected=0, closed=0)   # draft still there
        (brain / "pending" / "en" / "p-slug.md").unlink()                                                      # owner deleted it
        assert reconcile(brain, NOW + timedelta(hours=201)) == _c(published=0, rejected=1, closed=1)
        assert load_stories(brain / "state")["st-p"].status == "closed"


def test_old_state_without_version_state_still_counts_as_published():
    cfg = make_cfg()
    st = _bank_story(cfg, 4)                         # versions have no 'state' key (M1-era data)
    with tempfile.TemporaryDirectory() as d:
        from newsbot.state import save_stories
        save_stories(Path(d) / "state", {st.story_id: st})
        assert reconcile(d, NOW) == _c(published=0, rejected=0, closed=0)


def test_deploy_fingerprint_tracks_content_and_site_config_only():
    with tempfile.TemporaryDirectory() as d:
        brain = Path(d)
        (brain / "config").mkdir()
        (brain / "config" / "newsbot.yaml").write_text("site: {name: A}\n", encoding="utf-8")
        (brain / "content" / "en").mkdir(parents=True)
        (brain / "content" / "en" / "a.md").write_text("---\ntitle: a\n---\nx\n", encoding="utf-8")
        assert deploy_needed(brain)                                # never deployed
        fp = mark_deployed(brain, NOW)
        assert not deploy_needed(brain) and json.loads((brain / "state" / "deployed.json").read_text())["fingerprint"] == fp
        (brain / "state" / "decisions.jsonl").write_text("{}\n")   # state churn does not trigger a deploy
        (brain / "pending").mkdir()
        (brain / "pending" / "x.md").write_text("draft")           # neither do drafts
        assert not deploy_needed(brain)
        (brain / "content" / "en" / "b.md").write_text("---\ntitle: b\n---\ny\n", encoding="utf-8")
        assert deploy_needed(brain)                                # a merged article does
        mark_deployed(brain)
        (brain / "config" / "newsbot.yaml").write_text("site: {name: B}\n", encoding="utf-8")
        assert deploy_needed(brain)                                # so does a config change


def test_cli_deploy_commands():
    import io
    from contextlib import redirect_stdout
    from newsbot.cli import main
    with tempfile.TemporaryDirectory() as d:
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert main(["deploy-needed", "--brain", d]) == 0
        assert buf.getvalue().strip() == "deploy=true"
        assert main(["mark-deployed", "--brain", d]) == 0
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["deploy-needed", "--brain", d])
        assert buf.getvalue().strip() == "deploy=false"


def test_real_mode_run_records_stories_so_the_next_hour_does_not_repeat_them():
    """review/auto mode end to end offline: fixture feeds + fixture articles + mock LLM, real file writes."""
    import shutil
    from datetime import timedelta
    from newsbot.cli import FIXTURES, run_pipeline
    from newsbot.discover import fixture_fetcher
    from newsbot.fetch import fixture_article_fetcher
    cfg = make_cfg()
    with tempfile.TemporaryDirectory() as d:
        brain = Path(d) / "brain"
        shutil.copytree(TEMPLATE, brain)
        kw = dict(llm=_llm(), embedder=None, article_fetcher=fixture_article_fetcher(FIXTURES / "articles"),
                  feed_fetcher=fixture_fetcher(FIXTURES / "feeds", cfg.sources))
        r1 = run_pipeline(cfg, "review", brain, now=NOW, **kw)
        assert r1["mode"] == "review" and r1["NEW"] >= 1 and r1["articles"]
        stories = load_stories(brain / "state")
        assert set(stories) == {sid for sid, res in r1["articles"].items()
                                if any(x.status in ("publish", "pending") for x in res.values())} and stories
        for st in stories.values():
            assert all(v["state"] in ("awaiting_review", "pending") for v in st.versions.values())
        assert (brain / "state" / "decisions.jsonl").is_file()
        # next hour: the same fixture items must not be NEW again, and nothing new is written
        kw["llm"] = _llm()
        r2 = run_pipeline(cfg, "review", brain, now=NOW + timedelta(hours=1), **kw)
        for sid in stories:            # recorded stories are not NEW again; they wait for review (nothing live to update)
            d2 = next(x for x in r2["decisions"] if x.story_id == sid)
            assert (d2.action, d2.reason) == ("SKIP", "awaiting_review")
        assert not r2["articles"] and set(load_stories(brain / "state")) == set(stories)
        # a story that could not be verified (insufficient_sources) is not recorded; it waits out the cool-down
        # (limits.insufficient_sources_cooldown_hours) with no fetch and no LLM spend, then is retried (test_m8)
        retried = [x for x in r2["decisions"] if x.action == "NEW"]
        assert all("facts:cooldown" in x.reason for x in retried)
