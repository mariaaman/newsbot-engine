"""Run diagnostics: what happened in this run, stage by stage, success AND failure.

The engine repository is public, so its Actions logs are public: they carry codes, counts and timings only, never names,
figures or claims from a draft. The full detail (which names/numbers a gate rejected, the unsupported claims, what each source
fetch did, every LLM call with its duration and why it failed, every repair attempt before/after) is written to the PRIVATE
brain repository instead:

    state/diagnostics/last_run.md   the latest run, human readable (overwritten every run)
    state/diagnostics/runs.jsonl    one summary line per run, the last 300 runs

Nothing here can break a run: every public function swallows its own errors."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("newsbot.diag")
MAX_RUNS = 300
MAX_DETAIL = 1500          # characters of any one detail blob in the report


def detail_counts(details: dict) -> str:
    """Public-log safe rendering of gate details: only codes and numbers (ints, list lengths), never text, and only plain
    characters so it stays one log token: `min_words.words.120|unsupported_number.n.2`."""
    out = []
    for code, d in (details or {}).items():
        bits: list[str] = []
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    bits += [str(k), str(v if isinstance(v, int) else round(v, 2))]
                elif isinstance(v, (list, tuple, set)):
                    bits += [str(k), str(len(v))]
        elif isinstance(d, (int, float)) and not isinstance(d, bool):
            bits = ["n", str(d)]
        elif isinstance(d, (list, tuple, set)):
            bits = ["n", str(len(d))]
        out.append(".".join([str(code), *bits]))
    return "|".join(out)[:150]


def _blob(x) -> str:
    try:
        s = json.dumps(x, ensure_ascii=False, default=str)
    except Exception:
        s = str(x)
    return s if len(s) <= MAX_DETAIL else s[:MAX_DETAIL] + " ...(cut)"


class Diag:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.active = False
        self.events: list[dict] = []
        self.marks: list[tuple[str, float]] = []
        self.t0 = time.monotonic()
        self.mode = ""
        self.started = ""
        self.story = ""                 # the story being processed; events default to it

    def start(self, mode: str, started_iso: str = "") -> None:
        self.reset()
        self.active, self.mode, self.started = True, mode, started_iso

    def mark(self, name: str) -> None:
        if self.active:
            self.marks.append((name, round(time.monotonic() - self.t0, 1)))

    def add(self, kind: str, **data) -> None:
        if not self.active:
            return
        try:
            story = data.pop("story", self.story)
            with self._lock:
                self.events.append({"t": round(time.monotonic() - self.t0, 1), "kind": kind, "story": story, **data})
        except Exception:                                      # diagnostics must never break a run
            pass

    # ---------------- summary / reports ----------------
    def llm_stats(self) -> dict:
        calls = [e for e in self.events if e["kind"] == "llm"]
        ok = [e for e in calls if e.get("ok")]
        return {"calls": len(calls), "ok": len(ok), "failed": len(calls) - len(ok),
                "tokens": sum(int(e.get("tokens") or 0) for e in ok), "secs": round(sum(float(e.get("secs") or 0) for e in calls))}

    def stage_times(self) -> str:
        prev, out = 0.0, []
        for name, t in self.marks:
            out.append(f"{name}:{t - prev:.0f}s")
            prev = t
        return ",".join(out)

    def public_summary(self) -> str:
        s = self.llm_stats()
        arts = [f"{e['lang']}:{e['status']}" for e in self.events if e["kind"] == "article"]
        return (f"run summary secs={time.monotonic() - self.t0:.0f} stages={self.stage_times() or '-'} llm_calls={s['calls']} "
                f"ok={s['ok']} failed={s['failed']} tokens={s['tokens']} llm_secs={s['secs']} articles={','.join(arts) or '-'}")

    def summary(self) -> dict:
        s = self.llm_stats()
        arts: dict = {}
        for e in self.events:
            if e["kind"] == "article":
                arts.setdefault(e["story"], {})[e["lang"]] = e["status"]
        facts = {e["story"]: e.get("status") for e in self.events if e["kind"] == "facts" and e.get("status")}
        return {"started": self.started, "mode": self.mode, "secs": round(time.monotonic() - self.t0),
                "stages": self.stage_times(), "llm": s, "facts": facts, "articles": arts}

    def render_md(self) -> str:
        s = self.summary()
        lines = [f"# Run diagnostics ({self.mode}) {self.started}", "",
                 f"- duration: {s['secs']}s   stages: {s['stages'] or '-'}",
                 f"- LLM calls: {s['llm']['calls']} (ok {s['llm']['ok']}, failed {s['llm']['failed']}), "
                 f"tokens {s['llm']['tokens']}, time in calls {s['llm']['secs']}s", ""]
        order: list[str] = []
        for e in self.events:
            if e["story"] not in order:
                order.append(e["story"])
        for story in order:
            lines += [f"## {story or '(run)'}", ""]
            for e in (x for x in self.events if x["story"] == story):
                lines.append("- " + self._line(e))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _line(e: dict) -> str:
        k = e["kind"]
        rest = {a: b for a, b in e.items() if a not in ("t", "kind", "story")}
        head = f"`{e['t']:>6}s` **{k}**"
        if k == "llm":
            mark = "ok" if rest.get("ok") else "FAILED"
            return (f"{head} {rest.get('task')} -> {rest.get('provider')} {rest.get('model')}: {mark} "
                    f"tokens={rest.get('tokens', 0)} secs={rest.get('secs', 0)} finish={rest.get('finish') or '-'}"
                    + (f" reason={rest['reason']}" if rest.get("reason") else ""))
        if k == "gate":
            verdict = "PASS" if not rest.get("failures") else "FAIL " + ", ".join(rest["failures"])
            extra = []
            if rest.get("review"):
                extra.append("review=" + ",".join(rest["review"]))
            for key in ("offending", "unsupported", "details"):
                if rest.get(key):
                    extra.append(f"{key}={_blob(rest[key])}")
            return f"{head} {rest.get('lang')} {rest.get('stage')}: {verdict}" + ("  " + "  ".join(extra) if extra else "")
        return f"{head} " + " ".join(f"{a}={_blob(b) if not isinstance(b, (str, int, float)) else b}" for a, b in rest.items())

    # ---------------- persistence (private brain) ----------------
    def write(self, brain) -> None:
        if not self.active or not brain:
            return
        try:
            from newsbot.state import _atomic_write
            d = Path(brain) / "state" / "diagnostics"
            _atomic_write(d / "last_run.md", self.render_md())
            runs = d / "runs.jsonl"
            old = runs.read_text(encoding="utf-8").splitlines() if runs.is_file() else []
            keep = (old + [json.dumps(self.summary(), ensure_ascii=False, sort_keys=True)])[-MAX_RUNS:]
            _atomic_write(runs, "\n".join(keep) + "\n")
        except Exception as e:                                  # never fail the run over a diagnostics file
            log.warning("diagnostics not written: %s", type(e).__name__)


DIAG = Diag()
