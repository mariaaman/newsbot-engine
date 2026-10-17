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
from newsbot.qa import QAResult, run_gates
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
            out.append({"story_id": fm.get("story_id"), "title": fm["title"], "slug": fm.get("slug", f.stem)})
    return out


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
