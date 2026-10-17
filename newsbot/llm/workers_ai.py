"""Cloudflare Workers AI: fallback chat provider and the multilingual embedder used by cluster_items.
Endpoints and model names: VERIFY against Cloudflare docs. Uses the same Cloudflare token as deploy
(needs Workers AI permission)."""
from __future__ import annotations

from newsbot.llm.base import ProviderError, Response
from newsbot.llm.http import raise_for_status, requests_transport

API = "https://api.cloudflare.com/client/v4"   # VERIFY


class WorkersAIProvider:
    name = "workers_ai"

    def __init__(self, account_id: str, api_token: str, transport=requests_transport):
        self._acct, self._tok, self._t = account_id, api_token, transport

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
        body = {"messages": messages, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        res = self._run(model, body)
        text = res.get("response")
        if isinstance(text, (dict, list)):          # some models return parsed JSON in json mode
            import json
            text = json.dumps(text, ensure_ascii=False)
        if text is None:
            raise ProviderError("unexpected workers ai response shape")
        u = res.get("usage", {})
        return Response(text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), model, self.name)

    def list_models(self) -> list[str]:
        status, headers, data = self._t("GET", f"{API}/accounts/{self._acct}/ai/models/search?per_page=100",
                                        headers=self._h())
        raise_for_status(status, headers)
        return [m["name"] for m in (data or {}).get("result", [])]


class WorkersAIEmbedder:
    """embedder(texts) -> vectors; plug into cluster_items(embedder=...). bge-m3 handles Bengali."""

    def __init__(self, provider: WorkersAIProvider, model: str, batch: int = 50):
        self._p, self._model, self._batch = provider, model, batch

    def __call__(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            chunk = texts[i:i + self._batch]
            res = self._p._run(self._model, {"text": chunk})
            vecs = res.get("data")
            if not isinstance(vecs, list) or len(vecs) != len(chunk):
                raise ProviderError("unexpected embedding response shape")
            out += vecs
        return out
