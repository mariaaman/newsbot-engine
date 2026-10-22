"""OpenRouter (OpenAI-compatible; one key, many models, free ":free" variants). Same wire format as Groq.
Free models: ~20 requests/minute and ~50/day (more after a one-time credit purchase): VERIFY on openrouter.ai/docs.
Account setting: free models only answer if the privacy page allows free endpoints (prompts may be logged by the model
provider; this bot sends only public news text)."""
from __future__ import annotations

from newsbot.llm.groq import GroqProvider
from newsbot.llm.http import requests_transport

BASE_URL = "https://openrouter.ai/api/v1"   # VERIFY


class OpenRouterProvider(GroqProvider):
    name = "openrouter"
    # many free models do not support response_format: any 400 in JSON mode is retried once without it
    json_retry_codes = frozenset({"json_validate_failed", "400"})

    def __init__(self, api_key: str, transport=requests_transport, base_url: str = BASE_URL):
        super().__init__(api_key, transport, base_url)

    def _h(self) -> dict:
        return {**super()._h(), "X-Title": "newsbot"}
