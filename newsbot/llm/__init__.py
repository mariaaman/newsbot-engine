"""LLM layer: build_llm() wires providers from env keys; dry-run uses the mock provider."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from newsbot.llm.base import (LLM, BudgetExhausted, LLMError, PoolPicker, ProviderError, RateLimited, SchemaError,
                              estimate_tokens, expand_pool, parse_json)
from newsbot.llm.budget import Budget, budget_from_settings
from newsbot.llm.health import HealthCheckError, check_models
from newsbot.llm.mock import MockProvider

__all__ = ["LLM", "Budget", "BudgetExhausted", "LLMError", "PoolPicker", "ProviderError", "RateLimited", "SchemaError",
           "HealthCheckError", "MockProvider", "build_llm", "build_embedder", "build_providers", "check_models",
           "budget_from_settings", "estimate_tokens", "expand_pool", "parse_json", "load_llm_settings", "load_cf_accounts"]


LABEL = re.compile(r"^[A-Z0-9_]{1,32}$")
ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
log = logging.getLogger("newsbot.llm")


def load_cf_accounts(env=os.environ) -> list[dict]:
    """Every Cloudflare account that may be used for LLM work: [{label, account_id, token}], any number.
    Source 1: NEWSBOT_CF_ACCOUNTS_FILE (a JSON file written by scripts/collect_llm_accounts.py from the secrets CFLLM_<LABEL>)
    or NEWSBOT_CF_ACCOUNTS (the same JSON text). Source 2, only when there are none: the single deploy account
    (CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN). The same Cloudflare account listed twice counts once.
    Bad entries are skipped; the log names a label and a reason, never a value."""
    raw = None
    path = env.get("NEWSBOT_CF_ACCOUNTS_FILE")
    if path:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("llm accounts file unreadable")
    elif env.get("NEWSBOT_CF_ACCOUNTS"):
        try:
            raw = json.loads(env["NEWSBOT_CF_ACCOUNTS"])
        except ValueError:
            log.warning("llm accounts value is not JSON")
    out: list[dict] = []
    seen_ids: dict[str, str] = {}
    for a in raw if isinstance(raw, list) else []:
        label = str(a.get("label", "")).upper() if isinstance(a, dict) else ""
        acct, tok = (a.get("account_id", ""), a.get("token", "")) if isinstance(a, dict) else ("", "")
        if not LABEL.match(label) or not ACCOUNT_ID.match(str(acct)) or not str(tok).strip():
            log.warning("llm account skipped label=%s reason=malformed", label or "?")
            continue
        if acct in seen_ids:
            log.warning("llm account skipped label=%s reason=same_cloudflare_account_as=%s", label, seen_ids[acct])
            continue
        if any(o["label"] == label for o in out):
            log.warning("llm account skipped label=%s reason=duplicate_label", label)
            continue
        seen_ids[acct] = label
        out.append({"label": label, "account_id": acct, "token": str(tok).strip()})
    if not out and env.get("CLOUDFLARE_API_TOKEN") and env.get("CLOUDFLARE_ACCOUNT_ID"):
        out.append({"label": "MAIN", "account_id": env["CLOUDFLARE_ACCOUNT_ID"], "token": env["CLOUDFLARE_API_TOKEN"]})
    return out


def provider_name(label: str) -> str:
    return "cf_" + label.lower()


def build_providers(env=os.environ) -> dict:
    """One provider per Cloudflare account, named cf_<label>; all of them belong to the pool "workers_ai"."""
    from newsbot.llm.workers_ai import WorkersAIProvider
    return {provider_name(a["label"]): WorkersAIProvider(a["account_id"], a["token"], name=provider_name(a["label"]))
            for a in load_cf_accounts(env)}


def mock_settings(settings: dict) -> dict:
    """Same tasks, every tier -> the mock provider (dry-run / tests)."""
    tiers = {t: [{"provider": "mock", "model": f"mock-{t}"}] for t in set(settings.get("tasks", {}).values())}
    return {**settings, "tiers": tiers, "embedding": None}


def build_llm(settings: dict, state_dir, *, dry_run: bool = False, providers: dict | None = None,
              mock: MockProvider | None = None, budget: Budget | None = None) -> LLM:
    if dry_run:
        s = mock_settings(settings)
        return LLM(s, {"mock": mock or MockProvider()}, Budget(None))
    provs = providers if providers is not None else build_providers()
    for p in provs.values():                      # per-model request extras (e.g. reasoning_effort) from llm.yaml
        if hasattr(p, "set_model_params"):
            p.set_model_params(settings.get("model_params"))
    return LLM(settings, provs, budget or budget_from_settings(settings, state_dir))


def build_embedder(settings: dict, providers: dict | None = None, budget: Budget | None = None):
    """Workers AI embedder (all accounts, used equally) or None (lexical fallback) when no Cloudflare credentials."""
    from newsbot.llm.workers_ai import WorkersAIEmbedder
    provs = providers if providers is not None else build_providers()
    emb = settings.get("embedding")
    members = [provs[n] for n in expand_pool(provs, "workers_ai")] if emb and emb.get("provider") == "workers_ai" else []
    return WorkersAIEmbedder(members, emb["model"], budget=budget) if members else None


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
