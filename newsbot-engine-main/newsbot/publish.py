"""Stage 10 (state side): record new stories after write, reconcile review/pending outcomes, and decide whether
a deploy is needed. Git (commit, branch, PR) and Hugo/wrangler live in the workflows and scripts/brain_commit.sh.

Version states (Story.versions[lang]['state']; a missing state means 'published', for M1-era data):
  published       content/<lang>/<slug>.md is on the brain's main branch
  awaiting_review review mode: the article sits in an open PR (its content file is not on main yet)
  pending         failed a QA gate or is sensitive: draft is in pending/<lang>/<slug>.md for a human
  rejected        PR closed unmerged / draft deleted, or never decided within REVIEW_EXPIRY_HOURS
Only IDs, counts and states are logged (the engine repo is public)."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from newsbot.cluster import top_keywords
from newsbot.models import Story, iso, parse_ts, utcnow
from newsbot.score import lead_lang
from newsbot.state import _atomic_write, load_stories, save_stories

log = logging.getLogger("newsbot.publish")

REVIEW_EXPIRY_HOURS = 72
MAX_STORY_URLS = 200
RECORDED = ("publish", "pending")          # ArticleResult.status values that create a version


def _version_state(status: str, mode: str) -> str:
    if status == "pending":
        return "pending"
    return "published" if mode == "auto" else "awaiting_review"


def record_new_stories(state_dir, decisions, clusters_by_id: dict, factsheets: dict, articles: dict, now: datetime,
                       mode: str) -> list[str]:
    """After write: add every NEW story that produced a published or pending article to state/stories.jsonl so the
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
            state = _version_state(r.status, mode)
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


def record_updates(state_dir, outcomes: dict, now: datetime, mode: str) -> list[str]:
    """After an update run: story-level bookkeeping. Only stories that produced an article (published or pending)
    are touched; queued / failed ones retry next hour. Existing language versions keep state 'published' (the old
    article is still live) and get update_state awaiting_review|pending until the update is merged or approved;
    a language added by the update gets a normal version entry."""
    stories = load_stories(state_dir)
    recorded = []
    for sid, oc in outcomes.items():
        st = stories.get(sid)
        produced = [l for l in oc.updated + oc.added]
        if st is None or oc.fact_sheet is None or not produced:
            continue
        ts = iso(now)
        for lang in produced:
            r = oc.results[lang]
            if lang in oc.updated:
                v = st.versions[lang]
                v["last_updated"] = ts
                if r.status == "pending" or mode != "auto":
                    v["update_state"] = "pending" if r.status == "pending" else "awaiting_review"
                    v["update_lastmod"] = ts
                else:
                    v.pop("update_state", None)
                    v.pop("update_lastmod", None)
            else:
                state = _version_state(r.status, mode)
                st.versions[lang] = {"slug": r.article["slug"], "url": f"/{lang}/{r.article['slug']}/", "state": state,
                                     "published_at": ts if state == "published" else "", "last_updated": ts}
        langs = set(st.versions)
        st.route = "both" if len(langs) > 1 else next(iter(langs), st.route)
        st.last_updated = st.last_news = ts
        st.updates.append(ts)
        st.status = "developing"
        st.fact_sheet_version = oc.fact_sheet.version
        st.urls = sorted(set(st.urls) | set(oc.fresh_urls) | {x["url"] for x in oc.fact_sheet.sources})[-MAX_STORY_URLS:]
        first = next((e["text"] for l in produced for e in (oc.results[l].article or {}).get("timeline", [])[-1:]), "")
        st.timeline.append({"ts": ts, "version": oc.fact_sheet.version, "langs": sorted(produced), "text": first})
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


def reconcile(brain, now: datetime | None = None, expiry_hours: int = REVIEW_EXPIRY_HOURS) -> dict:
    """Align story state with what is actually on main. Run at the start of every real run.
    - content/<lang>/<slug>.md exists            -> published (a merged PR, or a pending draft the owner moved)
    - awaiting_review/pending, no file anywhere   -> rejected once older than expiry_hours (awaiting) or at once (pending
      draft deleted)
    - every version rejected                      -> story closed"""
    brain, now = Path(brain), now or utcnow()
    state_dir = brain / "state"
    stories = load_stories(state_dir)
    counts = {"published": 0, "rejected": 0, "closed": 0, "updates_applied": 0, "updates_rejected": 0}
    for st in stories.values():
        for lang, v in st.versions.items():
            slug = v.get("slug", "")
            us = v.get("update_state")
            if us in ("awaiting_review", "pending"):      # an update of a live article (the old one stays published)
                live, draft = brain / "content" / lang / f"{slug}.md", brain / "pending" / lang / f"{slug}.md"
                lastmod, wanted = _as_dt(_front_matter(live).get("lastmod")) if live.is_file() else None, _as_dt(v.get("update_lastmod"))
                if lastmod and wanted and lastmod >= wanted:
                    outcome = "applied"                    # PR merged, or the owner moved the draft into content/
                elif us == "pending" and not draft.is_file():
                    outcome = "rejected"
                elif us == "awaiting_review" and wanted and now - wanted > timedelta(hours=expiry_hours):
                    outcome = "rejected"
                else:
                    continue
                v.pop("update_state", None)
                v.pop("update_lastmod", None)
                counts["updates_" + outcome] += 1
                continue
            if v.get("state", "published") not in ("awaiting_review", "pending"):
                continue
            slug = v.get("slug", "")
            live = brain / "content" / lang / f"{slug}.md"
            draft = brain / "pending" / lang / f"{slug}.md"
            if live.is_file():
                v["state"] = "published"
                v["published_at"] = str(_front_matter(live).get("date") or iso(now))
                counts["published"] += 1
            elif v["state"] == "pending" and not draft.is_file():
                v["state"] = "rejected"
                counts["rejected"] += 1
            elif v["state"] == "awaiting_review" and now - parse_ts(st.first_seen) > timedelta(hours=expiry_hours):
                v["state"] = "rejected"
                counts["rejected"] += 1
        if st.status != "closed" and st.versions and all(v.get("state") == "rejected" for v in st.versions.values()):
            st.status = "closed"
            counts["closed"] += 1
    if any(counts.values()):
        save_stories(state_dir, stories)
    log.info("reconcile published=%d rejected=%d closed=%d updates_applied=%d updates_rejected=%d", *(counts[k] for k in (
        "published", "rejected", "closed", "updates_applied", "updates_rejected")))
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
