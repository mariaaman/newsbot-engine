#!/usr/bin/env python3
"""Sort the brain's pending/ drafts into what to do with each one. Local helper: run it on a checkout of the PRIVATE
brain repo and do not paste its output into a public issue (it prints draft file names).

    python scripts/pending_triage.py <brain_dir> [--list]

Why it exists: drafts that failed `min_words` under the OLD thresholds stay in pending/ forever, because nothing
re-checks them when you lower the limit. The word count here uses the same rule as the QA gate (headings and [S1]
markers do not count) against the brain's CURRENT qa.min_words.

Buckets
  read_and_approve  only human-review flags left (sensitive topic etc.): read it, then approve in Telegram / move it
  recheck_needed    its ONLY failure was min_words and it is long enough now: run the pipeline again or edit by hand
  rewrite_or_drop   a hard gate failed (name, number, quote, citation ...): the writer must rewrite it, or delete it
  duplicate_of      near-identical title to another draft or live article (same language): keep one
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

MARKER = re.compile(r"\[S\d+(?:\s*[,;]\s*S\d+)*\]|\(\s*S\d+(?:\s*[,;]\s*S\d+)*\s*\)")
DEFAULT_MIN = {"bn": 450, "en": 500}
STOP = set("the a an of in to and for on as after with by at is are was were from over into".split())


def split_front(text: str) -> tuple[dict, str]:
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        return (yaml.safe_load(parts[1]) or {}), parts[2]
    except yaml.YAMLError:
        return {}, parts[2]


def count_words(body: str) -> int:
    body = re.sub(r"(?m)^#.*$", "", MARKER.sub(" ", body))
    return len([w for w in body.split() if w.strip(".,;:!?\"'()[]“”‘’।")])


def tokens(title: str) -> frozenset:
    return frozenset(t for t in re.findall(r"\w+", str(title).lower()) if t not in STOP and len(t) > 1)


def jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def triage(brain: Path) -> dict:
    cfg = yaml.safe_load((brain / "config" / "newsbot.yaml").read_text(encoding="utf-8")) or {}
    min_words = {**DEFAULT_MIN, **((cfg.get("qa") or {}).get("min_words") or {})}
    out: dict[str, list] = defaultdict(list)
    seen: list[tuple[str, str, frozenset]] = []          # (lang, name, title tokens) of live articles first
    for lang_dir in sorted((brain / "content").glob("*")):
        for f in sorted(lang_dir.glob("*.md")):
            fm, _ = split_front(f.read_text(encoding="utf-8"))
            seen.append((lang_dir.name, f"content/{lang_dir.name}/{f.name}", tokens(fm.get("title", ""))))
    for lang_dir in sorted((brain / "pending").glob("*")):
        lang = lang_dir.name
        for f in sorted(lang_dir.glob("*.md")):
            fm, body = split_front(f.read_text(encoding="utf-8"))
            name = f"pending/{lang}/{f.name}"
            pend = fm.get("pending") or {}
            fails, review = list(pend.get("failures") or []), list(pend.get("review") or [])
            words, need = count_words(body), min_words.get(lang, 300)
            tk = tokens(fm.get("title", ""))
            dup = next((n for l, n, t in seen if l == lang and jaccard(tk, t) >= 0.5), None)
            seen.append((lang, name, tk))
            if dup:
                out["duplicate_of"].append((name, f"{dup}"))
            elif fails == ["min_words"] and words >= need:
                out["recheck_needed"].append((name, f"{words} words, needs {need}"))
            elif fails:
                hard = [x for x in fails if x != "min_words" or words < need]
                out["rewrite_or_drop"].append((name, ",".join(hard) or "min_words"))
            else:
                out["read_and_approve"].append((name, ",".join(review) or "no flags"))
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    brain, show = Path(argv[1]), "--list" in argv
    out = triage(brain)
    total = sum(len(v) for v in out.values())
    print(f"pending drafts: {total}")
    for k in ("read_and_approve", "recheck_needed", "rewrite_or_drop", "duplicate_of"):
        print(f"  {k:17} {len(out.get(k, []))}")
        if show:
            for name, why in out.get(k, []):
                print(f"      {name}  [{why}]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
