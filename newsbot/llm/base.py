"""LLM router: complete(task, messages, schema) over provider chains from config/llm.yaml.
Stdlib only. Never logs prompts, drafts or secrets (public repo): log task, provider, token counts."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

log = logging.getLogger("newsbot.llm")


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


class LLM:
    """settings: dict from llm.yaml (tasks, tiers, retry). providers: name -> Provider (only those
    with keys should be registered). budget: Budget | None."""

    def __init__(self, settings: dict, providers: dict[str, Provider], budget=None,
                 sleep: Callable[[float], None] = time.sleep):
        self.tasks: dict[str, str] = settings.get("tasks", {})
        self.tiers: dict[str, list[dict]] = settings.get("tiers", {})
        r = settings.get("retry", {})
        self.max_retries = int(r.get("max_retries", 3))
        self.base_delay = float(r.get("base_delay_s", 2))
        self.max_delay = float(r.get("max_delay_s", 60))
        self.providers = providers
        self.budget = budget
        self._sleep = sleep

    def chain(self, task: str) -> list[tuple[str, str]]:
        if task not in self.tasks:
            raise LLMError(f"unknown task: {task}")
        tier = self.tasks[task]
        out = [(e["provider"], e["model"]) for e in self.tiers.get(tier, [])
               if e["provider"] in self.providers]
        if not out:
            raise LLMError(f"no available provider for task {task} (tier {tier}); check keys")
        return out

    def can_run(self, task: str, est_tokens: int) -> bool:
        """Pre-check used by the pipeline to queue stories instead of starting LLM work."""
        return any(self.budget is None or self.budget.can_spend(p, m, est_tokens)
                   for p, m in self.chain(task))

    def _call(self, prov: Provider, model: str, messages, json_mode, max_tokens, task) -> Response:
        delay = self.base_delay
        for attempt in range(self.max_retries + 1):
            try:
                return prov.chat(model, messages, json_mode=json_mode, max_tokens=max_tokens, task=task)
            except RateLimited as e:
                if attempt == self.max_retries:
                    raise ProviderError("rate limited after retries") from e
                wait = min(e.retry_after if e.retry_after is not None else delay, self.max_delay)
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
        for pname, model in self.chain(task):
            if self.budget and not self.budget.can_spend(pname, model, est):
                continue
            tried = True
            prov = self.providers[pname]
            msgs = list(messages)
            try:
                for repair in (False, True):
                    resp = self._call(prov, model, msgs, want_json, max_tokens, task)
                    if self.budget:
                        self.budget.record(pname, model, resp.total_tokens or est)
                    log.info("llm task=%s provider=%s model=%s tokens=%d", task, pname, model, resp.total_tokens)
                    if not want_json:
                        return resp.text
                    try:
                        return validate(parse_json(resp.text), schema)
                    except (ValueError, TypeError) as e:  # JSONDecodeError and pydantic ValidationError are ValueErrors
                        last = SchemaError(f"invalid output for task {task}: {type(e).__name__}")
                        if repair:
                            break
                        # one repair retry: show the model its own output plus the error class, not more
                        msgs = msgs + [{"role": "assistant", "content": resp.text},
                                       {"role": "user", "content": "Your reply was not valid for the required JSON "
                                        f"schema ({str(e)[:300]}). Reply again with ONLY the corrected JSON."}]
                        if self.budget and not self.budget.can_spend(pname, model, est):
                            raise BudgetExhausted(task)
                raise last  # type: ignore[misc]
            except ProviderError as e:
                last = e
                log.warning("provider failed task=%s provider=%s reason=%s, trying next", task, pname, safe_reason(e))
                continue
        if not tried:
            raise BudgetExhausted(f"daily LLM budget reached for task {task}")
        raise last or LLMError("all providers failed")
