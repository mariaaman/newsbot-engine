"""LLM layer: build_llm() wires providers from env keys; dry-run uses the mock provider."""
from __future__ import annotations

import os

from newsbot.llm.base import (LLM, BudgetExhausted, LLMError, ProviderError, RateLimited, SchemaError,
                              estimate_tokens, parse_json)
from newsbot.llm.budget import Budget, budget_from_settings
from newsbot.llm.health import HealthCheckError, check_models
from newsbot.llm.mock import MockProvider

__all__ = ["LLM", "Budget", "BudgetExhausted", "LLMError", "ProviderError", "RateLimited", "SchemaError",
           "HealthCheckError", "MockProvider", "build_llm", "build_embedder", "check_models",
           "budget_from_settings", "estimate_tokens", "parse_json", "load_llm_settings"]


def build_providers(env=os.environ) -> dict:
    from newsbot.llm.gemini import GeminiProvider
    from newsbot.llm.groq import GroqProvider
    from newsbot.llm.openrouter import OpenRouterProvider
    from newsbot.llm.workers_ai import WorkersAIProvider
    out: dict = {}
    if env.get("GROQ_API_KEY"):
        out["groq"] = GroqProvider(env["GROQ_API_KEY"])
    if env.get("CLOUDFLARE_API_TOKEN") and env.get("CLOUDFLARE_ACCOUNT_ID"):
        out["workers_ai"] = WorkersAIProvider(env["CLOUDFLARE_ACCOUNT_ID"], env["CLOUDFLARE_API_TOKEN"])
    if env.get("GEMINI_API_KEY"):
        out["gemini"] = GeminiProvider(env["GEMINI_API_KEY"])
    if env.get("OPENROUTER_API_KEY"):
        out["openrouter"] = OpenRouterProvider(env["OPENROUTER_API_KEY"])
    return out


def mock_settings(settings: dict) -> dict:
    """Same tasks, every tier -> the mock provider (dry-run / tests)."""
    tiers = {t: [{"provider": "mock", "model": f"mock-{t}"}] for t in set(settings.get("tasks", {}).values())}
    return {**settings, "tiers": tiers, "embedding": None}


def build_llm(settings: dict, state_dir, *, dry_run: bool = False, providers: dict | None = None,
              mock: MockProvider | None = None) -> LLM:
    if dry_run:
        s = mock_settings(settings)
        return LLM(s, {"mock": mock or MockProvider()}, Budget(None))
    provs = providers if providers is not None else build_providers()
    return LLM(settings, provs, budget_from_settings(settings, state_dir))


def build_embedder(settings: dict, providers: dict | None = None):
    """Workers AI embedder or None (lexical fallback) when no Cloudflare credentials."""
    from newsbot.llm.workers_ai import WorkersAIEmbedder
    provs = providers if providers is not None else build_providers()
    emb = settings.get("embedding")
    if emb and emb.get("provider") == "workers_ai" and "workers_ai" in provs:
        return WorkersAIEmbedder(provs["workers_ai"], emb["model"])
    return None


def load_llm_settings(brain_dir) -> dict:
    """Plain-dict llm settings (newsbot.yaml `llm:` deep-merged with llm.yaml). Stdlib+PyYAML only,
    so the LLM layer and its tests run without pydantic."""
    from pathlib import Path

    import yaml
    d = Path(brain_dir) / "config"
    def rd(n):
        p = d / n
        return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    def merge(a, b):
        o = dict(a)
        for k, v in b.items():
            o[k] = merge(o[k], v) if isinstance(v, dict) and isinstance(o.get(k), dict) else v
        return o
    extra = rd("llm.yaml")
    return merge(rd("newsbot.yaml").get("llm") or {}, extra.get("llm", extra))
