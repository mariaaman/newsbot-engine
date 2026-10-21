"""Per-source claim extraction (small LLM). Output is validated and then mechanically checked
against the source text, so the model cannot add facts: numbers and quotes must appear verbatim."""
from __future__ import annotations

import logging
import re
import unicodedata

from newsbot.cluster import _BN_DIGITS, tokenize
from newsbot.models import Claim, Source
from newsbot.prompts import load_prompt, wrap_untrusted

log = logging.getLogger("newsbot.extract")
KINDS = ("who", "what", "when", "where", "number", "quote")
MAX_CLAIMS = 40            # per source (cfg.research.claims_per_source lowers it); fuller articles need more facts
MAX_CLAIM_CHARS = 300
MAX_QUOTE_WORDS = 15
GROUNDING = 0.6   # share of a claim's content tokens that must occur in the source text


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_BN_DIGITS).lower()
    s = s.translate(str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'}))
    return re.sub(r"\s+", " ", s).strip()


_MULT = {"thousand": 10**3, "thousands": 10**3, "হাজার": 10**3, "lakh": 10**5, "lakhs": 10**5, "lac": 10**5,
         "লাখ": 10**5, "লক্ষ": 10**5, "million": 10**6, "millions": 10**6, "মিলিয়ন": 10**6, "crore": 10**7,
         "crores": 10**7, "কোটি": 10**7, "billion": 10**9, "billions": 10**9, "বিলিয়ন": 10**9,
         # other languages (matched after NFKC + lowercase)
         "millones": 10**6, "millón": 10**6, "milhões": 10**6, "milhão": 10**6, "millionen": 10**6,
         "milliards": 10**9, "milliarden": 10**9, "milhar": 10**3, "tausend": 10**3, "juta": 10**6, "ribu": 10**3,
         "миллион": 10**6, "миллиона": 10**6, "миллионов": 10**6, "тысяч": 10**3, "тысячи": 10**3, "миллиард": 10**9,
         "миллиарда": 10**9, "миллиардов": 10**9,
         "हज़ार": 10**3, "हजार": 10**3, "लाख": 10**5, "करोड़": 10**7, "करोड": 10**7, "अरब": 10**9,
         "ہزار": 10**3, "لاکھ": 10**5, "کروڑ": 10**7, "ارب": 10**9,
         "ألف": 10**3, "الف": 10**3, "مليون": 10**6, "ملايين": 10**6, "مليار": 10**9, "مليارات": 10**9}
_MULT_N = {unicodedata.normalize("NFKC", k).lower(): v for k, v in _MULT.items()}
# East-Asian units follow the digit directly ("5万人"), so they need no word boundary.
_CJK_MULT = {"千": 10**3, "万": 10**4, "萬": 10**4, "億": 10**8, "亿": 10**8, "천": 10**3, "만": 10**4, "억": 10**8, "조": 10**12}
# "(?!\w)" instead of "\b": Bengali/Hindi words often end in a vowel SIGN (e.g. কোটি), which is not a \w character.
_NUM = re.compile(r"(\d+(?:[.,]\d+)*)(?:\s*(" + "|".join(sorted(map(re.escape, _MULT_N), key=len, reverse=True)) + r")(?!\w)"
                  r"|\s*(" + "|".join(_CJK_MULT) + r"))?", re.I)


def numbers_in(s: str) -> set[str]:
    """Canonical figures: Bengali digits -> ASCII, commas dropped, lakh/crore/million applied
    (so '৪ লাখ' == '400,000' == '0.4 million')."""
    from decimal import Decimal, InvalidOperation
    out = set()
    for num, mult, cjk in _NUM.findall(re.sub(r"(?<=\d):00(?!\d)", "", norm_text(s))):
        try:
            v = Decimal(num.replace(",", "")) * (_MULT_N[mult.lower()] if mult else _CJK_MULT[cjk] if cjk else 1)
        except InvalidOperation:
            continue
        out.add(str(int(v)) if v == v.to_integral() else str(v.normalize()))
    return out


def validate_claims(data, limit: int = MAX_CLAIMS) -> list[dict]:
    """schema for LLM.complete: {"claims": [{"kind":..., "text":...}]} -> list of clean dicts."""
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        raise ValueError("expected an object with a 'claims' list")
    out = []
    for c in data["claims"][:limit]:
        if not isinstance(c, dict):
            raise ValueError("claim must be an object")
        kind, text = c.get("kind"), c.get("text")
        if kind not in KINDS or not isinstance(text, str):
            raise ValueError("claim needs kind in %s and text" % (KINDS,))
        out.append({"kind": kind, "text": text.strip()})
    return out


def check_claim(kind: str, text: str, source_text: str) -> bool:
    """Deterministic guard against hallucinated or injected content."""
    if not text or len(text) > MAX_CLAIM_CHARS or re.search(r"(?:https?|ftp)://|\bwww\.|[\w.+-]+@[\w-]+\.\w|<[a-z/!]", text, re.I):
        return False
    src = norm_text(source_text)
    if kind == "quote":
        return len(text.split()) <= MAX_QUOTE_WORDS and norm_text(text).strip("\"' ") in src
    nums = numbers_in(text)
    if nums and not nums <= numbers_in(source_text):
        return False
    toks = set(tokenize(text))                       # grounding: a claim must be built from the source's own words
    return not toks or len(toks & set(tokenize(source_text))) / len(toks) >= GROUNDING


def extract_claims(llm, brain, source: Source, max_claims: int = MAX_CLAIMS) -> list[Claim]:
    msgs = [{"role": "system", "content": load_prompt(brain, "extract")},
            {"role": "user", "content": wrap_untrusted("source_data", source.text, id=source.id,
                                                       domain=source.domain, lang=source.lang)}]
    raw = llm.complete("extract", msgs, schema=lambda d: validate_claims(d, max_claims), max_tokens=min(6000, 1500 + 80 * max_claims))
    claims, dropped = [], 0
    for c in raw:
        if check_claim(c["kind"], c["text"], source.text):
            claims.append(Claim(f"{source.id}.C{len(claims) + 1}", source.id, c["kind"], c["text"]))
        else:
            dropped += 1
    log.info("extract source=%s claims=%d dropped=%d", source.id, len(claims), dropped)
    return claims
