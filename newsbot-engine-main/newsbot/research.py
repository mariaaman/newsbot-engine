"""Research for ONE selected story: before the pages are fetched, pull in related coverage that clustering left behind.

Clustering links two headlines when their multilingual embeddings are close (cluster.embedding_threshold, strict, so
unrelated stories are not chained together). A story that is covered in six languages often has outlets whose headline
is a little further from the rest: they end up alone. Here such single leftover items are attached to the story when
they are close to the story's CENTROID (cfg.research.attach_threshold, slightly looser). More organisations means more
verified facts and a fuller article.

Safety: only trusted (tier 1-2), direct-RSS items; only items that belong to no other story (a singleton cluster); the
verify prompt drops claims that are not about the main event; code, not the model, still decides confirmed / reported.
No network and no model call happens here: it reuses the embeddings made for clustering."""
from __future__ import annotations

import logging
import math
from datetime import timedelta

from newsbot.models import Cluster

log = logging.getLogger("newsbot.research")


def _unit_sum(vectors: list[list[float]]) -> list[float] | None:
    if not vectors:
        return None
    total = [math.fsum(col) for col in zip(*vectors)]
    n = math.sqrt(math.sumprod(total, total))
    return [x / n for x in total] if n else None


def expand_cluster(cluster: Cluster, pool, vectors: dict, size_of: dict, cfg) -> tuple[Cluster, int]:
    """Returns (cluster with extra items attached, number attached). The input cluster is not modified.
    pool: all items of this run; vectors: item url -> unit vector; size_of: item url -> size of its cluster."""
    rc = getattr(cfg, "research", None)
    if rc is None or rc.attach_threshold <= 0 or rc.max_extra_items <= 0 or not vectors:
        return cluster, 0
    members = {i.url for i in cluster.items}
    centroid = _unit_sum([vectors[i.url] for i in cluster.items if i.url in vectors])
    if centroid is None:
        return cluster, 0
    newest = max(i.published for i in cluster.items)
    window = timedelta(hours=rc.window_hours)
    scored = []
    for it in pool:
        if it.url in members or it.tier > 2 or it.kind != "rss" or it.url not in vectors:
            continue
        if size_of.get(it.url, 1) > 1:                      # already part of another story: leave it there
            continue
        if abs(newest - it.published) > window:
            continue
        sim = math.sumprod(centroid, vectors[it.url])
        if sim >= rc.attach_threshold:
            scored.append((sim, it))
    scored.sort(key=lambda t: (-t[0], t[1].url))
    extra = [it for _, it in scored[: rc.max_extra_items]]
    if not extra:
        return cluster, 0
    return Cluster(cluster.story_id, list(cluster.items) + extra, cluster.known, cluster.tokens), len(extra)


def make_expander(pool, clusters, vectors: dict, cfg):
    """A callable cluster -> cluster for gather_story_facts. Counts and ids only are logged (the log is public)."""
    size_of = {i.url: len(c.items) for c in clusters for i in c.items}

    def expand(cluster: Cluster) -> Cluster:
        out, n = expand_cluster(cluster, pool, vectors, size_of, cfg)
        if n:
            log.info("research story=%s attached=%d orgs_now=%d", cluster.story_id, n,
                     len({i.domain for i in out.items if i.tier <= 2}))
        return out
    return expand
