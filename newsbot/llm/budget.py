"""Persistent daily token/request counters in state/usage.json (per provider and model).
Limits come from config (VERIFY, free tiers change). LLM work stops at stop_fraction of a limit."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsbot.state import _atomic_write


class Budget:
    def __init__(self, path: str | Path | None, token_limits: dict[str, int] | None = None,
                 request_limits: dict[str, int] | None = None, stop_fraction: float = 0.9,
                 now_fn=lambda: datetime.now(timezone.utc), reset_offset_hours: float = 0):
        """Limit keys: 'provider' (sum over its models) or 'provider/model' (that model only).
        A model-level key wins over the provider-level key. No limit configured = unlimited."""
        self.path = Path(path) if path else None
        self.token_limits = token_limits or {}
        self.request_limits = request_limits or {}
        self.stop_fraction = stop_fraction
        self._now = now_fn
        self._off = reset_offset_hours
        self.day = self._today()
        self.counters: dict[str, dict[str, int]] = {}
        self._load()

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

    def _roll(self) -> None:
        if self._today() != self.day:
            self.day, self.counters = self._today(), {}

    def save(self) -> None:
        if self.path:
            _atomic_write(self.path, json.dumps({"day": self.day, "counters": self.counters},
                                                sort_keys=True, indent=1) + "\n")

    def _used(self, limit_key: str, field: str) -> int:
        if "/" in limit_key:
            return self.counters.get(limit_key, {}).get(field, 0)
        return sum(c.get(field, 0) for k, c in self.counters.items() if k.split("/", 1)[0] == limit_key)

    def _limit(self, table: dict[str, int], provider: str, model: str) -> tuple[str, int] | None:
        for key in (f"{provider}/{model}", provider):
            if key in table:
                return key, table[key]
        return None

    def can_spend(self, provider: str, model: str, est_tokens: int) -> bool:
        self._roll()
        for table, field, add in ((self.token_limits, "tokens", est_tokens), (self.request_limits, "requests", 1)):
            hit = self._limit(table, provider, model)
            if hit and self._used(hit[0], field) + add > hit[1] * self.stop_fraction:
                return False
        return True

    def record(self, provider: str, model: str, tokens: int) -> None:
        self._roll()
        c = self.counters.setdefault(f"{provider}/{model}", {"tokens": 0, "requests": 0})
        c["tokens"] += int(tokens)
        c["requests"] += 1
        self.save()

    def used_fraction(self, provider: str, model: str) -> float | None:
        hit = self._limit(self.token_limits, provider, model)
        return None if not hit or not hit[1] else self._used(hit[0], "tokens") / hit[1]


def budget_from_settings(settings: dict, state_dir: str | Path | None, **kw) -> Budget:
    b = settings.get("budget", {})
    tok = dict(b.get("tokens_per_day", {}))
    req = dict(b.get("requests_per_day", {}))
    if "groq_tokens_per_day" in b:      # starter-config key (spec section 13)
        tok.setdefault("groq", b["groq_tokens_per_day"])
    if "groq_requests_per_day" in b:
        req.setdefault("groq", b["groq_requests_per_day"])
    path = Path(state_dir) / "usage.json" if state_dir else None
    return Budget(path, tok, req, b.get("stop_fraction", 0.9), **kw)
