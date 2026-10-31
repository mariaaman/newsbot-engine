"""Startup health check: every configured model must exist on its provider's models endpoint.
Fails loudly (HealthCheckError) with model IDs only; never prints keys, prompts or config."""
from __future__ import annotations

import logging

from newsbot.llm.base import LLMError, expand_pool

log = logging.getLogger("newsbot.llm.health")


class HealthCheckError(LLMError):
    pass


def check_models(settings: dict, providers: dict, strict: bool = True) -> list[dict]:
    """Returns [{provider, model, status}] with status OK | MISSING | SKIP(no key) | ERROR.
    Raises HealthCheckError if strict and any MISSING/ERROR, so a bad model ID stops the run early."""
    results: list[dict] = []
    seen: dict[str, list[str] | None] = {}
    entries = [(tier, e) for tier, es in settings.get("tiers", {}).items() for e in es]
    emb = settings.get("embedding")
    if emb:
        entries.append(("embedding", emb))
    expanded = []                              # a pool entry ("workers_ai") is checked on EVERY account behind it
    for tier, e in entries:
        names = expand_pool(providers, e["provider"]) or [e["provider"]]
        expanded += [(tier, n, e["model"]) for n in names]
    for tier, pname, model in expanded:
        prov = providers.get(pname)
        if prov is None:
            results.append({"provider": pname, "model": model, "tier": tier, "status": "SKIP"})
            continue
        if pname not in seen:
            try:
                seen[pname] = prov.list_models()
            except LLMError:
                seen[pname] = None
        ids = seen[pname]
        if ids is None:
            status = "ERROR"
        elif "*" in ids or model in ids:
            status = "OK"
        else:
            status = "MISSING"
        results.append({"provider": pname, "model": model, "tier": tier, "status": status})
    for r in results:
        log.info("health provider=%s model=%s tier=%s status=%s", r["provider"], r["model"], r["tier"], r["status"])
    bad = [r for r in results if r["status"] in ("MISSING", "ERROR")]
    if strict and bad:
        raise HealthCheckError("model check failed: " + ", ".join(f"{r['provider']}:{r['model']}={r['status']}"
                                                                 for r in bad))
    # a task whose whole chain is SKIP/failed can never run
    for task, tier in settings.get("tasks", {}).items():
        ok = [r for r in results if r["tier"] == tier and r["status"] == "OK"]
        if not ok and strict:
            raise HealthCheckError(f"no usable provider for task {task} (tier {tier})")
    return results
