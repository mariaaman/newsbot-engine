"""Gemini free tier (optional, only registered when GEMINI_API_KEY exists). VERIFY URL and models."""
from __future__ import annotations

from newsbot.llm.base import ProviderError, Response
from newsbot.llm.http import raise_for_status, requests_transport

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"   # VERIFY


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, transport=requests_transport, base_url: str = BASE_URL):
        self._key, self._t, self._base = api_key, transport, base_url.rstrip("/")

    def _h(self) -> dict:
        return {"x-goog-api-key": self._key, "Content-Type": "application/json"}   # header, never in the URL

    def chat(self, model, messages, *, json_mode=False, max_tokens=1024, task="") -> Response:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in messages if m["role"] != "system"]
        gen = {"maxOutputTokens": max_tokens, "temperature": 0.2}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        body = {"contents": contents, "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        status, headers, data = self._t("POST", f"{self._base}/models/{model}:generateContent",
                                        headers=self._h(), json=body)
        raise_for_status(status, headers)
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            u = data.get("usageMetadata", {})
            return Response(text, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0), model, self.name)
        except (KeyError, IndexError, TypeError):
            raise ProviderError("unexpected gemini response shape") from None

    def list_models(self) -> list[str]:
        status, headers, data = self._t("GET", f"{self._base}/models", headers=self._h())
        raise_for_status(status, headers)
        return [m["name"].removeprefix("models/") for m in (data or {}).get("models", [])]
