"""Writers (spec sections 5.8 and 9): one llm.complete('write_bn'|'write_en') per language, from the verified
fact sheet plus that language's own sources (for spelling and local wording only). Not a translation.
The model output is validated, its source metadata is overwritten from the fact sheet (code is the authority
for tier/url/date), and the Markdown is sanitised (no raw HTML, no link outside the source list)."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import yaml

from newsbot.llm import estimate_tokens
from newsbot.models import FactSheet, iso, utcnow
from newsbot.prompts import load_prompt, wrap_untrusted

LIST_FIELDS = ("confirmed", "unclear", "disputed")
MAX_TITLE, MAX_SUMMARY = 300, 1200
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _strs(v: Any, name: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{name} must be a list of strings")
    return [x.strip() for x in v if x.strip()]


def _corrections(v: Any) -> list[dict]:
    """[{said, correct, date?}] (update flow). Absent or empty is fine."""
    if not isinstance(v, list):
        raise ValueError("corrections must be a list")
    out = []
    for c in v:
        if not isinstance(c, dict) or not isinstance(c.get("said"), str) or not isinstance(c.get("correct"), str) \
                or not c["said"].strip() or not c["correct"].strip():
            raise ValueError("corrections need non-empty said and correct strings")
        e = {"said": c["said"].strip(), "correct": c["correct"].strip()}
        if isinstance(c.get("date"), str) and c["date"].strip():
            e["date"] = c["date"].strip()
        out.append(e)
    return out


def validate_article(data: Any) -> dict:
    """Schema for LLM.complete (callable, so no pydantic needed). Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("article must be a JSON object")
    out: dict = {}
    for k in ("title", "summary", "body_markdown", "category", "slug"):
        if not isinstance(data.get(k), str) or not data[k].strip():
            raise ValueError(f"{k} must be a non-empty string")
        out[k] = data[k].strip()
    tl = data.get("timeline")
    if not isinstance(tl, list):
        raise ValueError("timeline must be a list")
    out["timeline"] = []
    for e in tl:
        if isinstance(e, str):
            e = {"time": "", "text": e}
        if not isinstance(e, dict) or not isinstance(e.get("text"), str) or not isinstance(e.get("time", ""), str):
            raise ValueError("timeline entries need text (and optional time) strings")
        out["timeline"].append({"time": e.get("time", "").strip(), "text": e["text"].strip()})
    for k in LIST_FIELDS:
        out[k] = _strs(data.get(k), k)
    out["corrections"] = _corrections(data.get("corrections", []))
    src = data.get("sources")
    if not isinstance(src, list) or not all(isinstance(s, dict) and isinstance(s.get("id"), str) for s in src):
        raise ValueError("sources must be a list of objects with an id")
    out["sources"] = [{"id": s["id"], "name": str(s.get("name", "")), "tier": s.get("tier"),
                       "url": str(s.get("url", "")), "published_at": str(s.get("published_at", ""))} for s in src]
    return out


def fact_sheet_payload(fs: FactSheet) -> dict:
    """Trimmed, language-neutral view shown to writer and qa_check (no claim ids, no source text)."""
    return {"story_id": fs.story_id,
            "sources": [{k: s.get(k) for k in ("id", "name", "tier", "url", "lang", "published_at")}
                        for s in fs.sources],
            "facts": [{"id": f.id, "kind": f.kind, "statement": f.statement, "status": f.status,
                       "source_ids": f.source_ids, "note": f.note} for f in fs.facts]}


# ---------------- sanitising ----------------

_BLOCKS = re.compile(r"<\s*(script|style|iframe|object|embed)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
_TAG = re.compile(r"<\s*/?\s*[a-zA-Z!][^>]*>")
_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MDLINK = re.compile(r"\[([^\]]+)\]\(\s*([^)\s]+)[^)]*\)")
_URL = re.compile(r"(?:(?:https?|ftp)://|\bwww\.)[^\s)\]>\"']+", re.I)     # www./ftp are autolinked by some renderers
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _norm_url(u: str) -> str:
    return u.strip().rstrip("/.,;").lower()


def sanitize_markdown(text: str, allowed_urls: set[str]) -> tuple[str, int]:
    """Markdown only: drop HTML (blocks like <script> entirely, other tags keep their text), images, and
    every link, bare URL (http, https, ftp, www.) or e-mail address that is not in the source list. Returns (clean, number_of_removals)."""
    allowed = {_norm_url(u) for u in allowed_urls}
    n = 0

    def sub(rx, repl, s):
        nonlocal n
        s2, k = rx.subn(repl, s)
        n += k
        return s2

    text = sub(_BLOCKS, "", text)
    text = sub(_TAG, "", text)
    text = sub(_IMG, "", text)

    def keep_link(m):
        nonlocal n
        if _norm_url(m.group(2)) in allowed:
            return m.group(0)
        n += 1
        return m.group(1)

    text = _MDLINK.sub(keep_link, text)
    protected = [m.group(0) for m in _MDLINK.finditer(text)]       # links that survived are allowed ones

    def bare(m):
        nonlocal n
        if _norm_url(m.group(0)) in allowed or any(m.group(0) in p for p in protected):
            return m.group(0)
        n += 1
        return ""

    text = _URL.sub(bare, text)
    text = sub(_EMAIL, "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip(), n


def slugify(s: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80].strip("-")
    return s if SLUG_RE.match(s or "x") and s else re.sub(r"[^a-z0-9]+", "-", fallback.lower()).strip("-")


def normalise_article(art: dict, fs: FactSheet) -> tuple[dict, int]:
    """Source metadata comes from the fact sheet (writer cannot change tier/url/date); strings are sanitised."""
    by_id = {s["id"]: s for s in fs.sources}
    art = dict(art)
    art["sources"] = [({k: by_id[s["id"]][k] for k in ("id", "name", "tier", "url", "published_at")}
                       if s["id"] in by_id else s) for s in art["sources"]]
    allowed = {s["url"] for s in art["sources"]}
    removed = 0

    def clean(s: str) -> str:
        nonlocal removed
        out, k = sanitize_markdown(s, allowed)
        removed += k
        return out

    for k in ("title", "summary", "body_markdown"):
        art[k] = clean(art[k])
    art["title"] = re.sub(r"\s+", " ", art["title"])
    art["timeline"] = [{"time": clean(e["time"]), "text": clean(e["text"])} for e in art["timeline"]]
    for k in LIST_FIELDS:
        art[k] = [clean(x) for x in art[k]]
    art["corrections"] = [{**c, "said": clean(c["said"]), "correct": clean(c["correct"])}
                          for c in art.get("corrections", [])]
    art["slug"] = slugify(art["slug"], fs.story_id)
    art["category"] = art["category"].strip().lower()
    return art, removed


# ---------------- prompting ----------------

def build_messages(fs: FactSheet, lang: str, cfg, brain, previous: dict | None = None) -> list[dict]:
    system = load_prompt(brain, "update" if previous else f"write_{lang}")
    system += f"\n\nLanguage of the article: {lang}. Local angle: {cfg.write.local_angle.get(lang, '')}"
    parts = [wrap_untrusted("fact_sheet", json.dumps(fact_sheet_payload(fs), ensure_ascii=False))]
    own = [s for s in fs.sources if s.get("lang") == lang]
    limit = cfg.write.own_source_chars
    for s in own if limit else []:
        text = fs.source_texts.get(s["id"], "")[:limit]
        if text:
            parts.append(wrap_untrusted("source_data", text, id=s["id"], lang=lang, use="spelling_and_wording_only"))
    if previous is not None:
        parts.append(wrap_untrusted("draft_data", json.dumps(previous, ensure_ascii=False)))
    parts.append("Write the article now. Return ONLY the JSON object.")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]


def estimate_write_tokens(fs: FactSheet, lang: str, cfg, brain, previous: dict | None = None) -> int:
    return estimate_tokens(build_messages(fs, lang, cfg, brain, previous), cfg.write.max_tokens)


def write_article(llm, brain, cfg, fs: FactSheet, lang: str, previous: dict | None = None) -> tuple[dict, int]:
    """Returns (normalised article dict, number of sanitiser removals).
    Raises BudgetExhausted / LLMError; the caller fails this language only."""
    msgs = build_messages(fs, lang, cfg, brain, previous)
    raw = llm.complete(f"write_{lang}", msgs, schema=validate_article, max_tokens=cfg.write.max_tokens)
    return normalise_article(raw, fs)


# ---------------- Markdown file with front matter (consumed by the Hugo theme in M5) ----------------

def front_matter(art: dict, fs: FactSheet, lang: str, now: datetime | None = None, *,
                 published: str | None = None, extra: dict | None = None) -> dict:
    ts = iso(now or utcnow())
    fm = {"title": art["title"], "summary": art["summary"], "slug": art["slug"], "translationKey": fs.story_id,
          "story_id": fs.story_id, "lang": lang, "date": published or ts, "lastmod": ts,
          "categories": [art["category"]], "timeline": art["timeline"], "confirmed": art["confirmed"],
          "unclear": art["unclear"], "disputed": art["disputed"], "sources": art["sources"],
          "corrections": art.get("corrections", []), "ai_assisted": True, "fact_sheet_version": fs.version}
    fm.update(extra or {})
    return fm


def to_markdown_file(art: dict, fs: FactSheet, lang: str, now: datetime | None = None, **kw) -> str:
    fm = yaml.safe_dump(front_matter(art, fs, lang, now, **kw), allow_unicode=True, sort_keys=False,
                        width=1000, default_flow_style=False)
    return f"---\n{fm}---\n\n{art['body_markdown'].strip()}\n"
