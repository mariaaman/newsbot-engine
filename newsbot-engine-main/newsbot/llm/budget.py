"""Persistent daily counters in state/usage.json (per provider and model), shared by every account of the pool.

Cloudflare Workers AI is billed in NEURONS, and every account has its own free daily allowance (resets 00:00 UTC).
So the budget tracks, per account (= provider name `cf_<label>`): requests, tokens, neurons, failures. LLM work for
an account stops at `stop_fraction` of its neuron limit and the next account takes over. Limits and rates come from
config (VERIFY: Cloudflare changes them). A limit that is not configured means unlimited."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsbot.state import _atomic_write

DEFAULT_RATE = {"in": 40909, "out": 290909}      # neurons per MILLION tokens; the dearest model on purpose (never under-count)


class Budget:
    def __init__(self, path: str | Path | None, token_limits: dict[str, int] | None = None,
                 request_limits: dict[str, int] | None = None, stop_fraction: float = 0.9,
                 now_fn=lambda: datetime.now(timezone.utc), reset_offset_hours: float = 0,
                 neuron_limit: int | None = None, neuron_limits: dict[str, int] | None = None,
                 rates: dict[str, dict[str, float]] | None = None):
        """Limit keys: 'provider' (sum over its models) or 'provider/model' (that model only).
        A model-level key wins over the provider-level key. No limit configured = unlimited.
        neuron_limit: daily neuron allowance of EVERY account; neuron_limits: per-account overrides.
        rates: {model: {in: neurons per M input tokens, out: neurons per M output tokens}, default: {...}}."""
        self.path = Path(path) if path else None
        self.token_limits = token_limits or {}
        self.request_limits = request_limits or {}
        self.neuron_limit = neuron_limit
        self.neuron_limits = neuron_limits or {}
        self.rates = rates or {}
        self.stop_fraction = stop_fraction
        self._now = now_fn
        self._off = reset_offset_hours
        self._lock = threading.RLock()
        self.day = self._today()
        self.counters: dict[str, dict[str, float]] = {}
        self.errors: dict[str, dict[str, str]] = {}
        self._load()

    # ---- persistence ----
    def _today(self) -> str:
        return (self._now().astimezone(timezone.utc) + timedelta(hours=self._off)).date().isoformat()

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                d = {}
            if d.get("day") == self.day:
                self.counters = d.get("counters", {})
                self.errors = d.get("errors", {})

    def _roll(self) -> None:
        if self._today() != self.day:
            self.day, self.counters, self.errors = self._today(), {}, {}

    def save(self) -> None:
        if self.path:
            doc = {"day": self.day, "counters": self.counters}
            if self.errors:
                doc["errors"] = self.errors
            _atomic_write(self.path, json.dumps(doc, sort_keys=True, indent=1) + "\n")

    # ---- reading ----
    def _used(self, limit_key: str, field: str) -> float:
        if "/" in limit_key:
            return self.counters.get(limit_key, {}).get(field, 0)
        return sum(c.get(field, 0) for k, c in self.counters.items() if k.split("/", 1)[0] == limit_key)

    def _limit(self, table: dict[str, int], provider: str, model: str) -> tuple[str, int] | None:
        for key in (f"{provider}/{model}", provider):
            if key in table:
                return key, table[key]
        return None

    def neuron_cap(self, provider: str) -> int | None:
        return self.neuron_limits.get(provider, self.neuron_limit)

    def rate(self, model: str) -> dict[str, float]:
        return self.rates.get(model) or self.rates.get("default") or DEFAULT_RATE

    def neurons_for(self, model: str, prompt: int, completion: int) -> float:
        r = self.rate(model)
        return (prompt * float(r.get("in", 0)) + completion * float(r.get("out", 0))) / 1_000_000

    def neurons_used(self, provider: str) -> float:
        with self._lock:
            self._roll()
            return self._used(provider, "neurons")

    def neuron_fraction(self, provider: str) -> float:
        """Share of today's neuron allowance this account has used (0 when it has no limit)."""
        cap = self.neuron_cap(provider)
        return 0.0 if not cap else self.neurons_used(provider) / cap

    def can_spend(self, provider: str, model: str, est_tokens: int, est_out: int = 0) -> bool:
        """est_tokens = prompt + reserved output; est_out = the reserved output part (priced dearer in neurons)."""
        with self._lock:
            self._roll()
            for table, field, add in ((self.token_limits, "tokens", est_tokens), (self.request_limits, "requests", 1)):
                hit = self._limit(table, provider, model)
                if hit and self._used(hit[0], field) + add > hit[1] * self.stop_fraction:
                    return False
            cap = self.neuron_cap(provider)
            if cap:
                est_n = self.neurons_for(model, max(est_tokens - est_out, 0), est_out)
                if self._used(provider, "neurons") + est_n > cap * self.stop_fraction:
                    return False
            return True

    # ---- writing ----
    def record(self, provider: str, model: str, tokens: int, *, prompt: int | None = None,
               completion: int | None = None) -> None:
        with self._lock:
            self._roll()
            c = self.counters.setdefault(f"{provider}/{model}", {"tokens": 0, "requests": 0})
            c["tokens"] += int(tokens)
            c["requests"] += 1
            if prompt is None and completion is None:        # only a total is known: price it at the mean of in/out
                r = self.rate(model)
                n = int(tokens) * (float(r.get("in", 0)) + float(r.get("out", 0))) / 2_000_000
            else:
                n = self.neurons_for(model, int(prompt or 0), int(completion or 0))
            c["neurons"] = round(c.get("neurons", 0) + n, 2)
            self.save()

    def record_failure(self, provider: str, model: str, reason: str) -> None:
        """A failed call is counted and its reason (a status word, never a message body) is kept for the dashboard."""
        with self._lock:
            self._roll()
            c = self.counters.setdefault(f"{provider}/{model}", {"tokens": 0, "requests": 0})
            c["failures"] = c.get("failures", 0) + 1
            self.errors[provider] = {"reason": str(reason)[:60], "at": self._now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            self.save()

    def used_fraction(self, provider: str, model: str) -> float | None:
        hit = self._limit(self.token_limits, provider, model)
        return None if not hit or not hit[1] else self._used(hit[0], "tokens") / hit[1]


def budget_from_settings(settings: dict, state_dir: str | Path | None, **kw) -> Budget:
    b = settings.get("budget", {})
    tok = dict(b.get("tokens_per_day", {}))
    req = dict(b.get("requests_per_day", {}))
    path = Path(state_dir) / "usage.json" if state_dir else None
    cap = b.get("neurons_per_day_per_account")
    return Budget(path, tok, req, b.get("stop_fraction", 0.9),
                  neuron_limit=int(cap) if cap else None, neuron_limits=dict(b.get("neurons_per_day", {})),
                  rates=settings.get("neuron_rates") or {}, **kw)
