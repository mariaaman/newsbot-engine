"""Group items about the same event into stories; attach to known stories from state.
Lexical fallback always works offline. Cross-language linking needs an embedder
(Cloudflare Workers AI bge-m3 or current multilingual model, VERIFY): embedder(texts) -> vectors."""
from __future__ import annotations

import hashlib
import math
import unicodedata
from typing import Callable, Sequence

from newsbot.models import Cluster, Item, Story

Embedder = Callable[[list[str]], list[list[float]]]

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
STOP = frozenset("""a an the and or of in on at to for from by with as is are was were be been
has have had will would says said say after before over under into new this that it its their
এবং ও এর এই করে করা জন্য থেকে হয়েছে হবে না কি যে তার তিনি তারা একটি এক সঙ্গে নিয়ে আজ""".split())


def tokenize(text: str) -> list[str]:
    """Normalised content tokens. Bengali digits -> ASCII. We keep letters, numbers AND
    combining marks (category M*): Python's \\w drops Bengali vowel signs and splits words."""
    text = unicodedata.normalize("NFKC", text).translate(_BN_DIGITS).lower()
    toks, cur = [], []
    for ch in text:
        if unicodedata.category(ch)[0] in "LNM":
            cur.append(ch)
        else:
            if cur:
                toks.append("".join(cur))
                cur = []
    if cur:
        toks.append("".join(cur))
    return [t for t in toks if t not in STOP and (len(t) > 1 or t.isdigit())]


def jaccard(a: frozenset[str] | set[str], b: frozenset[str] | set[str], min_shared: int = 2) -> float:
    inter = len(a & b)
    if inter < min_shared:
        return 0.0
    return inter / len(a | b)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, i: int) -> int:
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def union(self, i: int, j: int) -> None:
        self.p[self.find(i)] = self.find(j)


def make_story_id(items: list[Item]) -> str:
    first = min(items, key=lambda i: (i.published, i.url))
    key = " ".join(sorted(set(tokenize(first.title)))) + "|" + first.published.date().isoformat()
    return "st-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def top_keywords(items: list[Item], n: int = 30) -> list[str]:
    counts: dict[str, int] = {}
    for it in items:
        for t in set(tokenize(it.title)):
            counts[t] = counts.get(t, 0) + 1
    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def cluster_items(items: list[Item], cfg, embedder: Embedder | None = None,
                  known: dict[str, Story] | None = None) -> list[Cluster]:
    cc = cfg.clustering
    n = len(items)
    toks = [frozenset(tokenize(i.title)) for i in items]
    vecs = embedder([i.title for i in items]) if (embedder and n) else None
    uf = _UF(n)
    for i in range(n):
        for j in range(i + 1, n):
            same_lang = items[i].lang == items[j].lang
            lex = jaccard(toks[i], toks[j], cc.min_shared_tokens) if same_lang else 0.0
            emb = cosine(vecs[i], vecs[j]) if vecs else 0.0
            if lex >= cc.lexical_threshold or emb >= cc.embedding_threshold:
                uf.union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    known = known or {}
    url_owner = {u: s.story_id for s in known.values() for u in s.urls}
    out: list[Cluster] = []
    for idxs in groups.values():
        members = [items[i] for i in idxs]
        tokens = frozenset().union(*(toks[i] for i in idxs))
        sid, is_known = None, False
        owners = [url_owner[m.url] for m in members if m.url in url_owner]
        if owners:
            sid, is_known = max(set(owners), key=owners.count), True
        else:
            best, best_sim = None, 0.0
            for s in known.values():
                sim = jaccard(tokens, set(s.keywords), cc.min_shared_tokens)
                if sim > best_sim:
                    best, best_sim = s, sim
            if best is not None and best_sim >= cc.story_match_threshold:
                sid, is_known = best.story_id, True
        out.append(Cluster(story_id=sid or make_story_id(members), items=members,
                           known=is_known, tokens=tokens))
    # merge clusters that resolved to the same known story id
    merged: dict[str, Cluster] = {}
    for c in out:
        if c.story_id in merged:
            m = merged[c.story_id]
            m.items += c.items
            m.tokens = m.tokens | c.tokens
        else:
            merged[c.story_id] = c
    return sorted(merged.values(), key=lambda c: c.story_id)
