"""LLM router: complete(task, messages, schema) over provider chains from config/llm.yaml.
Stdlib only. Never logs prompts, drafts or secrets (public repo): log task, provider, token counts."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

log = logging.getLogger("newsbot.llm")
MAX_RETRY_TOKENS = 14000      # the most a cut-off answer is given on its second try


class LLMError(Exception):
    pass


class RateLimited(LLMError):
    def __init__(self, retry_after: float | None = None):
        super().__init__("rate limited")
        self.retry_after = retry_after


def safe_reason(e: Exception) -> str:
    """A log-safe word for why a provider call failed: status code (+ the API's error code), never a message body."""
    m = re.match(r"http (\d{3})(?: ([a-z0-9_.-]{1,40}))?$", str(e))
    if m:
        return f"status_{m.group(1)}" + (f"_{m.group(2)}" if m.group(2) else "")
    if isinstance(e, ProviderError):
        return re.sub(r"[^a-z0-9]+", "_", str(e).lower())[:40]
    return type(e).__name__


class ProviderError(LLMError):
    """Non-retryable provider failure (auth, bad model, 5xx after retries)."""


class BudgetExhausted(LLMError):
    """No provider in the chain has budget left; the caller should queue the story."""


class SchemaError(LLMError):
    """Model output failed JSON/schema validation after one repair retry."""


@dataclass
class Response:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    provider: str = ""
    finish_reason: str = ""      # "length" = the answer was cut off at max_tokens (reasoning models spend part of it thinking)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Provider(Protocol):
    name: str

    def chat(self, model: str, messages: list[dict], *, json_mode: bool = False,
             max_tokens: int = 1024, task: str = "") -> Response: ...

    def list_models(self) -> list[str]: ...


def estimate_tokens(messages: list[dict], max_tokens: int = 0) -> int:
    """Conservative pre-call estimate. Bengali tokenises ~3x worse than English."""
    chars = sum(len(m.get("content", "")) for m in messages)
    non_ascii = sum(1 for m in messages for c in m.get("content", "") if ord(c) > 127)
    per_token = 1.5 if chars and non_ascii / chars > 0.3 else 3.5
    return int(chars / per_token) + 8 * len(messages) + max_tokens


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def parse_json(text: str) -> Any:
    """Strict-ish JSON extraction: strips code fences and surrounding prose, then json.loads."""
    t = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON found")
    s = min(starts)
    end = max(t.rfind("}"), t.rfind("]"))
    if end <= s:
        raise ValueError("unterminated JSON")
    return json.loads(t[s:end + 1])


def validate(data: Any, schema: Any) -> Any:
    """schema: None | pydantic model class | callable(data)->data raising ValueError."""
    if schema is None:
        return data
    if hasattr(schema, "model_validate"):
        return schema.model_validate(data)
    return schema(data)


def expand_pool(providers: dict, pname: str) -> list[str]:
    """A chain entry names either one provider or a POOL (e.g. "workers_ai" = every Cloudflare account that was
    registered). Returns the registered provider names behind it, [] when none."""
    if pname in providers:
        return [pname]
    return sorted(n for n, p in providers.items() if getattr(p, "pool", None) == pname)


class PoolPicker:
    """Decides which account of a pool is used next, so all accounts are used EQUALLY.
    Least-used first (share of today's neuron allowance, rounded to 1%); accounts at the same level take turns
    (round robin); an account that just failed goes to the back for the rest of the run. Thread-safe."""

    def __init__(self, budget=None):
        self.budget = budget
        self.cooling: set[str] = set()
        self._rr = 0
        self._lock = threading.Lock()

    def order(self, names: list[str]) -> list[str]:
        """Accounts that are on the same level (same share of allowance used, none failing) take turns: the group is
        rotated by one more place on every call, so the first place is shared out evenly."""
        with self._lock:
            self._rr += 1
            frac = getattr(self.budget, "neuron_fraction", None) if self.budget else None
            keyed = sorted(((name in self.cooling, round(frac(name), 2) if frac else 0.0, i, name) for i, name in enumerate(names)))
            out: list[str] = []
            i = 0
            while i < len(keyed):
                j = i
                while j < len(keyed) and keyed[j][:2] == keyed[i][:2]:
                    j += 1
                group = [k[3] for k in keyed[i:j]]
                r = self._rr % len(group)
                out += group[r:] + group[:r]
                i = j
            return out

    def cool(self, name: str) -> None:
        with self._lock:
            self.cooling.add(name)

    def warm(self, name: str) -> None:
        with self._lock:
            self.cooling.discard(name)


class LLM:
    """settings: dict from llm.yaml (tasks, tiers, retry). providers: name -> Provider (only those
    with keys should be registered). budget: Budget | None."""

    def __init__(self, settings: dict, providers: dict[str, Provider], budget=None,
                 sleep: Callable[[float], None] = time.sleep, picker: PoolPicker | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.tasks: dict[str, str] = settings.get("tasks", {})
        self.tiers: dict[str, list[dict]] = settings.get("tiers", {})
        r = settings.get("retry", {})
        self.max_retries = int(r.get("max_retries", 3))
        self.base_delay = float(r.get("base_delay_s", 2))
        self.max_delay = float(r.get("max_delay_s", 60))
        self.providers = providers
        self.budget = budget
        self._sleep = sleep
        self.picker = picker or PoolPicker(budget)
        self._clock = clock
        self.deadline: float | None = None

    # ---- run time limit: the hourly job is killed at 30 minutes and then NOTHING is committed, so the run must end by itself ----
    def set_deadline(self, at: float | None) -> None:
        """`at` is a monotonic-clock time. Past it complete() raises BudgetExhausted: the story is queued for the next run and
        the run still commits the work it has."""
        self.deadline = at

    def time_left(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - self._clock()

    def has_time(self, seconds: float) -> bool:
        return self.time_left() >= seconds

    def chain(self, task: str) -> list[tuple[str, str]]:
        if task not in self.tasks:
            raise LLMError(f"unknown task: {task}")
        tier = self.tasks[task]
        out: list[tuple[str, str]] = []
        for e in self.tiers.get(tier, []):
            names = expand_pool(self.providers, e["provider"])
            if len(names) > 1:
                names = self.picker.order(names)          # equal use: the least-used account first, ties take turns
            out.extend((n, e["model"]) for n in names)
        if not out:
            raise LLMError(f"no available provider for task {task} (tier {tier}); check keys")
        return out

    def can_run(self, task: str, est_tokens: int) -> bool:
        """Pre-check used by the pipeline to queue stories instead of starting LLM work."""
        return any(self.budget is None or self.budget.can_spend(p, m, est_tokens)
                   for p, m in self.chain(task))

    def _call(self, prov: Provider, model: str, messages, json_mode, max_tokens, task,
              retries: int | None = None) -> Response:
        retries = self.max_retries if retries is None else retries
        delay = self.base_delay
        for attempt in range(retries + 1):
            try:
                return prov.chat(model, messages, json_mode=json_mode, max_tokens=max_tokens, task=task)
            except RateLimited as e:
                if attempt == retries:
                    raise ProviderError("rate limited after retries") from e
                wait = min(e.retry_after if e.retry_after is not None else delay, self.max_delay)
                if self.time_left() < wait + 45:                  # waiting would eat the time the run needs to commit its work
                    raise BudgetExhausted("run time limit reached while rate limited") from e
                log.warning("429 task=%s provider=%s attempt=%d wait=%.1fs", task, prov.name, attempt + 1, wait)
                self._sleep(wait)
                delay = min(delay * 2, self.max_delay)
        raise ProviderError("unreachable")

    def complete(self, task: str, messages: list[dict], schema: Any = None, *, max_tokens: int = 1024) -> Any:
        """Returns validated data when `schema` is given or JSON expected (schema is not None), else text.
        Raises BudgetExhausted (queue the story) or SchemaError/ProviderError (fail this story only)."""
        want_json = schema is not None
        est = estimate_tokens(messages, max_tokens)
        last: Exception | None = None
        tried = False
        if self.time_left() < 45:
            raise BudgetExhausted(f"run time limit reached before task {task}")
        chain = self.chain(task)
        bad_models: set[str] = set()        # a model whose answer failed the schema twice is not retried on other accounts
        for pos, (pname, model) in enumerate(chain):
            if model in bad_models:
                continue
            if self.budget and not self.budget.can_spend(pname, model, est, est_out=max_tokens):
                continue
            tried = True
            prov = self.providers[pname]
            msgs = list(messages)
            tokens_out = max_tokens                 # may grow once if the answer was cut off (see below)
            # another entry is waiting behind this one: give up on a 429 quickly and let it take over
            retries = self.max_retries if pos == len(chain) - 1 else min(1, self.max_retries)
            try:
                for repair in (False, True):
                    resp = self._call(prov, model, msgs, want_json, tokens_out, task, retries)
                    self.picker.warm(pname)
                    if self.budget:
                        self.budget.record(pname, model, resp.total_tokens or est,
                                           prompt=resp.prompt_tokens, completion=resp.completion_tokens)
                    log.info("llm task=%s provider=%s model=%s tokens=%d", task, pname, model, resp.total_tokens)
                    if not want_json:
                        return resp.text
                    try:
                        return validate(parse_json(resp.text), schema)
                    except (ValueError, TypeError) as e:  # JSONDecodeError and pydantic ValidationError are ValueErrors
                        last = SchemaError(f"invalid output for task {task}: {type(e).__name__}")
                        if repair:
                            break
                        if resp.finish_reason == "length":
                            # Cut off at max_tokens (a reasoning model spends part of it thinking). Sending the broken answer
                            # back would be cut off again, so ask the original question once more with more room.
                            bigger = min(int(tokens_out * 1.8), MAX_RETRY_TOKENS)
                            if bigger <= tokens_out:
                                break
                            log.warning("answer cut off task=%s provider=%s model=%s: asking again with more room", task, pname, model)
                            tokens_out = bigger
                            if self.budget and not self.budget.can_spend(pname, model, est, est_out=tokens_out):
                                raise BudgetExhausted(task)
                            continue
                        # one repair retry: show the model its own output plus the error class, not more
                        msgs = msgs + [{"role": "assistant", "content": resp.text},
                                       {"role": "user", "content": "Your reply was not valid for the required JSON "
                                        f"schema ({str(e)[:300]}). Reply again with ONLY the corrected JSON."}]
                        if self.budget and not self.budget.can_spend(pname, model, est, est_out=max_tokens):
                            raise BudgetExhausted(task)
                raise last  # type: ignore[misc]
            except SchemaError as e:             # the next DIFFERENT (stronger) model in the chain gets a try
                last = e
                bad_models.add(model)
                log.warning("schema failed task=%s provider=%s model=%s, trying a different model", task, pname, model)
                continue
            except ProviderError as e:
                last = e
                self.picker.cool(pname)
                if self.budget and hasattr(self.budget, "record_failure"):
                    self.budget.record_failure(pname, model, safe_reason(e))
                log.warning("provider failed task=%s provider=%s reason=%s, trying next", task, pname, safe_reason(e))
                continue
        if not tried:
            raise BudgetExhausted(f"daily LLM budget reached for task {task}")
        raise last or LLMError("all providers failed")
