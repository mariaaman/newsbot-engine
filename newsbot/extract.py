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
MAX_CLAIMS = 25
MAX_CLAIM_CHARS = 300
MAX_QUOTE_WORDS = 15
GROUNDING = 0.6   # share of a claim's content tokens that must occur in the source text


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_BN_DIGITS).lower()
    s = s.translate(str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'}))
    return re.sub(r"\s+", " ", s).strip()


_MULT = {"thousand": 10**3, "হাজার": 10**3, "lakh": 10**5, "lac": 10**5, "লাখ": 10**5, "লক্ষ": 10**5,
         "million": 10**6, "মিলিয়ন": 10**6, "crore": 10**7, "কোটি": 10**7, "billion": 10**9, "বিলিয়ন": 10**9}
_NUM = re.compile(r"(\d+(?:[.,]\d+)*)(?:\s*(" + "|".join(_MULT) + r")\b)?", re.I)


def numbers_in(s: str) -> set[str]:
    """Canonical figures: Bengali digits -> ASCII, commas dropped, lakh/crore/million applied
    (so '৪ লাখ' == '400,000' == '0.4 million')."""
    from decimal import Decimal, InvalidOperation
    out = set()
    for num, mult in _NUM.findall(norm_text(s)):
        try:
            v = Decimal(num.replace(",", "")) * (_MULT[mult.lower()] if mult else 1)
        except InvalidOperation:
            continue
        out.add(str(int(v)) if v == v.to_integral() else str(v.normalize()))
    return out


def validate_claims(data) -> list[dict]:
    """schema for LLM.complete: {"claims": [{"kind":..., "text":...}]} -> list of clean dicts."""
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        raise ValueError("expected an object with a 'claims' list")
    out = []
    for c in data["claims"][:MAX_CLAIMS]:
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


def extract_claims(llm, brain, source: Source) -> list[Claim]:
    msgs = [{"role": "system", "content": load_prompt(brain, "extract")},
            {"role": "user", "content": wrap_untrusted("source_data", source.text, id=source.id,
                                                       domain=source.domain, lang=source.lang)}]
    raw = llm.complete("extract", msgs, schema=validate_claims, max_tokens=1500)
    claims, dropped = [], 0
    for c in raw:
        if check_claim(c["kind"], c["text"], source.text):
            claims.append(Claim(f"{source.id}.C{len(claims) + 1}", source.id, c["kind"], c["text"]))
        else:
            dropped += 1
    log.info("extract source=%s claims=%d dropped=%d", source.id, len(claims), dropped)
    return claims
