"""Trend score. No LLM. score = w1*velocity + w2*breadth + w3*trends + w4*recency
+ w5*topic_fit - w6*saturation. All weights and norms come from config."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from newsbot.cluster import tokenize
from newsbot.models import Cluster, Trend
from newsbot.verify import org_key


@dataclass
class Scored:
    cluster: Cluster
    score: float
    components: dict
    blocked: bool
    trusted_domains: set = field(default_factory=set)
    trusted_orgs: set = field(default_factory=set)         # independent organisations (bbc.com + bbc.co.uk = one)
    trusted_by_lang: dict = field(default_factory=dict)   # lang -> set of trusted domains


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def trends_component(tokens: frozenset[str], trends: list[Trend], top_n: int) -> float:
    """Rank 1 -> 1.0, falling linearly to 0 at top_n. A trend matches when at least two thirds
    of its keyword tokens appear in the story tokens (and it has at least one token)."""
    best = 0.0
    for t in trends:
        kt = frozenset(tokenize(t.keyword))
        if kt and len(kt & tokens) / len(kt) >= 0.67:
            best = max(best, _clamp(1 - (t.rank - 1) / top_n))
    return best


def topic_component(tokens: frozenset[str], site) -> tuple[float, bool]:
    blocked = {w for b in site.blocked_topics for w in tokenize(b)}
    if blocked & tokens:
        return 0.0, True
    allowed = {w for a in site.allowed_topics for w in tokenize(a)}
    if not allowed:
        return 1.0, False
    return (1.0 if allowed & tokens else 0.3), False


def score_cluster(c: Cluster, trends: list[Trend], cfg, now: datetime) -> Scored:
    sc = cfg.scoring
    w = sc.weights
    trusted = [i for i in c.items if i.tier <= 2]
    trusted_domains = {i.domain for i in trusted}
    by_lang: dict[str, set] = {}
    for i in trusted:
        by_lang.setdefault(i.lang, set()).add(i.domain)

    window_start = now - timedelta(hours=sc.velocity_window_hours)
    trusted_orgs = {org_key(d) for d in trusted_domains}
    recent_orgs = {org_key(i.domain) for i in trusted if i.published >= window_start}
    velocity = _clamp(len(recent_orgs) / sc.velocity_norm)
    breadth = _clamp(len(trusted_orgs) / sc.breadth_norm)
    trends_c = trends_component(c.tokens, trends, sc.trends_top_n)
    newest = max(i.published for i in c.items)
    age_h = max(0.0, (now - newest).total_seconds() / 3600)
    recency = 0.5 ** (age_h / sc.recency_half_life_hours)
    topic, blocked = topic_component(c.tokens, cfg.site)
    all_domains = {i.domain for i in c.items}
    saturation = _clamp((len(all_domains) - sc.saturation_start) / sc.saturation_span)

    comp = {"velocity": velocity, "breadth": breadth, "trends": trends_c, "recency": recency,
            "topic_fit": topic, "saturation": saturation}
    score = (w.velocity * velocity + w.breadth * breadth + w.trends * trends_c
             + w.recency * recency + w.topic_fit * topic - w.saturation * saturation)
    return Scored(cluster=c, score=round(score, 4), components={k: round(v, 4) for k, v in comp.items()},
                  blocked=blocked, trusted_domains=trusted_domains, trusted_by_lang=by_lang,
                  trusted_orgs=trusted_orgs)


def score_all(clusters: list[Cluster], trends: list[Trend], cfg, now: datetime) -> list[Scored]:
    return sorted((score_cluster(c, trends, cfg, now) for c in clusters),
                  key=lambda s: (-s.score, s.cluster.story_id))
