"""Gate: NEW / UPDATE / SKIP per story, honouring daily caps. Most hours end here with zero LLM calls.
Final proof of 'new confirmed fact' happens in verify (M3); here we use a cheap proxy:
>= 2 new items from distinct trusted domains, or a status-change keyword in a new trusted title."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from newsbot.cluster import tokenize
from newsbot.models import Story, parse_ts
from newsbot.score import Scored
from newsbot.verify import org_key
from newsbot.state import updates_today

STATUS_WORDS = frozenset("""arrested arrest charged cancelled canceled postponed resigns resigned
statement confirms confirmed correction retracted announced suspended banned released
গ্রেপ্তার বাতিল স্থগিত পদত্যাগ বিবৃতি ঘোষণা সংশোধন নিষিদ্ধ মুক্তি""".split())


@dataclass
class Decision:
    story_id: str
    action: str                      # NEW | UPDATE | SKIP
    reason: str
    route: list = field(default_factory=list)   # languages to write: ["bn"], ["en"], ["bn","en"]
    score: float = 0.0
    components: dict = field(default_factory=dict)
    queued: bool = False             # high-score story that did not fit today's caps

    def log_dict(self) -> dict:      # IDs and numbers only
        return {"story_id": self.story_id, "action": self.action, "reason": self.reason,
                "route": self.route, "score": self.score, "components": self.components,
                "queued": self.queued}


def route_for(s: Scored, cfg) -> list[str]:
    """Which site languages to write. cross_language (default): a story found in ANY language (a Japanese item,
    an Urdu one ...) is written in every site language once enough independent organisations cover it;
    the writers work from the language-neutral fact sheet, not from a translation."""
    lim = cfg.limits
    if getattr(lim, "routing_mode", "same_language") == "cross_language":
        orgs = {org_key(d) for d in s.trusted_domains}
        return list(cfg.site.languages) if len(orgs) >= lim.routing_min_orgs else []
    need = lim.routing_min_domains_per_language
    langs = [l for l in cfg.site.languages if len(s.trusted_by_lang.get(l, ())) >= need]
    return langs


def _threshold(cfg, langs: list[str]) -> float:
    per = cfg.scoring.per_language_threshold
    vals = [per.get(l, cfg.scoring.threshold) for l in langs] or [cfg.scoring.threshold]
    return min(vals)


def has_new_confirmed_signal(s: Scored, story: Story) -> bool:
    seen = set(story.urls)
    new = [i for i in s.cluster.items if i.url not in seen and i.tier <= 2]
    if len({i.domain for i in new}) >= 2:
        return True
    return any(STATUS_WORDS & set(tokenize(i.title)) for i in new)


def decide(scored: list[Scored], known: dict[str, Story], cfg, now: datetime,
           day_start: datetime, new_today_count: int) -> list[Decision]:
    lim = cfg.limits
    decisions: list[Decision] = []
    new_count = new_today_count
    new_this_run = 0
    for s in scored:                              # already sorted by score desc
        sid = s.cluster.story_id
        story = known.get(sid)

        def mk(action, reason, route=None, queued=False):
            return Decision(sid, action, reason, route or [], s.score, s.components, queued)

        if s.blocked:
            decisions.append(mk("SKIP", "blocked_topic"))
            continue
        if story is not None:
            if story.status == "closed":
                decisions.append(mk("SKIP", "story_closed"))
                continue
            if not any(v.get("state", "published") == "published" for v in story.versions.values()):
                decisions.append(mk("SKIP", "awaiting_review"))     # nothing live yet to update (PR / pending draft)
                continue
            if any(v.get("update_state") for v in story.versions.values()):
                decisions.append(mk("SKIP", "update_awaiting_review"))   # an earlier update is still in a PR / pending
                continue
            last = parse_ts(story.last_updated or story.first_seen)
            routed = route_for(s, cfg)
            missing = [l for l in routed if l not in story.versions]     # language never written (queued/failed at NEW time)
            if now - last < timedelta(hours=lim.min_hours_between_updates):
                decisions.append(mk("SKIP", "too_soon_since_update"))
            elif updates_today(story, day_start) >= lim.max_updates_per_story_per_day:
                decisions.append(mk("SKIP", "update_cap_reached"))
            else:
                signal = has_new_confirmed_signal(s, story)
                if not signal and not missing:
                    decisions.append(mk("SKIP", "no_new_confirmed_fact"))
                else:
                    existing = ([l for l in routed if l in story.versions] or list(story.versions)) if signal else []
                    decisions.append(mk("UPDATE", "new_trusted_facts" if signal else "new_language_version",
                                        existing + missing))
            continue
        # unknown story -> NEW?
        if len(s.trusted_domains) < lim.min_independent_sources:
            decisions.append(mk("SKIP", "too_few_trusted_sources"))
            continue
        langs = route_for(s, cfg)
        if not langs:
            decisions.append(mk("SKIP", "no_language_with_enough_sources"))
            continue
        if s.score < _threshold(cfg, langs):
            decisions.append(mk("SKIP", "below_threshold"))
            continue
        if new_count >= lim.max_new_per_day:
            decisions.append(mk("SKIP", "daily_cap_reached", langs, queued=True))
            continue
        if new_this_run >= getattr(lim, "max_new_per_run", 10**6):
            decisions.append(mk("SKIP", "run_cap_reached", langs, queued=True))    # next hour picks it up
            continue
        new_count += 1
        new_this_run += 1
        decisions.append(mk("NEW", "trending_and_verified_breadth", langs))
    return decisions
