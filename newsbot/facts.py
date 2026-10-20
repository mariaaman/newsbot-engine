"""fetch -> extract -> verify for one story. Failures affect that story only."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from newsbot.extract import extract_claims
from newsbot.fetch import Fetcher, fetch_sources
from newsbot.llm import BudgetExhausted, LLMError
from newsbot.models import Cluster, Fact, FactSheet
from newsbot.state import _atomic_write
from newsbot.verify import build_fact_sheet, org_key

log = logging.getLogger("newsbot.facts")
MIN_SOURCES = 2          # fewer fetched trusted organisations -> nothing can be confirmed; skip the story


def gather_facts(cluster: Cluster, cfg, brain, llm, fetcher: Fetcher, version: int = 1) -> tuple[FactSheet | None, str]:
    """Returns (fact_sheet, status) where status is ok | insufficient_sources | queued_budget | llm_failed."""
    sources = fetch_sources(cluster, cfg, fetcher)
    if len({org_key(s.domain) for s in sources}) < MIN_SOURCES:
        return None, "insufficient_sources"
    est = sum(len(s.text) for s in sources) // 2 + 1500 * len(sources)     # rough, conservative
    if not (llm.can_run("extract", est) and llm.can_run("verify", est)):
        return None, "queued_budget"
    try:
        per_source = getattr(getattr(cfg, "research", None), "claims_per_source", 25)
        claims = [c for s in sources for c in extract_claims(llm, brain, s, per_source)]
        if not claims:
            return None, "insufficient_sources"
        fs = build_fact_sheet(llm, brain, cluster.story_id, sources, claims, version)
        fs.source_texts = {s.id: s.text for s in sources}      # in memory only (QA overlap check); not saved
        return fs, "ok"
    except BudgetExhausted:
        return None, "queued_budget"
    except LLMError as e:
        log.warning("facts failed story=%s error=%s", cluster.story_id, type(e).__name__)
        return None, "llm_failed"


def save_fact_sheet(state_dir: str | Path, fs: FactSheet) -> None:
    _atomic_write(Path(state_dir) / "factsheets" / f"{fs.story_id}.json",
                  json.dumps(fs.to_dict(), ensure_ascii=False, sort_keys=True, indent=1) + "\n")


def load_fact_sheet(state_dir: str | Path, story_id: str) -> FactSheet | None:
    """The fact sheet saved by the last NEW/UPDATE of a story (source texts are not stored)."""
    path = Path(state_dir) / "factsheets" / f"{story_id}.json"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return FactSheet(d["story_id"], int(d["version"]), [Fact(**f) for f in d["facts"]], d["sources"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
