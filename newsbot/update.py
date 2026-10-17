"""M7 update flow (spec section 7). An UPDATE decision from the gate becomes: re-gather only the NEW sources,
extract + verify them, merge the result into the story's saved fact sheet (old source ids stay valid), and
continue only if the merge holds a new confirmed fact or a status change. Then the update.md prompt rewrites the
published article, code enforces the rules (same slug/URL, `date` kept, timeline only grows, dated corrections
log, never silently rewrite facts) and the normal QA gates run with the old article as context.
Also adds a language version that was missing at NEW time (queued/failed/not enough sources then)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from newsbot.cluster import tokenize
from newsbot.extract import extract_claims, norm_text, numbers_in
from newsbot.facts import load_fact_sheet, save_fact_sheet
from newsbot.fetch import fetch_sources
from newsbot.gate import STATUS_WORDS
from newsbot.llm import BudgetExhausted, LLMError
from newsbot.models import Cluster, Fact, FactSheet, domain_of, iso
from newsbot.qa import _MARKER, strip_markers
from newsbot.state import load_stories
from newsbot.verify import build_fact_sheet, org_key

log = logging.getLogger("newsbot.update")

MAX_FACTS = 40
SAME_FACT = 0.6          # token overlap at or above this = the same fact, rephrased
CHANGED_FIGURE = 0.4     # similar statement but different figures = the fact changed (candidate correction)
UPDATE_NOTE = {"en": "Article updated with new verified facts.",
               "bn": "নতুন যাচাই করা তথ্য যোগ করে প্রতিবেদন হালনাগাদ করা হয়েছে।"}


def stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M UTC")


# ---------------- previous article ----------------

def load_published(brain, lang: str, slug: str) -> tuple[dict, str] | None:
    path = Path(brain) / "content" / lang / f"{slug}.md"
    try:
        parts = path.read_text(encoding="utf-8").split("---", 2)
        fm = yaml.safe_load(parts[1]) or {}
    except (OSError, IndexError, yaml.YAMLError):
        return None
    return (fm, parts[2].strip()) if len(parts) == 3 and isinstance(fm, dict) else None


def previous_article(fm: dict, body: str) -> dict:
    """The published article in the writer's JSON shape (what update.md calls <draft_data>)."""
    cats = fm.get("categories") or ["world"]
    return {"title": fm.get("title", ""), "summary": fm.get("summary", ""), "body_markdown": body,
            "timeline": [{"time": str(e.get("time", "")), "text": str(e.get("text", ""))} for e in fm.get("timeline") or []],
            "confirmed": list(fm.get("confirmed") or []), "unclear": list(fm.get("unclear") or []),
            "disputed": list(fm.get("disputed") or []), "sources": list(fm.get("sources") or []),
            "category": cats[0], "slug": fm.get("slug", ""), "corrections": list(fm.get("corrections") or [])}


def fact_sheet_from_article(story_id: str, fm: dict) -> FactSheet:
    """Fallback when state/factsheets/<id>.json is missing: rebuild the facts from what the article itself states."""
    lang = fm.get("lang", "")
    sources = [{"id": s["id"], "name": s.get("name", ""), "tier": s.get("tier", 3), "url": s.get("url", ""),
                "domain": domain_of(s.get("url", "")), "lang": lang, "published_at": s.get("published_at", "")}
               for s in fm.get("sources") or [] if isinstance(s, dict) and s.get("id")]
    facts = []
    for key, status in (("confirmed", "confirmed"), ("unclear", "reported"), ("disputed", "disputed")):
        for text in fm.get(key) or []:
            ids = sorted({f"S{m}" for m in _MARKER.findall(text)})
            facts.append(Fact(f"F{len(facts) + 1}", "what", strip_markers(text).strip(), status, [], ids, ""))
    return FactSheet(story_id, int(fm.get("fact_sheet_version") or 1), facts, sources)


# ---------------- merging fact sheets ----------------

def _overlap(a: str, b: str) -> float:
    ta, tb = frozenset(tokenize(a)), frozenset(tokenize(b))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def relation(new: str, old: str) -> str | None:
    """'same' (rephrased old fact), 'changed' (same subject, different figures) or None (unrelated)."""
    j, na, nb = _overlap(new, old), numbers_in(new), numbers_in(old)
    if na and nb and na != nb:
        return "changed" if j >= CHANGED_FIGURE else None
    return "same" if j >= SAME_FACT or (na and na == nb and j >= 0.3) else None


def _status(source_ids: list[str], by_id: dict, disputed: bool) -> str:
    if disputed:
        return "disputed"
    orgs = {org_key(by_id[i]["domain"]) for i in source_ids if i in by_id and by_id[i].get("tier", 3) <= 2}
    return "confirmed" if len(orgs) >= 2 else "reported"


def merge_fact_sheets(old: FactSheet, new: FactSheet, min_words: int = 5) -> tuple[FactSheet, list[str]]:
    """Old facts and source ids are kept (the published article cites them). New sources get the next ids.
    Returns (merged sheet with version old+1, ids of facts that justify an update). A fact justifies an update when
    it is new (not a rephrasing of an old one), longer than `min_words`, and either confirmed by 2+ independent
    organisations or a status change; or when a rephrased old fact became confirmed through the new sources."""
    nums = [int(s["id"][1:]) for s in old.sources if s["id"][1:].isdigit()]
    top, by_url, remap, sources = max(nums, default=0), {s["url"]: s["id"] for s in old.sources}, {}, list(old.sources)
    for s in new.sources:
        if s["url"] in by_url:
            remap[s["id"]] = by_url[s["url"]]
        else:
            top += 1
            remap[s["id"]] = f"S{top}"
            sources.append({**s, "id": f"S{top}"})
    by_id = {s["id"]: s for s in sources}
    facts = [Fact(f.id, f.kind, f.statement, f.status, list(f.claim_ids), list(f.source_ids), f.note) for f in old.facts]
    n_old, fresh, significant = len(facts), set(), []
    next_id = max([int(f.id[1:]) for f in facts if f.id[1:].isdigit()], default=0)
    for nf in new.facts:
        ids = sorted({remap[i] for i in nf.source_ids if i in remap})
        rel, match = None, None
        for f in facts[:n_old]:
            r = relation(nf.statement, f.statement)
            if r == "same" or (r == "changed" and rel is None):
                rel, match = r, f
                if r == "same":
                    break
        words_ok = len(nf.statement.split()) >= min_words
        status_change = bool(STATUS_WORDS & set(tokenize(nf.statement)))
        if rel == "same":
            before = match.status
            match.source_ids = sorted(set(match.source_ids) | set(ids))
            match.status = _status(match.source_ids, by_id, "disputed" in (before, nf.status))
            if before != "confirmed" and match.status == "confirmed" and words_ok:
                significant.append(match.id)
            continue
        next_id += 1
        fid = f"F{next_id}"
        note = nf.note
        if rel == "changed":
            note = (note + " " if note else "") + f"later figures differ from {match.id}"
            match.note = (match.note + " " if match.note else "") + f"later reports give different figures ({fid})"
        facts.append(Fact(fid, nf.kind, nf.statement, nf.status, [], ids, note))
        fresh.add(fid)
        if words_ok and (nf.status == "confirmed" or status_change):
            significant.append(fid)
    while len(facts) > MAX_FACTS:          # keep the prompt bounded: drop the oldest single-source facts first
        victim = next((f for f in facts if f.status == "reported" and f.id not in fresh), None)
        if victim is None:
            break
        facts.remove(victim)
    cited = {i for f in facts for i in f.source_ids}
    merged = FactSheet(old.story_id, old.version + 1, facts, [s for s in sources if s["id"] in cited])
    merged.source_texts = {remap[i]: t for i, t in new.source_texts.items() if i in remap}
    return merged, significant


def gather_update_facts(cluster: Cluster, cfg, brain, llm, fetcher, old: FactSheet, *,
                        allow_empty: bool = False) -> tuple[FactSheet | None, list[str], str]:
    """(merged sheet, significant fact ids, status): ok | no_new_fact | insufficient_sources | queued_budget | llm_failed.
    allow_empty: a missing language version is being added, so 'nothing new' is not a reason to stop."""
    seen = {s["url"] for s in old.sources}
    fresh = [i for i in cluster.items if i.url not in seen]
    sources = fetch_sources(Cluster(cluster.story_id, fresh, True, cluster.tokens), cfg, fetcher)
    if not sources:
        return None, [], "insufficient_sources"
    est = sum(len(s.text) for s in sources) // 2 + 1500 * len(sources)
    if not (llm.can_run("extract", est) and llm.can_run("verify", est)):
        return None, [], "queued_budget"
    try:
        claims = [c for s in sources for c in extract_claims(llm, brain, s)]
        if not claims:
            return None, [], "insufficient_sources"
        new = build_fact_sheet(llm, brain, cluster.story_id, sources, claims, old.version + 1)
    except BudgetExhausted:
        return None, [], "queued_budget"
    except LLMError as e:
        log.warning("update facts failed story=%s error=%s", cluster.story_id, type(e).__name__)
        return None, [], "llm_failed"
    new.source_texts = {s.id: s.text for s in sources}
    merged, significant = merge_fact_sheets(old, new, cfg.limits.min_update_fact_words)
    if not significant and not allow_empty:
        return None, [], "no_new_fact"
    return merged, significant, "ok"


# ---------------- rules applied to the written article ----------------

def finalize_update(art: dict, prev: dict, lang: str, now: datetime) -> dict:
    """Code enforces what a prompt can only ask for: same slug and category, the timeline only grows (old entries
    first, new ones dated), corrections are kept and dated (nothing is ever deleted)."""
    art = dict(art)
    art["slug"], art["category"] = prev["slug"], prev["category"]
    seen = {norm_text(e["text"]) for e in prev["timeline"]}
    added = [{"time": e["time"] or stamp(now), "text": e["text"]} for e in art["timeline"] if norm_text(e["text"]) not in seen]
    art["timeline"] = prev["timeline"] + (added or [{"time": stamp(now), "text": UPDATE_NOTE[lang]}])
    old = prev.get("corrections", [])
    keys = {(c["said"], c["correct"]) for c in old}
    art["corrections"] = old + [{**c, "date": c.get("date") or stamp(now)} for c in art.get("corrections", [])
                                if (c["said"], c["correct"]) not in keys]
    return art


def _ts_str(v) -> str:
    return iso(v) if isinstance(v, datetime) else str(v or "")      # YAML parses unquoted ISO timestamps to datetime


def _sheet_from_any_version(brain, story, live: dict) -> FactSheet | None:
    if live:
        return fact_sheet_from_article(story.story_id, next(iter(live.values()))[0])
    for lang, v in story.versions.items():
        loaded = load_published(brain, lang, v.get("slug", "")) if v.get("state", "published") == "published" else None
        if loaded:
            return fact_sheet_from_article(story.story_id, loaded[0])
    return None


@dataclass
class UpdateOutcome:
    story_id: str
    status: str                                   # ok | no_new_fact | insufficient_sources | queued_budget | llm_failed | ...
    fact_sheet: FactSheet | None = None
    results: dict = field(default_factory=dict)   # lang -> ArticleResult
    updated: list = field(default_factory=list)   # languages whose published article was updated
    added: list = field(default_factory=list)     # languages newly written for this story
    dates: dict = field(default_factory=dict)     # lang -> original publication date (kept)
    fresh_urls: list = field(default_factory=list)


def run_updates(decisions, scored, cfg, brain, mode: str, llm, fetcher, embedder, now) -> dict[str, UpdateOutcome]:
    """Stage 5-10 for UPDATE decisions. Writes files (never in dry-run) and records story state (real runs only)."""
    from newsbot.articles import produce_articles, write_outputs
    from newsbot.publish import mark_seen, record_updates
    todo = [d for d in decisions if d.action == "UPDATE" and not d.queued]
    if not todo or llm is None:
        return {}
    brain, dry = Path(brain), mode == "dry-run"
    state_dir = brain / "state"
    stories = load_stories(state_dir)
    clusters = {s.cluster.story_id: s.cluster for s in scored}
    out: dict[str, UpdateOutcome] = {}
    for d in todo:
        story, cluster = stories.get(d.story_id), clusters.get(d.story_id)
        if story is None or cluster is None:
            continue
        live = {}
        for lang in d.route:
            v = story.versions.get(lang)
            if v and v.get("state", "published") == "published":
                loaded = load_published(brain, lang, v["slug"])
                if loaded:
                    live[lang] = loaded
        new_langs = [l for l in d.route if l not in story.versions]
        if not live and not new_langs:
            d.reason += "|no_live_article"
            continue
        old = load_fact_sheet(state_dir, d.story_id) or _sheet_from_any_version(brain, story, live)
        if old is None:
            d.reason += "|no_fact_sheet"
            continue
        merged, sig, status = gather_update_facts(cluster, cfg, brain, llm, fetcher, old, allow_empty=bool(new_langs))
        d.reason += f"|facts:{status}"
        known_urls = {s["url"] for s in old.sources}
        fresh_urls = sorted({i.url for i in cluster.items if i.url not in known_urls})
        out[d.story_id] = oc = UpdateOutcome(d.story_id, status, merged, fresh_urls=fresh_urls)
        if merged is None:
            if status == "queued_budget":
                d.queued = True
            elif status == "no_new_fact" and not dry:
                mark_seen(state_dir, d.story_id, fresh_urls)     # examined and judged not new: do not re-pay every hour
            log.info("update story=%s status=%s", d.story_id, status)
            continue
        update_langs = list(live) if sig else []
        previous = {l: previous_article(*live[l]) for l in update_langs}
        oc.results = produce_articles(merged, update_langs + new_langs, cfg, brain, llm, embedder=embedder,
                                      previous=previous, now=now)
        oc.updated = [l for l in update_langs if oc.results[l].status in ("publish", "pending")]
        oc.added = [l for l in new_langs if oc.results[l].status in ("publish", "pending")]
        oc.dates = {l: _ts_str(live[l][0].get("date")) for l in update_langs}
        if not dry:
            write_outputs(brain, merged, oc.results, now, published_dates=oc.dates)
            save_ok = oc.updated or oc.added
            if save_ok:
                save_fact_sheet(state_dir, merged)
        for r in oc.results.values():
            d.reason += f"|{r.lang}:{r.status}"
        log.info("update story=%s updated=%s added=%s", d.story_id, ",".join(oc.updated) or "-", ",".join(oc.added) or "-")
    if not dry and out:
        record_updates(state_dir, out, now, mode)
    return out
