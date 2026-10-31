"""Cloudflare Workers AI. ONE WorkersAIProvider = ONE Cloudflare account (its own free daily allowance). Any number of
accounts can be registered; they form the pool "workers_ai" and the router (base.PoolPicker) uses them equally.

Chat goes through Cloudflare's OpenAI-compatible endpoint (/ai/v1/chat/completions) so every model answers in the same
shape. Embeddings use the native /ai/run/<model> endpoint. Endpoint paths and model names: VERIFY against Cloudflare docs;
`python -m newsbot llmcheck` proves each account really answers."""
from __future__ import annotations

import json
import time

from newsbot.llm.base import PoolPicker, ProviderError, RateLimited, Response, estimate_tokens, expand_pool
from newsbot.llm.http import raise_for_status, requests_transport

API = "https://api.cloudflare.com/client/v4"   # VERIFY


def chat_timeout(max_tokens: int) -> float:
    """Seconds to wait for ONE non-streaming answer. A fixed 60 s cut off long answers (gpt-oss-120b writes roughly
    80-100 tokens a second, so 5000+ tokens need more than a minute): the request failed with ReadTimeout on every account
    and the story with it. Grows with the answer size; capped so a hung connection cannot stall the run for long."""
    return float(min(240.0, 45.0 + max_tokens / 25.0))


def _text_of(data) -> str | None:
    """The answer text from an OpenAI-style reply, or (older / native shapes) result.response / a Responses-style output."""
    if not isinstance(data, dict):
        return None
    ch = data.get("choices")
    if isinstance(ch, list) and ch and isinstance(ch[0], dict):
        c = (ch[0].get("message") or {}).get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(p.get("text", "") for p in c if isinstance(p, dict))
        return None
    res = data.get("result", data)
    if not isinstance(res, dict):
        return None
    r = res.get("response")
    if isinstance(r, (dict, list)):                      # some models return parsed JSON in json mode
        return json.dumps(r, ensure_ascii=False)
    if isinstance(r, str):
        return r
    out = res.get("output")
    if isinstance(out, list):
        parts = [c.get("text", "") for item in out if isinstance(item, dict) and item.get("type") == "message"
                 for c in (item.get("content") or []) if isinstance(c, dict)]
        return "".join(parts) if parts else None
    return None


class WorkersAIProvider:
    pool = "workers_ai"

    def __init__(self, account_id: str, api_token: str, transport=requests_transport, name: str = "workers_ai",
                 model_params: dict | None = None):
        self._acct, self._tok, self._t = account_id, api_token, transport
        self.name = name
        self.account_id = account_id
        self._params = dict(model_params or {})      # model -> extra request fields, e.g. {"reasoning_effort": "low"}
        self._dropped: dict[str, set[str]] = {}      # model -> optional features this API refused (never sent again)

    def set_model_params(self, params: dict | None) -> None:
        self._params = dict(params or {})

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self._tok}", "Content-Type": "application/json"}

    def _run(self, model: str, body: dict) -> dict:
        status, headers, data = self._t("POST", f"{API}/accounts/{self._acct}/ai/run/{model}",
                                        headers=self._h(), json=body)
        raise_for_status(status, headers)
        if not isinstance(data, dict) or not data.get("success", True):
            raise ProviderError("workers ai call failed")
        return data.get("result", {})

    def chat(self, model, messages, *, json_mode=False, max_tokens=1024, task="") -> Response:
        dropped = self._dropped.setdefault(model, set())
        while True:
            body = {"model": model, "messages": messages, "max_tokens": max_tokens}
            extra = None if "params" in dropped else self._params.get(model)
            if extra:
                body.update(extra)
            if json_mode and "json" not in dropped:
                body["response_format"] = {"type": "json_object"}
            status, headers, data = self._t("POST", f"{API}/accounts/{self._acct}/ai/v1/chat/completions",
                                            headers=self._h(), json=body, timeout=chat_timeout(max_tokens))
            if status == 400:                         # a model that rejects an OPTIONAL field: stop sending it, ask again
                if extra:
                    dropped.add("params")
                    continue
                if "response_format" in body:
                    dropped.add("json")
                    continue
            raise_for_status(status, headers)
            break
        if isinstance(data, dict) and data.get("success") is False:
            raise ProviderError("workers ai call failed")
        text = _text_of(data)
        if text is None or not text.strip():
            raise ProviderError("empty reply")        # e.g. a reasoning model used every token on thinking
        u = (data.get("usage") or (data.get("result") or {}).get("usage") or {}) if isinstance(data, dict) else {}
        fin = ""
        if isinstance(data, dict) and isinstance(data.get("choices"), list) and data["choices"] and isinstance(data["choices"][0], dict):
            fin = str(data["choices"][0].get("finish_reason") or "")
        return Response(text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), model, self.name, fin)

    def list_models(self) -> list[str]:
        names: list[str] = []
        for page in range(1, 6):
            status, headers, data = self._t("GET", f"{API}/accounts/{self._acct}/ai/models/search?per_page=100&page={page}",
                                            headers=self._h())
            raise_for_status(status, headers)
            got = [m["name"] for m in (data or {}).get("result", []) if isinstance(m, dict) and "name" in m]
            names += got
            if len(got) < 100:
                break
        return names


class WorkersAIEmbedder:
    """embedder(texts) -> vectors; plug into cluster_items(embedder=...). bge-m3 handles Bengali.
    `provider` is one account or a list of accounts: chunks go to the least-used account, a failing one is skipped."""

    def __init__(self, provider, model: str, batch: int = 50, budget=None, picker: PoolPicker | None = None):
        self._provs = list(provider) if isinstance(provider, (list, tuple)) else [provider]
        self._model, self._batch, self._budget = model, batch, budget
        self._picker = picker or PoolPicker(budget)

    def _ordered(self):
        by_name = {p.name: p for p in self._provs}
        return [by_name[n] for n in self._picker.order(list(by_name))]

    def _embed(self, prov, chunk):
        for attempt in range(3):                       # a 429/5xx is retried; a spent daily allowance is not curable here
            try:
                return prov._run(self._model, {"text": chunk})
            except RateLimited as e:
                if attempt == 2:
                    raise
                time.sleep(min(e.retry_after or 4 * (attempt + 1), 20))
        raise ProviderError("unreachable")

    def __call__(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            chunk = texts[i:i + self._batch]
            last: Exception | None = None
            for prov in self._ordered():
                if self._budget and not self._budget.can_spend(prov.name, self._model, estimate_tokens([{"content": t} for t in chunk])):
                    continue
                try:
                    res = self._embed(prov, chunk)
                except (ProviderError, RateLimited) as e:
                    last = e
                    self._picker.cool(prov.name)
                    if self._budget:
                        self._budget.record_failure(prov.name, self._model, "embedding_failed")
                    continue
                vecs = res.get("data")
                if not isinstance(vecs, list) or len(vecs) != len(chunk):
                    last = ProviderError("unexpected embedding response shape")
                    continue
                if self._budget:
                    est = estimate_tokens([{"content": t} for t in chunk])
                    self._budget.record(prov.name, self._model, est, prompt=est, completion=0)
                out += vecs
                break
            else:
                raise last or ProviderError("no Workers AI account can embed right now")
        return out


__all__ = ["WorkersAIProvider", "WorkersAIEmbedder", "expand_pool"]
