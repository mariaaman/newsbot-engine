"""Stage 8-9 orchestration for one story: write each routed language, run QA, and put the result where it
belongs (content/<lang>/<slug>.md or pending/<lang>/<slug>.md). Failures affect that language only.
Committing / PRs / deploy are M6; story-state updates for new articles are M6/M7."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from newsbot.llm import BudgetExhausted, LLMError
from newsbot.models import FactSheet, utcnow
from newsbot.qa import QAResult, offending_terms, run_gates
from newsbot.state import _atomic_write
from newsbot.write import estimate_write_tokens, to_markdown_file, write_article

log = logging.getLogger("newsbot.articles")


@dataclass
class ArticleResult:
    lang: str
    status: str                       # publish | pending | queued_budget | write_failed
    article: dict | None = None
    qa: QAResult | None = None
    path: str | None = None


def load_recent_articles(brain: str | Path | None, lang: str, limit: int = 300) -> list[dict]:
    """Front-matter summaries (story_id, title, slug) of the newest published and pending articles."""
    if not brain:
        return []
    files: list[Path] = []
    for folder in ("content", "pending"):
        d = Path(brain) / folder / lang
        if d.is_dir():
            files += [f for f in d.glob("*.md")]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        head = f.read_text(encoding="utf-8").split("---", 2)
        if len(head) < 3:
            continue
        try:
            fm = yaml.safe_load(head[1]) or {}
        except yaml.YAMLError:
            continue
        if isinstance(fm.get("title"), str):
            out.append({"story_id": fm.get("story_id"), "title": fm["title"], "slug": fm.get("slug", f.stem),
                        "summary": fm["summary"] if isinstance(fm.get("summary"), str) else ""})
    return out


# Failures a rewrite can cure WITHOUT weakening any gate: the writer used a name or figure that is not in the fact sheet,
# is too short, quotes wrongly, cites badly, copies source wording, or the LLM fact check found unsupported claims.
# Not repairable (a rewrite would not make them true): schema, source_not_fetched, no_sources, duplicate_title,
# duplicate_embedding, no_change, correction_unsupported. Those stay in pending/ with their reasons.
REPAIRABLE = {
    "unsupported_name", "unsupported_number", "confirmed_unsupported_number", "unsupported_claims",
    "min_words", "title_length", "category", "slug", "duplicate_slug",
    "quote_not_in_sources", "quote_too_long", "quote_repeat_source",
    "unresolved_marker", "missing_marker", "no_sections", "ngram_overlap",
    "banned_phrase", "placeholder", "raw_html", "unlisted_link",
    "disputed_omitted", "confirmed_without_support", "unexpected_correction",
}


def _claim_text(c) -> str:
    return str(c.get("claim", "") if isinstance(c, dict) else c).strip()


def repair_note(failures: list[str], art: dict, fs, lang: str, cfg, qa) -> str:
    """Tell the writer exactly what the gates rejected. The gates themselves are never relaxed: the rewrite has to pass
    every one of them again, including the LLM fact check. Offending words are sent to the writer, never logged."""
    q, f = cfg.qa, set(failures)
    lines = ["QA REJECTED YOUR DRAFT. Rewrite the whole article with the same JSON shape and fix every point below. "
             "Before answering, check each name, number, age, date and year in every field (title, summary, body, "
             "timeline, lists) against the fact sheet: if the fact sheet does not state it, remove it. "
             "Use only what the fact sheet states. Do not add anything new."]
    if f & {"unsupported_name", "unsupported_number", "confirmed_unsupported_number"}:
        names, numbers = offending_terms(art, fs)
        lines.append(f"- Names not allowed: {', '.join(names) or '-'}. Figures, ages or years not allowed: "
                     f"{', '.join(numbers) or '-'}. Do not name, mention or hint at them anywhere.")
    if "unsupported_claims" in f:
        bad = [_claim_text(c) for c in qa.details.get("unsupported_claims", {}).get("unsupported", [])]
        lines.append("- A fact check found claims the fact sheet does not support. Remove or restate them using only "
                     "the fact sheet: " + " | ".join(x for x in bad if x)[:1500])
    if "min_words" in f:
        n = qa.details.get("min_words", {}).get("words", 0)
        lines.append(f"- Too short ({n} words). Write at least {q.min_words[lang]} words of body text by explaining the "
                     "stated facts, the timeline and what is still unclear. Never pad with invented detail.")
    if "title_length" in f:
        lo, hi = q.title_len[lang]
        lines.append(f"- The title must be {lo} to {hi} characters and specific (who did what), not a generic label.")
    if "category" in f:
        lines.append(f"- \"category\" must be exactly one of: {', '.join(cfg.site.categories)}.")
    if f & {"slug", "duplicate_slug"}:
        lines.append("- \"slug\" must be short, lowercase ASCII words joined by hyphens, and different from earlier articles.")
    if f & {"quote_not_in_sources", "quote_too_long", "quote_repeat_source"}:
        lines.append(f"- Remove every direct quotation unless it is copied word for word from one source, shorter than "
                     f"{q.max_quote_words} words, and used at most once per source. When unsure, paraphrase instead.")
    if f & {"unresolved_marker", "missing_marker", "no_sections"}:
        lines.append("- The body needs at least one \"## \" section, every paragraph of the first section must end with a "
                     "citation marker such as [S1], and markers may only use the source ids you were given.")
    if "ngram_overlap" in f:
        lines.append("- You copied wording from the sources. Rephrase everything completely in your own words.")
    if f & {"banned_phrase", "placeholder", "raw_html", "unlisted_link"}:
        lines.append("- Remove filler phrases, placeholders such as [insert ...], HTML tags and any link that is not one of "
                     "the listed sources.")
    if "disputed_omitted" in f:
        lines.append("- The fact sheet contains a disputed fact. Describe it in the \"disputed\" list.")
    if "confirmed_without_support" in f:
        lines.append("- Leave \"confirmed\" empty unless the fact sheet marks a fact as confirmed.")
    if "unexpected_correction" in f:
        lines.append("- \"corrections\" must be an empty list for a new article.")
    return "\n".join(lines)


def _repair(llm, brain, cfg, fs, lang, art, qa, recent, embedder, est, prev, now):
    """Up to qa.max_repair_attempts rewrites. Each rewrite is graded by ALL gates again (including the LLM fact check);
    a rewrite is kept when it has no more failures than the draft before it. Returns (article, qa, attempts)."""
    best_art, best_qa, tried = art, qa, 0
    while (tried < cfg.qa.max_repair_attempts and best_qa.failures and set(best_qa.failures) <= REPAIRABLE
           and (not hasattr(llm, "has_time") or llm.has_time(240))          # a rewrite takes minutes: never start one near the limit
           and llm.can_run(f"write_{lang}", est)):
        note = repair_note(best_qa.failures, best_art, fs, lang, cfg, best_qa)
        try:
            art2, _ = write_article(llm, brain, cfg, fs, lang, prev, fix=note, draft=best_art)
        except (BudgetExhausted, LLMError):
            break
        if prev is not None:
            from newsbot.update import finalize_update
            art2 = finalize_update(art2, prev, lang, now or utcnow())
        qa2 = run_gates(art2, fs, lang, cfg, recent=recent, embedder=embedder, llm=llm, brain=brain, previous=prev)
        tried += 1
        log.info("repair story=%s lang=%s try=%d before=%s after=%s", fs.story_id, lang, tried,
                 ",".join(best_qa.failures), ",".join(qa2.failures) or "-")
        # keep a rewrite only if it is strictly better, or equally good and still cureable: a rewrite must never swap a
        # fixable problem for one that cannot be fixed (for example a lost source list)
        if len(qa2.failures) < len(best_qa.failures) or (len(qa2.failures) == len(best_qa.failures)
                                                        and set(qa2.failures) <= REPAIRABLE):
            best_art, best_qa = art2, qa2
    return best_art, best_qa, tried


def produce_articles(fs: FactSheet, languages: list[str], cfg, brain, llm, *, embedder=None, recent_by_lang=None,
                     previous: dict | None = None, now=None) -> dict[str, ArticleResult]:
    """previous: lang -> currently published article (update flow); languages without an entry are written fresh."""
    results: dict[str, ArticleResult] = {}
    for lang in languages:
        est = estimate_write_tokens(fs, lang, cfg, brain, previous.get(lang) if previous else None)
        try:
            if not llm.can_run(f"write_{lang}", est):
                results[lang] = ArticleResult(lang, "queued_budget")
                continue
            art, removed = write_article(llm, brain, cfg, fs, lang, previous.get(lang) if previous else None)
        except BudgetExhausted:
            results[lang] = ArticleResult(lang, "queued_budget")
            continue
        except LLMError as e:
            log.warning("write failed story=%s lang=%s error=%s", fs.story_id, lang, type(e).__name__)
            results[lang] = ArticleResult(lang, "write_failed")
            continue
        prev = previous.get(lang) if previous else None
        if prev is not None:
            from newsbot.update import finalize_update
            art = finalize_update(art, prev, lang, now or utcnow())
        recent = (recent_by_lang or {}).get(lang)
        if recent is None:
            recent = load_recent_articles(brain, lang, cfg.qa.recent_articles_checked)
        qa = run_gates(art, fs, lang, cfg, recent=recent, embedder=embedder, llm=llm, brain=brain, previous=prev)
        if qa.failures and set(qa.failures) <= REPAIRABLE:
            art, qa, _ = _repair(llm, brain, cfg, fs, lang, art, qa, recent, embedder, est, prev, now)
        results[lang] = ArticleResult(lang, qa.disposition, art, qa)
        log.info("article story=%s lang=%s status=%s failures=%s review=%s sanitized=%d", fs.story_id, lang,
                 qa.disposition, ",".join(qa.failures) or "-", ",".join(qa.review) or "-", removed)
    return results


def write_outputs(brain: str | Path, fs: FactSheet, results: dict[str, ArticleResult], now=None,
                  published_dates: dict | None = None) -> list[str]:
    """publish -> content/<lang>/<slug>.md, pending -> pending/<lang>/<slug>.md (with the reasons).
    Returns the written paths relative to brain."""
    now = now or utcnow()
    written = []
    for lang, r in results.items():
        if r.article is None or r.qa is None:
            continue
        if r.status == "publish":
            rel, extra = Path("content") / lang / f"{r.article['slug']}.md", None
        else:
            rel = Path("pending") / lang / f"{r.article['slug']}.md"
            extra = {"pending": {"failures": r.qa.failures, "review": r.qa.review,
                                 "unsupported": r.qa.details.get("unsupported_claims", {}).get("unsupported", [])}}
        _atomic_write(Path(brain) / rel, to_markdown_file(r.article, fs, lang, now, extra=extra,
                                                       published=(published_dates or {}).get(lang) or None))
        r.path = str(rel)
        written.append(str(rel))
    return written
