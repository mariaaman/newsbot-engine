"""Groq (OpenAI-compatible). Base URL and models endpoint: VERIFY against current Groq docs."""
from __future__ import annotations

from newsbot.llm.base import ProviderError, Response
from newsbot.llm.http import raise_for_status, requests_transport

BASE_URL = "https://api.groq.com/openai/v1"   # VERIFY


class GroqProvider:
    name = "groq"

    def __init__(self, api_key: str, transport=requests_transport, base_url: str = BASE_URL):
        self._key, self._t, self._base = api_key, transport, base_url.rstrip("/")

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    def chat(self, model, messages, *, json_mode=False, max_tokens=1024, task="") -> Response:
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        status, headers, data = self._t("POST", f"{self._base}/chat/completions", headers=self._h(), json=body)
        try:
            raise_for_status(status, headers)
        except ProviderError as e:
            err = data.get("error") if isinstance(data, dict) else None
            code = "".join(ch for ch in str((err or {}).get("code") or (err or {}).get("type") or "").lower()
                           if ch.isalnum() or ch in "_.-")[:40] if isinstance(err, dict) else ""
            raise ProviderError(f"{e} {code}".strip()) from None
        try:
            u = data.get("usage", {})
            return Response(data["choices"][0]["message"]["content"] or "", u.get("prompt_tokens", 0),
                            u.get("completion_tokens", 0), model, self.name)
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError("unexpected groq response shape") from None

    def list_models(self) -> list[str]:
        status, headers, data = self._t("GET", f"{self._base}/models", headers=self._h())
        raise_for_status(status, headers)
        return [m["id"] for m in (data or {}).get("data", [])]
