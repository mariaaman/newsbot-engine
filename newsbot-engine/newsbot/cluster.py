"""Group items about the same event into stories; attach to known stories from state.
Lexical fallback always works offline. Cross-language linking needs an embedder
(Cloudflare Workers AI bge-m3 or current multilingual model, VERIFY): embedder(texts) -> vectors."""
from __future__ import annotations

import hashlib
import logging
import math
import unicodedata
from typing import Callable, Sequence

from newsbot.models import Cluster, Item, Story

Embedder = Callable[[list[str]], list[list[float]]]
log = logging.getLogger("newsbot.cluster")

# Bengali, Arabic-Indic, Persian/Urdu and Devanagari digits -> ASCII (name kept: extract.py imports it)
_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹०१२३४५६७८९", "0123456789" * 4)
STOP = frozenset("""a an the and or of in on at to for from by with as is are was were be been
has have had will would says said say after before over under into new this that it its their
এবং ও এর এই করে করা জন্য থেকে হয়েছে হবে না কি যে তার তিনি তারা একটি এক সঙ্গে নিয়ে আজ
de la el los las un una y en que por con para del se su al lo como más
le les des du et dans pour sur par au aux avec ce cette ses son sa est sont qui
der die das den dem ein eine einer und zu mit von für auf ist sind im am bei nach aus
o os as um uma do da dos das e em no na nos nas ao
yang dan di ke dari untuk dengan pada ini itu akan telah oleh dalam sebagai atau juga
ve bir bu için ile da ne ama gibi çok daha
и в во не на что с по из за от как это для к о у но он она они же бы
के का की है में और से को पर यह कि था थे ने भी हैं एक
کے کا کی ہے میں اور سے کو پر یہ کہ تھا ہیں بھی ایک
في من على إلى عن مع أن هذا هذه التي الذي كان بعد قبل ما لا و""".split())

# Chinese / Japanese are written without spaces: lexical matching needs character bigrams.
_CJK = ((0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _CJK)


def _split_cjk(tok: str) -> list[str]:
    """'東京で地震発生' -> ['東京', '京で', ...]; non-CJK parts of a mixed token are kept whole."""
    if not any(_is_cjk(c) for c in tok):
        return [tok]
    out, run, other = [], [], []
    for ch in tok + "\0":
        if ch != "\0" and _is_cjk(ch):
            if other:
                out.append("".join(other))
                other = []
            run.append(ch)
            continue
        if run:
            out += ["".join(run[i:i + 2]) for i in range(len(run) - 1)] if len(run) > 1 else []
            run = []
        if ch != "\0":
            other.append(ch)
    if other:
        out.append("".join(other))
    return out


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
    toks = [p for t in toks for p in _split_cjk(t)]
    return [t for t in toks if t not in STOP and (len(t) > 1 or t.isdigit())]


def jaccard(a: frozenset[str] | set[str], b: frozenset[str] | set[str], min_shared: int = 2) -> float:
    inter = len(a & b)
    if inter < min_shared:
        return 0.0
    return inter / len(a | b)


def _unit(v: Sequence[float]) -> list[float]:
    n = math.sqrt(math.sumprod(v, v))
    return [x / n for x in v] if n else list(v)


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
                  known: dict[str, Story] | None = None, vec_out: dict | None = None) -> list[Cluster]:
    """vec_out (optional): filled with item url -> unit vector, so later stages (research) can reuse the embeddings."""
    cc = cfg.clustering
    n = len(items)
    toks = [frozenset(tokenize(i.title)) for i in items]
    vecs = None
    if embedder and n:
        try:
            vecs = [_unit(v) for v in embedder([i.title for i in items])]
        except Exception as e:      # rate limit, daily allowance spent, network: fall back to same-language matching
            log.warning("embeddings unavailable reason=%s, clustering by words only", type(e).__name__)
    if vecs and vec_out is not None:
        vec_out.update({it.url: v for it, v in zip(items, vecs)})
    uf = _UF(n)
    for i in range(n):
        for j in range(i + 1, n):
            same_lang = items[i].lang == items[j].lang
            lex = jaccard(toks[i], toks[j], cc.min_shared_tokens) if same_lang else 0.0
            emb = math.sumprod(vecs[i], vecs[j]) if vecs else 0.0     # unit vectors: dot product = cosine
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
