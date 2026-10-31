"""Run report: every pipeline step of one run as an event with exactly one outcome.

    ok     the step ran and did its job
    fail   the step ran and did not get what it needed (a feed down, a provider failed, a QA gate failed)
    error  an unexpected crash (type and file:line only)
    skip   nothing to do, or deliberately not run (no story to cover, cool-down, budget queue, robots.txt)

Events carry IDs, counts, domains and reason codes only (the same vocabulary the public logs already use).
Review items (title, summary, reasons, source names) are the one exception: they are written to a temporary file
for the `notify` step and never to logs or to the repos. stdlib only, so the notify step works even when the
engine's own dependencies failed to install."""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

STATUSES = ("ok", "fail", "error", "skip")
STEP_ORDER = ("discover", "cluster", "score", "gate", "fetch", "extract", "verify", "facts", "write", "qa",
              "update", "publish", "llm", "run")


class Report:
    def __init__(self, mode: str = "", started: str = ""):
        self.mode, self.started = mode, started
        self.events: list[dict] = []
        self.stories: dict[str, dict] = {}        # story_id -> {action, route, queued}
        self.reviews: list[dict] = []
        self.tokens: dict[str, int] = {}          # task -> tokens used this run

    def add(self, step: str, status: str, detail: str = "", story: str = "", lang: str = "") -> dict:
        if status not in STATUSES:
            raise ValueError(f"bad status {status}")
        ev = {"step": step, "status": status, "detail": detail, "story": story, "lang": lang}
        self.events.append(ev)
        return ev

    def counts(self) -> dict[str, int]:
        c = Counter(e["status"] for e in self.events)
        return {s: c.get(s, 0) for s in STATUSES}

    def covered_stories(self) -> int:
        return sum(1 for s in self.stories.values() if not s.get("queued"))

    def to_dict(self) -> dict:
        return {"mode": self.mode, "started": self.started, "events": self.events, "stories": self.stories,
                "reviews": self.reviews, "tokens": self.tokens}

    def save(self, path: str | Path | None) -> None:
        if path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path | None) -> "Report | None":
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        r = cls(str(d.get("mode", "")), str(d.get("started", "")))
        r.events = [e for e in d.get("events", []) if isinstance(e, dict) and e.get("status") in STATUSES]
        r.stories, r.reviews, r.tokens = d.get("stories", {}), d.get("reviews", []), d.get("tokens", {})
        return r


# ---------------- granular events from the (already safe) log lines ----------------

_KV = re.compile(r"(\w+)=(\S*)")
_FEED = re.compile(r"name=(.+?) error=(\w+)")


class _Collector(logging.Handler):
    """Turns the engine's own log lines (IDs, counts, domains, status words) into events. Fetch and extract lines come
    before the story they belong to is logged, so they wait in `pending` until a line with story=<id> arrives."""

    def __init__(self, report: Report):
        super().__init__(logging.INFO)
        self.report, self.pending = report, []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._handle(record.name, record.getMessage())
        except Exception:               # a reporting bug must never break a run
            pass

    def _ev(self, *a, **k):
        return self.report.add(*a, **k)

    def _handle(self, name: str, msg: str) -> None:
        f = dict(_KV.findall(msg))
        d = f.get("domain", "?")
        if name == "newsbot.discover" and msg.startswith("feed failed"):
            m = _FEED.search(msg)
            self._ev("discover", "fail", f"{m.group(1)}: {m.group(2)}" if m else "a feed failed")
        elif name == "newsbot.fetch":
            if msg.startswith("robots disallow"):
                self.pending.append(self._ev("fetch", "skip", f"{d}: robots.txt disallows"))
            elif msg.startswith("fetch failed"):
                self.pending.append(self._ev("fetch", "fail", f"{d}: {f.get('error', 'error')}"))
            elif msg.startswith("fetch skipped"):
                self.pending.append(self._ev("fetch", "skip", f"{d}: HTTP {f.get('status', '?')}"))
            elif msg.startswith("injection-like"):
                self.pending.append(self._ev("fetch", "ok", f"{d}: {f.get('count', '?')} injection-like sentence(s) removed"))
            elif msg.startswith("extract too short"):
                self.pending.append(self._ev("fetch", "skip", f"{d}: text too short"))
        elif name == "newsbot.extract" and msg.startswith("extract source="):
            self.pending.append(self._ev("extract", "ok", f"{f.get('source')}: {f.get('claims')} claims, "
                                                           f"{f.get('dropped')} dropped by the guards"))
        elif name == "newsbot.verify" and msg.startswith("verify story="):
            self._ev("verify", "ok", f"{f.get('facts')} facts, {f.get('confirmed')} confirmed, {f.get('disputed')} disputed",
                     f["story"])
        elif name == "newsbot.llm":
            if msg.startswith("429"):
                self._ev("llm", "skip", f"{f.get('provider')} rate limited, waited {f.get('wait')}")
            elif msg.startswith("provider failed"):
                self._ev("llm", "fail", f"{f.get('provider')} failed, next provider tried")
            elif msg.startswith("llm task="):
                try:
                    self.report.tokens[f["task"]] = self.report.tokens.get(f["task"], 0) + int(f.get("tokens", 0))
                except (KeyError, ValueError):
                    pass
        story = f.get("story")
        if story and self.pending:
            for ev in self.pending:
                ev["story"] = story
            self.pending = []


@contextmanager
def collect(report: Report):
    root = logging.getLogger("newsbot")
    h, old = _Collector(report), root.level
    root.addHandler(h)
    if root.getEffectiveLevel() > logging.INFO:
        root.setLevel(logging.INFO)
    try:
        yield h
    finally:
        root.removeHandler(h)
        root.setLevel(old)


# ---------------- events from the data the run returns ----------------

def _article_events(r: Report, sid: str, results: dict, kind: str, mode: str) -> None:
    for lang, res in results.items():
        if res.status == "queued_budget":
            r.add("write", "skip", "queued_budget (daily token budget)", sid, lang)
            continue
        if res.status == "write_failed":
            r.add("write", "fail", "write_failed", sid, lang)
            continue
        q = res.qa
        r.add("write", "ok", "", sid, lang)
        if q is not None and q.failures:
            r.add("qa", "fail", ", ".join(q.failures), sid, lang)
        else:
            r.add("qa", "ok", ("needs a human: " + ", ".join(q.review)) if q is not None and q.review
                  else "all gates passed", sid, lang)
        a = res.article
        if not a:
            continue
        status = "pending" if res.status == "pending" else {"review": "awaiting_review", "auto": "published"}.get(mode, "dry_run")
        r.reviews.append({"story_id": sid, "lang": lang, "kind": kind, "status": status, "title": a["title"],
                          "summary": a["summary"], "slug": a["slug"], "reasons": q.reasons() if q is not None else [],
                          "sources": [{"name": s.get("name", ""), "tier": s.get("tier", "")} for s in a["sources"]]})


def record_run(r: Report, *, mode: str, stats: dict, n_stories: int, decisions: list, articles: dict, updates: dict) -> None:
    """Adds the whole-run events (discover .. publish) from what run_pipeline already holds."""
    r.mode = mode
    feeds_ok, feeds_bad = stats.get("feeds_ok", 0), stats.get("feeds_failed", 0)
    r.add("discover", "fail" if feeds_ok == 0 and feeds_bad else "ok",
          f"{stats.get('items', 0)} items, {feeds_ok} feeds ok, {feeds_bad} failed")
    r.add("cluster", "ok", f"{n_stories} stories")
    r.add("score", "ok", f"{n_stories} stories scored")
    n = {a: sum(d.action == a for d in decisions) for a in ("NEW", "UPDATE", "SKIP")}
    queued = sum(bool(d.queued) for d in decisions)
    r.add("gate", "ok", f"NEW {n['NEW']} · UPDATE {n['UPDATE']} · SKIP {n['SKIP']}" + (f" · queued {queued}" if queued else ""))
    why = Counter(d.reason.split("|")[0] for d in decisions if d.action == "SKIP")
    if why:
        r.add("gate", "skip", ", ".join(f"{k}×{v}" for k, v in why.most_common(6)))
    todo = [d for d in decisions if d.action in ("NEW", "UPDATE")]
    for d in todo:
        r.stories[d.story_id] = {"action": d.action, "route": list(d.route), "queued": bool(d.queued)}
        if d.queued:
            r.add("gate", "skip", "queued for a later hour (daily cap or token budget)", d.story_id)
        else:
            r.add("gate", "ok", f"{d.action} → {'+'.join(d.route) or '-'} (score {d.score:.2f})", d.story_id)
        m = re.search(r"facts:(\w+)", d.reason)
        if d.action == "NEW" and m and m.group(1) != "ok":
            r.add("facts", "fail" if m.group(1) == "llm_failed" else "skip", m.group(1), d.story_id)
    for sid, results in articles.items():
        _article_events(r, sid, results, "new", mode)
    for sid, oc in updates.items():
        st = oc.status
        r.add("update", "ok" if st == "ok" else ("fail" if st == "llm_failed" else "skip"),
              st if st != "ok" else f"updated {','.join(oc.updated) or '-'} · added {','.join(oc.added) or '-'}", sid)
        _article_events(r, sid, oc.results, "update", mode)
    if not [d for d in todo if not d.queued]:
        for step in ("fetch", "extract", "verify", "write", "qa", "publish"):
            r.add(step, "skip", "no story to cover this hour")
    else:
        c = Counter(x["status"] for x in r.reviews)
        if r.reviews:
            r.add("publish", "ok", f"{c['awaiting_review']} awaiting review · {c['pending']} pending · {c['published']} published")
        else:
            r.add("publish", "skip", "nothing was written")
