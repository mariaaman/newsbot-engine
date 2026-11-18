"""Writer evaluation (M4): run the saved stories through every configured model of the write tasks and
report gate pass rate, Bengali fluency flags and tokens per article, so the owner can pick models.
Real runs spend provider tokens (not counted in state/usage.json); --dry-run is offline."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from newsbot.llm import LLM, LLMError
from newsbot.llm.base import Response
from newsbot.models import FactSheet, Fact
from newsbot.qa import bn_fluency_flags, fact_vocab, run_gates
from newsbot.write import write_article

STORIES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "eval" / "stories.json"


def load_stories(path: str | Path = STORIES) -> list[FactSheet]:
    out = []
    for s in json.loads(Path(path).read_text(encoding="utf-8")):
        facts = [Fact(f["id"], f["kind"], f["statement"], f["status"], [], f["source_ids"], f.get("note", ""))
                 for f in s["facts"]]
        pub = [{k: v for k, v in x.items() if k != "text"} for x in s["sources"]]
        fs = FactSheet(s["story_id"], 1, facts, pub)
        fs.source_texts = {x["id"]: x["text"] for x in s["sources"]}
        out.append(fs)
    return out


class CountingProvider:
    """Wraps a provider and counts tokens (the LLM router does not return usage to callers)."""

    def __init__(self, inner):
        self.inner, self.name, self.tokens = inner, inner.name, 0

    def chat(self, model, messages, **kw) -> Response:
        r = self.inner.chat(model, messages, **kw)
        self.tokens += r.total_tokens
        return r

    def list_models(self):
        return self.inner.list_models()


def targets_from_settings(settings: dict, providers: dict, tasks=("write_bn", "write_en")) -> list[tuple]:
    """(task, provider, model) for every configured model of each write task whose provider has a key."""
    out = []
    for task in tasks:
        for e in settings.get("tiers", {}).get(settings.get("tasks", {}).get(task, ""), []):
            if e["provider"] in providers:
                out.append((task, e["provider"], e["model"]))
    return out


@dataclass
class Row:
    task: str
    provider: str
    model: str
    stories: int = 0
    passed: int = 0
    review: int = 0
    write_failed: int = 0
    flagged: int = 0
    tokens: int = 0
    failure_counts: dict = field(default_factory=dict)

    def cells(self) -> list[str]:
        n = max(self.stories, 1)
        flags = f"{self.flagged}/{self.stories}" if self.task == "write_bn" else "-"
        top = ",".join(f"{k}x{v}" for k, v in sorted(self.failure_counts.items(), key=lambda kv: -kv[1])[:3]) or "-"
        return [self.task, f"{self.provider}/{self.model}", str(self.stories), f"{100 * self.passed // n}%",
                str(self.review), flags, str(self.tokens // n), top]


def run_eval(cfg, brain, stories: list[FactSheet], targets: list[tuple], providers: dict, retry: dict | None = None) -> list[Row]:
    rows = []
    for task, pname, model in targets:
        counter = CountingProvider(providers[pname])
        settings = {"tasks": {task: "eval"}, "tiers": {"eval": [{"provider": pname, "model": model}]},
                    "retry": retry or {"max_retries": 2, "base_delay_s": 2, "max_delay_s": 30}}
        llm = LLM(settings, {pname: counter}, None)
        lang, row = task.split("_")[1], Row(task, pname, model)
        for fs in stories:
            row.stories += 1
            before = counter.tokens
            try:
                art, _ = write_article(llm, brain, cfg, fs, lang)
            except LLMError:
                row.write_failed += 1
                row.failure_counts["write_failed"] = row.failure_counts.get("write_failed", 0) + 1
                continue
            finally:
                row.tokens += counter.tokens - before
            qa = run_gates(art, fs, lang, cfg, recent=[], run_llm_check=False)
            row.passed += qa.passed
            row.review += bool(qa.passed and qa.review)
            for code in qa.failures:
                row.failure_counts[code] = row.failure_counts.get(code, 0) + 1
            if lang == "bn" and bn_fluency_flags(art["body_markdown"], fact_vocab(fs)[1]):
                row.flagged += 1
        rows.append(row)
    return rows


HEADER = ["task", "model", "n", "gates", "review", "bn flags", "tok/article", "top failures"]


def format_table(rows: list[Row]) -> str:
    cells = [HEADER] + [r.cells() for r in rows]
    w = [max(len(c[i]) for c in cells) for i in range(len(HEADER))]
    line = lambda c: "  ".join(x.ljust(w[i]) for i, x in enumerate(c))      # noqa: E731
    return "\n".join([line(cells[0]), "  ".join("-" * x for x in w), *[line(c) for c in cells[1:]]])
