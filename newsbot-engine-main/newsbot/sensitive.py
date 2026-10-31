"""Sensitive-topic detection (spec rule 6). Sensitive drafts never auto-publish: they go to pending/ for
human review. Keyword lists come from config (`sensitive.keywords`); the built-in lists apply when the
config gives none. English entries match whole words ("word*" = prefix match); entries containing
non-ASCII characters (Bengali) match as substrings because Bengali has no stemming here.
Deliberately broad: a false positive costs a human glance, a false negative costs trust."""
from __future__ import annotations

import re
from functools import lru_cache

DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "death": ["died", "dead", "death*", "kill*", "fatal*", "casualt*", "drown*", "funeral*", "body found",
              "মৃত্যু", "মারা", "নিহত", "লাশ", "প্রাণহানি", "মৃত"],
    "crime": ["arrest*", "accus*", "alleg*", "charged", "convict*", "murder*", "rape*", "assault*", "fraud*",
              "indict*", "suspect*", "trafficking", "গ্রেপ্তার", "গ্রেফতার", "অভিযুক্ত", "অভিযোগ", "হত্যা",
              "ধর্ষণ", "খুন", "মামলা", "আসামি"],
    "health": ["vaccin*", "cancer", "disease*", "outbreak*", "epidemic", "pandemic", "virus*", "medic*",
               "treatment*", "cure", "symptom*", "dengue", "diagnos*", "clinical",
               "টিকা", "ক্যান্সার", "রোগ", "চিকিৎসা", "ভাইরাস", "প্রাদুর্ভাব", "ডেঙ্গু", "ওষুধ"],
    "election": ["election*", "vote", "votes", "voting", "voter*", "ballot*", "rigging", "rigged", "corrupt*",
                 "bribe*", "impeach*", "নির্বাচন", "ভোট", "দুর্নীতি", "ঘুষ", "কারচুপি"],
    "religion": ["communal", "religio*", "mosque*", "temple*", "church*", "blasphem*", "riot*", "sectarian",
                 "minorit*", "সাম্প্রদায়িক", "ধর্ম", "মসজিদ", "মন্দির", "গির্জা", "দাঙ্গা", "সংখ্যালঘু"],
    "minors": ["child", "children", "minor", "minors", "juvenile*", "teenager*", "underage", "under-age",
               "schoolgirl*", "schoolboy*", "শিশু", "কিশোর", "নাবালক", "অপ্রাপ্তবয়স্ক"],
    "financial_advice": ["invest in", "investment advice", "stock tip*", "buy shares", "should buy", "crypto*",
                         "bitcoin", "financial advice", "guaranteed return*", "বিনিয়োগ", "ক্রিপ্টো", "বিটকয়েন"],
}


@lru_cache(maxsize=32)
def _compile(entries: tuple[str, ...]) -> re.Pattern | None:
    parts = []
    for e in entries:
        e = e.strip().lower()
        if not e:
            continue
        if not e.isascii():
            parts.append(re.escape(e))
        elif e.endswith("*"):
            parts.append(r"\b" + re.escape(e[:-1]) + r"\w*")
        else:
            parts.append(r"\b" + re.escape(e) + r"\b")
    return re.compile("|".join(parts), re.I) if parts else None


def detect(text: str, keywords: dict[str, list[str]] | None = None) -> list[str]:
    """Sorted list of sensitive category names whose keywords occur in text."""
    kw = keywords or DEFAULT_KEYWORDS
    low = text.lower()
    return sorted(cat for cat, entries in kw.items()
                  if (rx := _compile(tuple(entries))) is not None and rx.search(low))
