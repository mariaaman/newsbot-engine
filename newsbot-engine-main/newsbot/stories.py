"""Story status transitions (spec section 7): developing -> stable after `stable_after_hours` without news,
-> closed after `closed_after_days` without news; a stable story that gets a new confirmed signal is developing
again. 'News' = a new item from a trusted (tier 1-2) source about the story, not only our own updates.
Closed stories never reopen (a later event gets a new story). Runs every real run after reconcile."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from newsbot.gate import has_new_confirmed_signal
from newsbot.models import Story, iso, parse_ts

log = logging.getLogger("newsbot.stories")


def last_news(story: Story) -> datetime:
    return parse_ts(story.last_news or story.last_updated or story.first_seen)


def refresh_statuses(stories: dict[str, Story], scored: list, cfg, now: datetime) -> dict:
    """Mutates stories in place. Returns counts {news, reopened, stable, closed}; save when any is non-zero."""
    counts = {"news": 0, "reopened": 0, "stable": 0, "closed": 0}
    for s in scored:
        st = stories.get(s.cluster.story_id)
        if st is None or st.status == "closed":
            continue
        seen = set(st.urls)
        if any(i.url not in seen and i.tier <= 2 for i in s.cluster.items):
            st.last_news = iso(now)
            counts["news"] += 1
        if st.status == "stable" and has_new_confirmed_signal(s, st):
            st.status = "developing"
            counts["reopened"] += 1
    lim = cfg.limits
    for st in stories.values():
        if st.status == "closed":
            continue
        idle = now - last_news(st)
        if idle >= timedelta(days=lim.closed_after_days):
            st.status = "closed"
            counts["closed"] += 1
        elif st.status == "developing" and idle >= timedelta(hours=lim.stable_after_hours):
            st.status = "stable"
            counts["stable"] += 1
    log.info("statuses news=%d reopened=%d stable=%d closed=%d", *(counts[k] for k in ("news", "reopened", "stable", "closed")))
    return counts
