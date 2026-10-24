"""QA gates (spec section 9). Any hard failure parks the draft in pending/; sensitive topics and low
confidence also go to pending/ (human review). Nothing here logs article text: gate codes and counts only.

Conventions the writer prompts must follow (the gates depend on them):
- body_markdown has H2 sections; the FIRST H2 section is 'what happened' and every paragraph in it
  carries at least one [S#] marker. Markers anywhere must resolve to a listed source.
- The summary box, timeline, confirmed/unclear/disputed and sources are structured fields, not body text.
Limits: Bengali has no capitalisation, so proper-name checking is deterministic for Latin-script names only;
Bengali names are covered by the final qa_check LLM pass."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field

from newsbot.cluster import cosine, jaccard, tokenize
from newsbot.extract import norm_text, numbers_in
from newsbot.llm import BudgetExhausted, LLMError
from newsbot.models import FactSheet, parse_ts
from newsbot.prompts import load_prompt, wrap_untrusted
from newsbot.sensitive import detect
from newsbot.write import SLUG_RE, fact_sheet_payload, validate_article

BANNED = ["as an ai", "as a language model", "language model", "in conclusion", "i cannot", "i can't assist",
          "lorem ipsum", "placeholder", "tbd", "todo", "insert here", "উপসংহারে", "একটি ভাষা মডেল", "এআই হিসেবে"]
_PLACEHOLDER = re.compile(r"\{\{.*?\}\}|\[(?:insert|todo|tbd|placeholder|source|link)[^\]]*\]|\bxxx+\b", re.I)
_HTML = re.compile(r"<\s*/?\s*[a-zA-Z!][^>]*>")
_MARKER = re.compile(r"\[S(\d+)\]")
_QUOTE = re.compile(r"[“\"]([^”\"]{1,400})[”\"]")
_EDGE = "\"'“”‘’.,;:!?()[]{}«»।—–-*_#>`/\\|"
DATE_WORDS = set("""january february march april may june july august september october november december
monday tuesday wednesday thursday friday saturday sunday""".split())


@dataclass
class QAResult:
    failures: list = field(default_factory=list)   # hard gate codes
    review: list = field(default_factory=list)     # needs a human: sensitive:<cat>, low_confidence, qa_check_*
    details: dict = field(default_factory=dict)    # gate -> counts/short info (never article text, except 'unsupported')

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def disposition(self) -> str:
        return "publish" if not self.failures and not self.review else "pending"

    def reasons(self) -> list[str]:
        return [*self.failures, *self.review]


# ---------------- text helpers ----------------

def words(s: str) -> list[str]:
    """Lowercased, normalised word tokens (Bengali-safe: splits on whitespace, strips edge punctuation)."""
    return [w for w in (t.strip(_EDGE) for t in norm_text(s).split()) if w]


def raw_words(s: str) -> list[str]:
    s = unicodedata.normalize("NFKC", s)
    out = []
    for t in s.split():
        t = re.sub(r"['’]s$", "", t.strip(_EDGE))
        if t:
            out.append(t)
    return out


_PAREN_MARKER = re.compile(r"\(\s*S\d+(?:\s*[,;]\s*S\d+)*\s*\)")      # a writer's "(S1)" is a citation, not a figure


def strip_markers(s: str) -> str:
    return _PAREN_MARKER.sub(" ", _MARKER.sub(" ", s))


def article_text(a: dict) -> str:
    """Everything a reader sees except the title and source metadata (title is checked separately)."""
    parts = [a["summary"], a["body_markdown"], *[e["text"] for e in a["timeline"]],
             *a["confirmed"], *a["unclear"], *a["disputed"]]
    return "\n".join(parts)


def body_sections(body: str) -> list[str]:
    """Text of each H2 section (content before the first H2 is ignored)."""
    chunks = re.split(r"(?m)^##\s+.*$", body)
    return [c.strip() for c in chunks[1:]]


def paragraphs(section: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", section) if p.strip() and not p.strip().startswith("#")]


def fact_vocab(fs: FactSheet) -> tuple[set[str], set[str], set[str]]:
    """(allowed numbers, allowed latin words, numbers of confirmed facts only)."""
    nums, vocab, conf = set(), set(), set()
    for f in fs.facts:
        nums |= numbers_in(f.statement) | numbers_in(f.note)
        vocab |= {w.lower() for w in raw_words(f.statement + " " + f.note)}
        if f.status == "confirmed":
            conf |= numbers_in(f.statement)
    for s in fs.sources:
        vocab |= {w.lower() for w in raw_words(s["name"] + " " + s.get("domain", ""))}
        try:                                      # only year, month, day from the publish date (not clock digits)
            d = parse_ts(s["published_at"])
            nums |= {str(d.year), str(d.month), str(d.day)}
            conf |= {str(d.year), str(d.month), str(d.day)}
        except (ValueError, KeyError):
            pass
    return nums, vocab, conf


def _numbers_text(text: str) -> str:
    text = re.sub(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+", "", text)           # list markers are not figures
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    return strip_markers(text)


# Office and role words that are ordinary English, not names of people, places or organisations. A name still has to be
# in the fact sheet; these only describe a role ("the Senator", "Parliament") and carry no fact of their own.
ROLE_WORDS = set("""president prime minister senator parliament member members justice king queen section article citizen
citizens ministry government council committee court police army navy force forces party leader leaders chairman chair
spokesman spokesperson official officials mayor governor judge""".split())
_HYPHENS = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2212-]")


def unsupported_names(text: str, vocab: set[str]) -> list[str]:
    """Latin-script capitalised words (not just sentence starters) that the fact sheet never mentions.
    Not flagged: a word that also appears in lowercase in the same text (a title-case or sentence-case word such as
    "Exceeds" is an ordinary word, a name is always capitalised), office words, and the parts of a hyphenated
    compound are checked one by one (so "Turkish\u2010language" is judged by "Turkish")."""
    lowered: set[str] = set()
    for w in raw_words(strip_markers(text)):
        for part in _HYPHENS.split(w):
            if part and part[0].isalpha() and part[0].islower():
                lowered.add(part.lower())
    bad: list[str] = []
    for line in strip_markers(text).splitlines():
        if line.lstrip().startswith("#"):
            continue
        for sent in re.split(r"(?<=[.!?\u0964])\s+", line):
            toks = raw_words(sent)
            for i, tok in enumerate(toks):
                nxt = toks[i + 1] if i + 1 < len(toks) else ""
                if tok.lower() in vocab or _HYPHENS.sub("-", tok).lower() in vocab:
                    continue                                          # a compound the fact sheet itself uses
                for w in _HYPHENS.split(tok):
                    if len(w) < 2 or not w[0].isalpha() or not w[0].isupper() or any(c.isdigit() for c in w):
                        continue
                    if i == 0 and w is tok and not (nxt[:1].isalpha() and nxt[:1].isupper()):
                        continue                                      # ordinary sentence-initial word
                    lw = w.lower()
                    if lw in DATE_WORDS or lw in vocab or lw.rstrip("s") in vocab:
                        continue
                    if lw in ROLE_WORDS or lw in lowered or lw.rstrip("s") in lowered:
                        continue
                    bad.append(w)
    return sorted(set(bad))


def ngrams(ws: list[str], n: int) -> set[tuple]:
    return {tuple(ws[i:i + n]) for i in range(len(ws) - n + 1)}


def split_quotes(text: str) -> tuple[str, list[str]]:
    spans = [m.group(1) for m in _QUOTE.finditer(text) if len(m.group(1).split()) >= 3]
    return _QUOTE.sub(" ", text), spans


# ---------------- LLM pass ----------------

def validate_qa_check(data) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("unsupported"), list):
        raise ValueError("expected an object with an 'unsupported' list")
    out = []
    for x in data["unsupported"]:
        if isinstance(x, dict):
            x = x.get("claim", "")
        if not isinstance(x, str):
            raise ValueError("unsupported items must be strings or {claim, reason}")
        if x.strip():
            out.append(x.strip()[:300])
    return out


def llm_check(llm, brain, a: dict, fs: FactSheet) -> list[str]:
    body = f"{a['title']}\n\n{a['summary']}\n\n{a['body_markdown']}\n\nTIMELINE\n" + \
           "\n".join(f"{e['time']} {e['text']}" for e in a["timeline"]) + \
           "\n\nCONFIRMED\n" + "\n".join(a["confirmed"]) + "\n\nUNCLEAR\n" + "\n".join(a["unclear"]) + \
           "\n\nDISPUTED\n" + "\n".join(a["disputed"])
    msgs = [{"role": "system", "content": load_prompt(brain, "qa_check")},
            {"role": "user", "content": wrap_untrusted("fact_sheet", json.dumps(fact_sheet_payload(fs), ensure_ascii=False))
             + "\n\n" + wrap_untrusted("draft_data", body)}]
    return llm.complete("qa_check", msgs, schema=validate_qa_check, max_tokens=800)


# ---------------- the gates ----------------

def offending_terms(a: dict, fs: FactSheet) -> tuple[list[str], list[str]]:
    """(names, numbers) the article uses that the fact sheet does not support. Used only to tell the writer what to
    remove in its one repair attempt; the words are never logged (public logs carry counts only)."""
    nums, vocab, _ = fact_vocab(fs)
    all_text = a["title"] + "\n" + article_text(a)
    return unsupported_names(all_text, vocab), sorted(numbers_in(_numbers_text(all_text)) - nums)


def run_gates(a: dict, fs: FactSheet, lang: str, cfg, *, recent: list | None = None, embedder=None,
              llm=None, brain=None, run_llm_check: bool = True, previous: dict | None = None) -> QAResult:
    """previous = the currently published article when this is an update (M7 rules: corrections must be real,
    figures may not vanish silently, retained quotes were already vetted, an unchanged article is not an update)."""
    r = QAResult()
    try:
        a = validate_article(a)
    except ValueError:
        r.failures.append("schema")
        return r
    q = cfg.qa

    def fail(code, **detail):
        if code not in r.failures:
            r.failures.append(code)
        if detail:
            r.details[code] = detail

    # structure and size
    lo, hi = q.title_len[lang]
    if not lo <= len(a["title"]) <= hi:
        fail("title_length", length=len(a["title"]))
    n_words = len(words(re.sub(r"(?m)^#.*$", "", strip_markers(a["body_markdown"]))))
    if n_words < q.min_words[lang]:
        fail("min_words", words=n_words)
    if a["category"] not in cfg.site.categories:
        fail("category")
    if not SLUG_RE.match(a["slug"]) or len(a["slug"]) > 80:
        fail("slug")

    # citations and sources
    listed = {s["id"]: s for s in a["sources"]}
    fetched = {s["id"]: s for s in fs.sources}
    if not listed:
        fail("no_sources")
    bad_src = [i for i, s in listed.items() if i not in fetched or s["url"] != fetched[i]["url"]]
    if bad_src:
        fail("source_not_fetched", count=len(bad_src))
    markers = {f"S{m}" for m in _MARKER.findall(article_text(a))}
    if markers - set(listed):
        fail("unresolved_marker", count=len(markers - set(listed)))
    secs = body_sections(a["body_markdown"])
    if not secs:
        fail("no_sections")
    else:
        missing = [p for p in paragraphs(secs[0]) if not _MARKER.search(p)]
        if missing:
            fail("missing_marker", paragraphs=len(missing))

    # facts: numbers, names, statuses
    text = article_text(a)
    nums, vocab, conf_nums = fact_vocab(fs)
    all_text = a["title"] + "\n" + text
    extra = numbers_in(_numbers_text(all_text)) - nums
    if extra:
        fail("unsupported_number", count=len(extra))
    names = unsupported_names(all_text, vocab)
    if names:
        fail("unsupported_name", count=len(names))
    confirmed_facts = [f for f in fs.facts if f.status == "confirmed"]
    if a["confirmed"] and not confirmed_facts:
        fail("confirmed_without_support")
    elif numbers_in(_numbers_text("\n".join(a["confirmed"]))) - conf_nums:
        fail("confirmed_unsupported_number")
    if any(f.status == "disputed" for f in fs.facts) and not a["disputed"]:
        fail("disputed_omitted")

    # copyright: quotes and n-gram overlap against the fetched source texts
    stripped, quotes = split_quotes(text)
    texts = {sid: norm_text(t) for sid, t in fs.source_texts.items()}
    per_source: dict[str, int] = {}
    for qt in quotes:
        if len(qt.split()) > q.max_quote_words:
            fail("quote_too_long")
        hits = [sid for sid, t in texts.items() if norm_text(qt).strip("\"' ") in t]
        if texts and not hits and not (previous and norm_text(qt).strip("\"' ") in norm_text(article_text(previous))):
            fail("quote_not_in_sources")      # (a quote kept from the published article was vetted when it went live)
        for sid in hits[:1]:
            per_source[sid] = per_source.get(sid, 0) + 1
    if any(v > 1 for v in per_source.values()):
        fail("quote_repeat_source")
    mine = ngrams(words(strip_markers(stripped)), q.ngram_size)
    if mine:
        worst = max((len(mine & ngrams(words(t), q.ngram_size)) / len(mine) for t in fs.source_texts.values()),
                    default=0.0)
        r.details["ngram_overlap_max"] = round(worst, 3)
        if worst >= q.max_ngram_overlap:
            fail("ngram_overlap", ratio=round(worst, 3))

    # boilerplate, html, links
    low = all_text.lower()
    if any(b in low for b in BANNED + [p.lower() for p in q.banned_phrases]):
        fail("banned_phrase")
    if _PLACEHOLDER.search(all_text):
        fail("placeholder")
    if _HTML.search(all_text):
        fail("raw_html")
    allowed_urls = {s["url"].rstrip("/").lower() for s in a["sources"]}
    links = re.findall(r"https?://[^\s)\]>\"']+", all_text, re.I)
    if any(u.rstrip("/.,;").lower() not in allowed_urls for u in links):
        fail("unlisted_link")

    # duplicates against recent articles (same story_id = an update, not a duplicate)
    others = [x for x in (recent or []) if x.get("story_id") != fs.story_id]
    mt = frozenset(tokenize(a["title"]))
    if any(jaccard(mt, frozenset(tokenize(x["title"])), 2) >= q.duplicate_title_similarity for x in others):
        fail("duplicate_title")
    if any(x.get("slug") == a["slug"] for x in others):
        fail("duplicate_slug")
    if embedder is not None and others and "duplicate_title" not in r.failures:
        try:
            vecs = embedder([a["title"] + ". " + a["summary"]] + [x["title"] for x in others[:q.recent_articles_checked]])
            if any(cosine(vecs[0], v) >= q.duplicate_embedding_threshold for v in vecs[1:]):
                fail("duplicate_embedding")
        except Exception as e:                     # an embedding outage must not block QA; title check still ran
            r.details["embedding_check"] = f"skipped:{type(e).__name__}"

    # update rules (M7)
    new_corr = [c for c in a["corrections"] if previous is None or c not in previous.get("corrections", [])]
    if previous is None:
        if a["corrections"]:
            fail("unexpected_correction")
    else:
        prev_text = article_text(previous)
        if norm_text(a["body_markdown"]) == norm_text(previous["body_markdown"]) \
                and norm_text(a["summary"]) == norm_text(previous["summary"]):
            fail("no_change")
        prev_toks = set(tokenize(prev_text))
        for c in new_corr:
            said_toks = set(tokenize(c["said"]))
            said_nums = numbers_in(_numbers_text(c["said"]))
            if (said_nums and not said_nums <= numbers_in(_numbers_text(prev_text))) \
                    or (not said_nums and said_toks and len(said_toks & prev_toks) / len(said_toks) < 0.6) \
                    or numbers_in(_numbers_text(c["correct"])) - nums:
                fail("correction_unsupported")
        covered = numbers_in(_numbers_text(" ".join(c["said"] for c in a["corrections"] + [{"said": x} for x in a["disputed"]])))
        vanished = numbers_in(_numbers_text(prev_text)) - numbers_in(_numbers_text(text)) - covered
        if vanished:
            r.review.append("silent_change")     # a figure the old article had is gone without a correction or dispute

    # human review: sensitive topics, low confidence
    cats = detect(all_text + "\n" + " ".join(f.statement for f in fs.facts), cfg.sensitive.keywords or None)
    r.review += [f"sensitive:{c}" for c in cats]
    if len(confirmed_facts) < q.min_confirmed_facts:
        r.review.append("low_confidence")

    # final LLM pass (only when the cheap gates passed: saves tokens)
    if not r.failures and run_llm_check:
        if llm is None:
            r.review.append("qa_check_not_run")
        else:
            try:
                bad = llm_check(llm, brain, a, fs)
                if bad:
                    fail("unsupported_claims", count=len(bad), unsupported=bad)
            except BudgetExhausted:
                r.review.append("qa_check_unavailable")
            except LLMError:
                r.review.append("qa_check_failed")
    return r


# ---------------- tooling ----------------

def bn_fluency_flags(text: str, vocab: set[str] | None = None) -> list[str]:
    """Cheap heuristics for the Bengali evaluation (not a QA gate): untranslated Latin text, broken characters,
    repetition, run-on sentences, no sentence-final danda."""
    flags = []
    ws = [w for w in text.split() if w.strip(_EDGE)]
    latin = [w for w in ws if re.search(r"[A-Za-z]", w) and not (vocab and w.strip(_EDGE).lower() in vocab)]
    if ws and len(latin) / len(ws) > 0.12:
        flags.append("latin_text")
    if "\ufffd" in text:
        flags.append("broken_chars")
    sents = [s.strip() for s in re.split(r"[।!?\n]+", text) if len(s.split()) > 3]
    if len(sents) != len(set(sents)):
        flags.append("repeated_sentences")
    if sents and sum(len(s.split()) for s in sents) / len(sents) > 40:
        flags.append("long_sentences")
    if "।" not in text:
        flags.append("no_danda")
    return flags
