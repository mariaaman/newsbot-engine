"""Verify (strong LLM groups claims) + deterministic status rules (code decides, not the model):
confirmed  = supported by >=2 independent tier 1-2 organisations and not disputed
disputed   = the model flagged a conflict OR supporting claims carry different figures
reported   = everything else (single source, or only tier-3 / same-organisation support)"""
from __future__ import annotations

import json
import logging
import re

from newsbot.extract import numbers_in
from newsbot.models import Claim, Fact, FactSheet, Source
from newsbot.prompts import load_prompt, wrap_untrusted

log = logging.getLogger("newsbot.verify")
MAX_FACTS = 60
MIN_LEFTOVER_WORDS = 5     # an ungrouped claim shorter than this is dropped, not passed on
# second-level labels under a country code (bbc.co.uk, nhk.or.jp, yna.co.kr, abc.net.au, kantei.go.jp ...)
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu", "or", "ne", "go", "gob", "gouv", "mil", "web", "sch", "gv", "nom"}
# one organisation with several domains: they must count as ONE independent source
_ORG_ALIASES = {"bbc.co.uk": "bbc.com", "bbci.co.uk": "bbc.com", "aljazeera.net": "aljazeera.com",
                "guardian.co.uk": "theguardian.com", "dw.de": "dw.com"}


def org_key(domain: str) -> str:
    """Registrable domain, so bbc.com/bengali and bbc.com/news count as ONE organisation."""
    parts = domain.lower().split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        key = ".".join(parts[-3:])
    else:
        key = ".".join(parts[-2:])
    return _ORG_ALIASES.get(key, key)


_WORD = re.compile(r"[^\W\d_]{3,}")


def _names(text: str) -> set[str]:
    """Capitalised words that are NOT at the start of a sentence (a rough proper-noun detector for cased scripts).
    Bengali, Japanese, Arabic etc. have no capitals, so they yield nothing and are never filtered by this guard."""
    out = set()
    for m in _WORD.finditer(text):
        w = m.group(0)
        before = text[:m.start()].rstrip(" \"'(\u201c")
        if not w[0].isupper() or not before or before[-1] in ".!?:;":
            continue
        out.add(w.lower())
    return out


def _off_topic(text: str, main_names: set[str], main_text: str) -> bool:
    """A leftover claim (one the model left out of every group) that names people/places which occur nowhere in the
    grouped claims is about ANOTHER event (a roundup line, a sidebar): the verify prompt told the model to leave it
    out, so it must not come back as a 'reported' fact. Names are compared with the grouped text, not only names."""
    names = _names(text)
    if not names:
        return False
    low = main_text.lower()
    return not any(n in main_names or n in low for n in names)


def validate_facts(data) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        raise ValueError("expected an object with a 'facts' list")
    out = []
    for f in data["facts"]:
        if not isinstance(f, dict) or not isinstance(f.get("statement"), str) \
                or not isinstance(f.get("claim_ids"), list):
            raise ValueError("fact needs statement (string) and claim_ids (list)")
        out.append({"statement": f["statement"].strip(), "claim_ids": [str(c) for c in f["claim_ids"]],
                    "kind": f.get("kind") if f.get("kind") in ("who", "what", "when", "where", "number", "quote") else "what",
                    "disputed": bool(f.get("disputed", False)), "note": str(f.get("note", ""))[:200]})
    return out


def compute_status(claims: list[Claim], sources: dict[str, Source], model_disputed: bool, min_orgs: int = 2) -> tuple[str, str]:
    figs = [(c, numbers_in(c.text)) for c in claims if c.kind == "number"]
    figs = [(c, n) for c, n in figs if n]
    orgs_by_fig = {}
    for c, n in figs:
        orgs_by_fig.setdefault(frozenset(n), set()).add(org_key(sources[c.source_id].domain))
    if model_disputed:
        return "disputed", ""
    if len(orgs_by_fig) > 1:
        return "disputed", "figures differ: " + " vs ".join(sorted("/".join(sorted(k)) for k in orgs_by_fig))
    trusted = {org_key(sources[c.source_id].domain) for c in claims if sources[c.source_id].tier <= 2}
    return ("confirmed" if len(trusted) >= min_orgs else "reported"), ""


def build_fact_sheet(llm, brain, story_id: str, sources: list[Source], claims: list[Claim],
                     version: int = 1, min_orgs: int = 2) -> FactSheet:
    by_src = {s.id: s for s in sources}
    by_claim = {c.id: c for c in claims}
    payload = {"sources": [{"id": s.id, "name": s.name, "tier": s.tier} for s in sources],
               "claims": [{"id": c.id, "source": c.source_id, "kind": c.kind, "text": c.text} for c in claims]}
    msgs = [{"role": "system", "content": load_prompt(brain, "verify")},
            {"role": "user", "content": wrap_untrusted("claims_data", json.dumps(payload, ensure_ascii=False))}]
    # the answer lists every fact with its claim ids: it grows with the claims (7 sources x 30 claims needs far more than 5000
    # tokens, and a reasoning model also spends some on thinking); a cut-off answer is invalid JSON and sank the whole story
    groups = llm.complete("verify", msgs, schema=validate_facts, max_tokens=min(14000, 4500 + 45 * len(claims)))
    facts: list[Fact] = []
    used: set[str] = set()
    for g in groups:
        ids = [i for i in dict.fromkeys(g["claim_ids"]) if i in by_claim and i not in used]   # unknown ids ignored
        if not ids or not g["statement"]:
            continue
        used.update(ids)
        group = [by_claim[i] for i in ids]
        status, auto_note = compute_status(group, by_src, g["disputed"], min_orgs)
        stmt = g["statement"]
        allowed = set().union(*(numbers_in(c.text) for c in group))
        if not numbers_in(stmt) <= allowed:           # model added a figure that no claim supports
            stmt = group[0].text
        facts.append(Fact("", g["kind"], stmt, status, ids, sorted({c.source_id for c in group}),
                          g["note"] or auto_note))
    dropped = 0
    main_text = " ".join([by_claim[i].text for i in used] + [f.statement for f in facts])
    main_names = _names(main_text)
    for c in claims:                                   # leftovers become single-source facts, but never bare fragments:
        if c.id not in used:                           # a lone name, place or time ("রাজশাহী") says nothing to a writer
            if len(c.text.split()) < MIN_LEFTOVER_WORDS or _off_topic(c.text, main_names, main_text):
                dropped += 1                           # (also: claims about another event, left out on purpose)
                continue
            facts.append(Fact("", c.kind, c.text, "reported", [c.id], [c.source_id], ""))
    facts = facts[:MAX_FACTS]
    order = {"confirmed": 0, "disputed": 1, "reported": 2}
    facts.sort(key=lambda f: order[f.status])
    for i, f in enumerate(facts, 1):
        f.id = f"F{i}"
    cited = {s for f in facts for s in f.source_ids}
    log.info("verify story=%s facts=%d confirmed=%d disputed=%d fragments_dropped=%d", story_id, len(facts),
             sum(f.status == "confirmed" for f in facts), sum(f.status == "disputed" for f in facts), dropped)
    return FactSheet(story_id, version, facts, [s.public_dict() for s in sources if s.id in cited])
