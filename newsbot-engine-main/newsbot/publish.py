"""Stage 10 (state side): record new stories after write, tidy old states, and decide whether a deploy is needed.
Git (commit) and Hugo/wrangler live in the workflows and scripts/brain_commit.sh.

Version states (Story.versions[lang]['state']; a missing state means 'published', for M1-era data):
  published  content/<lang>/<slug>.md is on the brain's main branch and goes to the site
  rejected   the draft was cancelled (a gate still failed after the automatic fixes): archived in rejected/<lang>/<slug>.md
Nothing waits for a human. Only IDs, counts and states are logged (the engine repo is public)."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from newsbot.cluster import top_keywords
from newsbot.models import Story, iso, parse_ts, utcnow
from newsbot.score import lead_lang
from newsbot.state import _atomic_write, load_stories, save_stories

log = logging.getLogger("newsbot.publish")

MAX_STORY_URLS = 200
RECORDED = ("publish", "rejected")          # ArticleResult.status values that create a version


def _version_state(status: str) -> str:
    return "rejected" if status == "rejected" else "published"


def record_new_stories(state_dir, decisions, clusters_by_id: dict, factsheets: dict, articles: dict, now: datetime) -> list[str]:
    """After write: add every NEW story that produced a published or rejected article to state/stories.jsonl so the
    next hour does not treat it as NEW again. Stories whose languages were all queued or failed are NOT recorded
    (they stay candidates and retry next hour). Never called in dry-run. Returns the recorded story ids."""
    stories = load_stories(state_dir)
    recorded = []
    for d in decisions:
        results = articles.get(d.story_id)
        if d.action != "NEW" or not results or d.story_id in stories:
            continue
        versions = {}
        for lang, r in results.items():
            if r.status not in RECORDED or r.article is None:
                continue
            state = _version_state(r.status)
            versions[lang] = {"slug": r.article["slug"], "url": f"/{lang}/{r.article['slug']}/", "state": state,
                              "published_at": iso(now) if state == "published" else "", "last_updated": iso(now)}
        if not versions:
            continue
        cl = clusters_by_id.get(d.story_id)
        fs = factsheets.get(d.story_id)
        urls = {i.url for i in cl.items} if cl else set()
        urls |= {s["url"] for s in fs.sources} if fs else set()
        route = "both" if len(d.route) > 1 else (d.route[0] if d.route else "bn")
        stories[d.story_id] = Story(
            story_id=d.story_id, status="developing", first_seen=iso(now), last_updated=iso(now), route=route,
            fact_sheet_version=fs.version if fs else 0, versions=versions, urls=sorted(urls)[:MAX_STORY_URLS],
            keywords=top_keywords(cl.items, 30) if cl else [], lead_lang=lead_lang(cl.items) if cl else "")
        if all(v["state"] == "rejected" for v in versions.values()):
            stories[d.story_id].status = "closed"          # cancelled everywhere: a later event gets a new story
        recorded.append(d.story_id)
    if recorded:
        save_stories(state_dir, stories)
    log.info("recorded stories=%d", len(recorded))
    return recorded


def mark_seen(state_dir, story_id: str, urls: list[str]) -> None:
    """URLs examined and judged not to justify an update join story.urls, so the gate does not fire on them again."""
    stories = load_stories(state_dir)
    st = stories.get(story_id)
    if st is None or not urls:
        return
    st.urls = sorted(set(st.urls) | set(urls))[-MAX_STORY_URLS:]
    save_stories(state_dir, stories)


def record_updates(state_dir, outcomes: dict, now: datetime) -> list[str]:
    """After an update run: story-level bookkeeping. A language whose update was published gets its timestamps moved on;
    one whose update was rejected keeps the old live article (the new URLs are still marked as seen, so the next hour
    does not pay for the same update again). A language added by the update gets a normal version entry.
    Queued / failed stories retry next hour."""
    stories = load_stories(state_dir)
    recorded = []
    for sid, oc in outcomes.items():
        st = stories.get(sid)
        produced = [l for l in oc.updated + oc.added]
        if st is None or oc.fact_sheet is None or not produced:
            continue
        ts = iso(now)
        live = []
        for lang in produced:
            r = oc.results[lang]
            if lang in oc.updated:
                if r.status == "publish":
                    st.versions[lang]["last_updated"] = ts
                    live.append(lang)
            else:
                state = _version_state(r.status)
                st.versions[lang] = {"slug": r.article["slug"], "url": f"/{lang}/{r.article['slug']}/", "state": state,
                                     "published_at": ts if state == "published" else "", "last_updated": ts}
                if state == "published":
                    live.append(lang)
        langs = {l for l, v in st.versions.items() if v.get("state", "published") == "published"}
        st.route = "both" if len(langs) > 1 else next(iter(langs), st.route)
        st.urls = sorted(set(st.urls) | set(oc.fresh_urls) | {x["url"] for x in oc.fact_sheet.sources})[-MAX_STORY_URLS:]
        if live:
            st.last_updated = st.last_news = ts
            st.updates.append(ts)
            st.status = "developing"
            st.fact_sheet_version = oc.fact_sheet.version
            first = next((e["text"] for l in live for e in (oc.results[l].article or {}).get("timeline", [])[-1:]), "")
            st.timeline.append({"ts": ts, "version": oc.fact_sheet.version, "langs": sorted(live), "text": first})
        recorded.append(sid)
    if recorded:
        save_stories(state_dir, stories)
    log.info("recorded updates=%d", len(recorded))
    return recorded


def _as_dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        return parse_ts(str(v))
    except ValueError:
        return None


def _front_matter(path: Path) -> dict:
    parts = path.read_text(encoding="utf-8").split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        return yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}


def reconcile(brain, now: datetime | None = None) -> dict:
    """Tidy story state at the start of every real run. Only needed for data written by older versions, which had
    'awaiting_review' / 'pending' states and an update_state flag:
    - content/<lang>/<slug>.md exists -> published
    - otherwise                        -> rejected (nothing waits for a human any more)
    - every version rejected           -> story closed"""
    brain, now = Path(brain), now or utcnow()
    state_dir = brain / "state"
    stories = load_stories(state_dir)
    counts = {"published": 0, "rejected": 0, "closed": 0}
    for st in stories.values():
        for lang, v in st.versions.items():
            v.pop("update_state", None)
            v.pop("update_lastmod", None)
            if v.get("state", "published") not in ("awaiting_review", "pending"):
                continue
            live = brain / "content" / lang / f"{v.get('slug', '')}.md"
            if live.is_file():
                v["state"] = "published"
                v["published_at"] = str(_front_matter(live).get("date") or iso(now))
                counts["published"] += 1
            else:
                v["state"] = "rejected"
                counts["rejected"] += 1
        if st.status != "closed" and st.versions and all(v.get("state") == "rejected" for v in st.versions.values()):
            st.status = "closed"
            counts["closed"] += 1
    if any(counts.values()):
        save_stories(state_dir, stories)
    log.info("reconcile published=%d rejected=%d closed=%d", counts["published"], counts["rejected"], counts["closed"])
    return counts


def content_fingerprint(brain) -> str:
    """Hash of everything that changes the built site: published content, trust-page overrides, site config."""
    brain, h = Path(brain), hashlib.sha256()
    files = sorted((brain / "content").rglob("*.md")) if (brain / "content").is_dir() else []
    cfg = brain / "config" / "newsbot.yaml"
    files += [cfg] if cfg.is_file() else []
    for f in files:
        h.update(str(f.relative_to(brain)).encode("utf-8") + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def deploy_needed(brain) -> bool:
    """True when the site content or config differs from what the last successful deploy used."""
    rec = Path(brain) / "state" / "deployed.json"
    try:
        last = json.loads(rec.read_text(encoding="utf-8")).get("fingerprint")
    except (OSError, ValueError):
        last = None
    return last != content_fingerprint(brain)


def mark_deployed(brain, now: datetime | None = None) -> str:
    fp = content_fingerprint(brain)
    _atomic_write(Path(brain) / "state" / "deployed.json",
                  json.dumps({"fingerprint": fp, "deployed_at": iso(now or utcnow())}, sort_keys=True) + "\n")
    return fp
