"""Cheap, LLM-free repair of a draft that a gate rejected for an unsupported number, name or claim.

Instead of asking the model to write the whole article again (minutes and ~10k tokens per try), the engine edits the draft itself:
  1. a sentence with ONE wrong figure is corrected in place when the fact sheet gives an unambiguous answer
     (the sentence clearly describes one fact, and that fact has exactly one figure the sentence lacks: "12 May" -> "13 May");
  2. every other offending sentence (unsupported name, unsupported figure with no clear answer, a claim the fact check
     rejected) is REMOVED. A shorter true article beats a padded one.
The edited draft goes through ALL gates again; it is kept only if it has fewer failures than before. Nothing is ever added.
Gives up (returns None) when the edit would delete more than MAX_LOSS of the body or the summary would vanish."""
from __future__ import annotations

import copy
import re

from newsbot.extract import numbers_in
from newsbot.qa import _numbers_text, fact_vocab, offending_terms, raw_words, strip_markers
from newsbot.cluster import tokenize

MAX_LOSS = 0.35                    # most of the body (by words) that may be deleted before the draft goes to a human instead
FIXABLE = {"unsupported_name", "unsupported_number", "confirmed_unsupported_number", "unsupported_claims"}

_SENT = re.compile(r"(?<=[.!?।])\s+")
_TRAIL_MARKS = re.compile(r"((?:\s*\[S\d+\])+)\s*$")
_ONLY_MARKS = re.compile(r"^(?:\s*\[S\d+\])+\s*$")
_DIGITS = re.compile(r"[0-9০-৯]+(?:[.,][0-9০-৯]+)*")
_BN = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")


def _split(par: str) -> list[str]:
    """Sentences of a paragraph; a citation marker that follows the full stop stays with the sentence before it."""
    out: list[str] = []
    for s in _SENT.split(par.strip()):
        if out and _ONLY_MARKS.match(s):
            out[-1] += " " + s.strip()
        elif s.strip():
            out.append(s.strip())
    return out


def _canon(token: str) -> str:
    n = numbers_in(token)
    return next(iter(n)) if len(n) == 1 else ""


class _Ctx:
    def __init__(self, fs, bad_names, bad_nums, bad_conf_nums, claims):
        self.fs, self.names, self.nums, self.conf_nums = fs, bad_names, bad_nums, bad_conf_nums
        self.claims = [set(tokenize(c)) for c in claims if c]
        self.facts = [(set(t for t in tokenize(f.statement) if not t.isdigit()), f) for f in fs.facts]
        self.replaced = 0
        self.dropped = 0

    def bad_numbers(self, sent: str, conf: bool = False) -> set[str]:
        found = numbers_in(_numbers_text(sent))
        return found & (self.nums | (self.conf_nums if conf else set()))

    def bad_name(self, sent: str) -> bool:
        s = strip_markers(sent)
        return any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", s) for n in self.names)

    def bad_claim(self, sent: str) -> bool:
        toks = set(tokenize(strip_markers(sent)))
        return bool(toks) and any(c and len(c & toks) / len(c) >= 0.6 for c in self.claims)

    def correction(self, sent: str, bad: str) -> str | None:
        """The sentence with its one wrong figure replaced, or None when the fact sheet gives no unambiguous answer."""
        toks = {t for t in tokenize(strip_markers(sent)) if not t.isdigit()}
        if len(toks) < 4:
            return None
        scored = sorted(((len(toks & ft) / len(toks), len(toks & ft), f) for ft, f in self.facts), key=lambda x: -x[0])
        if not scored or scored[0][0] < 0.4 or scored[0][1] < 3:
            return None
        if len(scored) > 1 and scored[1][0] > scored[0][0] - 0.15:
            return None                                              # two facts fit about equally well: do not guess
        fact = scored[0][2]
        mine = numbers_in(_numbers_text(sent))
        options = numbers_in(fact.statement) - mine
        if len(options) != 1:
            return None
        new = next(iter(options))
        src = [m for m in _DIGITS.findall(fact.statement) if _canon(m) == new]
        old = [m for m in _DIGITS.finditer(sent) if _canon(m.group()) == bad]
        if len(src) != 1 or len(old) != 1:
            return None                                              # a figure written with words, lakh, million...: cannot swap safely
        repl = src[0].translate(_BN) if re.search(r"[০-৯]", old[0].group()) else src[0]
        m = old[0]
        return sent[:m.start()] + repl + sent[m.end():]

    def fix_sentence(self, sent: str, conf: bool = False) -> str | None:
        """The sentence as it should stay: unchanged, corrected, or None (remove it)."""
        bad_n = self.bad_numbers(sent, conf)
        if not bad_n and not self.bad_name(sent) and not self.bad_claim(sent):
            return sent
        if len(bad_n) == 1 and not self.bad_name(sent) and not self.bad_claim(sent):
            fixed = self.correction(sent, next(iter(bad_n)))
            if fixed and not self.bad_numbers(fixed, conf):
                self.replaced += 1
                return fixed
        self.dropped += 1
        return None


def _fix_paragraph(par: str, ctx: _Ctx) -> str:
    if par.lstrip().startswith("#"):
        return par
    keep: list[str] = []
    lost_marks = ""
    for s in _split(par):
        r = ctx.fix_sentence(s)
        if r is None:
            m = _TRAIL_MARKS.search(s)
            lost_marks += m.group(1) if m else ""
        else:
            keep.append(r)
    if not keep:
        return ""
    if lost_marks and not _TRAIL_MARKS.search(keep[-1]):                # the paragraph must still end with a citation
        keep[-1] += " " + " ".join(re.findall(r"\[S\d+\]", lost_marks))
    return " ".join(keep)


def autofix_article(art: dict, fs, lang: str, qa, lead: dict | None = None) -> tuple[dict, int, int] | None:
    """(edited copy, sentences corrected, sentences removed) or None when nothing can or should be done."""
    if not set(qa.failures) & FIXABLE:
        return None
    nums, _vocab, conf = fact_vocab(fs)
    names, numbers = offending_terms(art, fs, lead)
    # act only on what the gates actually rejected: a language without a name check (es, fr...) must not lose sentences over "names"
    names = names if "unsupported_name" in qa.failures else []
    numbers = numbers if "unsupported_number" in qa.failures else []
    claims = [str(c.get("claim", c) if isinstance(c, dict) else c) for c in
              qa.details.get("unsupported_claims", {}).get("unsupported", [])]
    claims = claims if "unsupported_claims" in qa.failures else []
    ctx = _Ctx(fs, names, set(numbers), set(), claims)
    out = copy.deepcopy(art)

    body_before = len(" ".join(art["body_markdown"].split()).split())
    paras = [p for p in re.split(r"\n\s*\n", out["body_markdown"]) if p.strip()]
    out["body_markdown"] = "\n\n".join(x for x in (_fix_paragraph(p, ctx) for p in paras) if x)
    body_after = len(" ".join(out["body_markdown"].split()).split())
    if body_before and (body_before - body_after) / body_before > MAX_LOSS:
        return None

    summ = " ".join(x for x in (ctx.fix_sentence(s) for s in _split(out["summary"])) if x)
    if not summ:
        return None
    out["summary"] = summ

    def fix_items(items: list[str], conf_list: bool = False) -> list[str]:
        kept = []
        for it in items:
            r = ctx.fix_sentence(it, conf=conf_list)
            if r is not None:
                kept.append(r)
        return kept

    cn = ctx.conf_nums
    ctx.conf_nums = set()
    # the "confirmed" list is held to the stricter figure set: only figures of confirmed facts
    bad_conf = set()
    if "confirmed_unsupported_number" in qa.failures:
        for it in art["confirmed"]:
            bad_conf |= numbers_in(_numbers_text(it)) - conf
    ctx.conf_nums = bad_conf
    out["confirmed"] = fix_items(art["confirmed"], conf_list=True)
    ctx.conf_nums = cn
    out["unclear"] = fix_items(art["unclear"])
    out["disputed"] = fix_items(art["disputed"])
    tl = []
    for e in art["timeline"]:
        r = ctx.fix_sentence(e["text"])
        if r is not None:
            tl.append({**e, "text": r})
    out["timeline"] = tl
    if ctx.replaced == 0 and ctx.dropped == 0:
        return None
    return out, ctx.replaced, ctx.dropped
