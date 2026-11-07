"""What the engine has learned from its own failures, and the private log of what it could NOT fix. All of it is DATA in the brain
repo (state/learning/), never code, so it can be read, corrected or deleted by hand.

  rules.json    learned rules. A rule says "the word X is not an invented name IF these words are in the story's fact sheet"
                (e.g. {"term": "Gaza City", "needs": ["gaza"]}). The condition makes a rule safe by construction: it can never let a
                name through unless the fact sheet itself contains the words that justify it.
  review.jsonl one line per draft that still failed a gate after every automatic fix: gate, offending terms, short context, what
                autofix did. THIS is the file to hand to the developer after a few days: it shows which new kind of fix to build next.
  stats.json    per UTC day counters: gate failures by code, autofix results, diagnose calls, rules learned, rule uses, so the trend
                (fewer model calls, fewer failures) is visible.
The public engine log carries counts only; names and sentences live only in these private brain files. Nothing is written in dry-run
or when no brain is configured (tests)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("newsbot.learned")
DIR = ("state", "learning")
MAX_REVIEW_BYTES = 400_000
MAX_RULES = 500


class _State:
    brain: Path | None = None
    write = False
    rules: list[dict] = []
    stats_cache: dict | None = None


S = _State()


def configure(brain, write: bool) -> None:
    """Called once at the start of a run. write=False (dry-run, tests): rules are still read, nothing is written."""
    S.brain, S.write, S.stats_cache = (Path(brain) if brain else None), bool(write), None
    S.rules = _read_json(_path("rules.json"), {"rules": []}).get("rules", []) if S.brain else []
    S.rules = [r for r in S.rules if isinstance(r, dict) and r.get("term") and r.get("needs")]


def _path(name: str) -> Path | None:
    return S.brain.joinpath(*DIR, name) if S.brain else None


def _read_json(p: Path | None, default):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p and p.is_file() else default
    except (OSError, ValueError):
        return default


def _write(p: Path | None, text: str) -> None:
    if not (S.write and p):
        return
    from newsbot.state import _atomic_write
    try:
        _atomic_write(p, text)
    except OSError as e:
        log.warning("learning file not written error=%s", type(e).__name__)


def name_ok(term: str, vocab: set[str]) -> bool:
    """True when a learned rule says `term` is fine for a fact sheet with this vocabulary (all the rule's 'needs' words present)."""
    t = term.lower()
    for r in S.rules:
        if r["term"].lower() == t and all(n in vocab for n in r["needs"]):
            bump("rule_used")
            return True
    return False


def add_rule(term: str, needs: list[str], kind: str, lang: str, story: str) -> bool:
    needs = [n.lower() for n in needs if n]
    if not term or not needs or len(S.rules) >= MAX_RULES or any(r["term"].lower() == term.lower() and r["needs"] == needs for r in S.rules):
        return False
    S.rules.append({"term": term, "needs": needs, "kind": kind, "lang": lang, "story": story,
                    "learned": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
    _write(_path("rules.json"), json.dumps({"rules": S.rules}, ensure_ascii=False, indent=1) + "\n")
    bump("rule_learned")
    return True


def log_case(case: dict) -> None:
    """A draft that is still failing after every automatic fix (private brain file; see the module docstring)."""
    p = _path("review.jsonl")
    if not (S.write and p):
        return
    case = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **case}
    try:
        old = p.read_text(encoding="utf-8") if p.is_file() else ""
        if len(old) > MAX_REVIEW_BYTES:
            old = "\n".join(old.splitlines()[len(old.splitlines()) // 2:]) + "\n"          # keep the newer half
        from newsbot.state import _atomic_write
        _atomic_write(p, old + json.dumps(case, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("review log not written error=%s", type(e).__name__)


def bump(key: str, n: int = 1) -> None:
    p = _path("stats.json")
    if S.stats_cache is None:
        S.stats_cache = _read_json(p, {})
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = S.stats_cache.setdefault(day, {})
    d[key] = d.get(key, 0) + n
    if len(S.stats_cache) > 120:
        for k in sorted(S.stats_cache)[:-120]:
            S.stats_cache.pop(k)
    _write(p, json.dumps(S.stats_cache, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def report(brain) -> str:
    """Plain-text summary for the owner / developer: python -m newsbot learning-report --brain <dir>"""
    configure(brain, False)
    stats = _read_json(_path("stats.json"), {})
    lines = ["== per day =="]
    for day in sorted(stats)[-14:]:
        lines.append(f"{day}  " + "  ".join(f"{k}={v}" for k, v in sorted(stats[day].items())))
    lines.append(f"\n== learned rules ({len(S.rules)}) ==")
    lines += [f"{r['term']!r} ok when {r['needs']} in the fact sheet ({r.get('kind')}, {r.get('lang')}, {r.get('learned')})" for r in S.rules[-30:]]
    p = _path("review.jsonl")
    rows = []
    for ln in (p.read_text(encoding="utf-8").splitlines() if p and p.is_file() else []):
        try:
            rows.append(json.loads(ln))
        except ValueError:
            pass
    from collections import Counter
    lines.append(f"\n== drafts that still failed after every fix: {len(rows)} ==")
    for k, v in Counter(f for r in rows for f in r.get("failures", [])).most_common(12):
        lines.append(f"{v:4d}  {k}")
    lines.append("\nlatest cases:")
    for r in rows[-8:]:
        lines.append(f"- {r.get('ts')} {r.get('lang')} {','.join(r.get('failures', []))} names={r.get('names')} numbers={r.get('numbers')} verdicts={r.get('verdicts')}")
    return "\n".join(lines)
