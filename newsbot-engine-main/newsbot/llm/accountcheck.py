"""Account check (`python -m newsbot llmcheck`): does EVERY Cloudflare account of the pool really work?
Per account: (1) the token is accepted and sees the model catalogue (auth + Workers AI permission), (2) every model the
config uses is in that account's catalogue, (3) one tiny real call per model and one embedding call. The result goes to
state/llm_health.json for the dashboard. Never writes tokens, account ids, prompts or answers: labels, model IDs and
status words only. The probes are tiny (a few dozen neurons per account) and are not added to usage.json."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from newsbot.llm.base import LLMError, ProviderError, RateLimited, expand_pool, safe_reason

PROBE = [{"role": "user", "content": "Reply with the single word OK."}]

_WHY = {"status_401": "token_rejected", "status_403": "token_has_no_workers_ai_permission",
        "status_404": "model_or_account_not_found", "status_429": "daily_allowance_or_rate_limit"}


def why(e: Exception) -> str:
    r = safe_reason(e)
    if isinstance(e, RateLimited):
        return _WHY["status_429"]
    return _WHY.get("_".join(r.split("_")[:2]), r)


def _models(settings: dict) -> tuple[list[str], str | None]:
    seen: list[str] = []
    for entries in settings.get("tiers", {}).values():
        for e in entries:
            if e.get("provider") == "workers_ai" and e["model"] not in seen:
                seen.append(e["model"])
    emb = settings.get("embedding") or {}
    return seen, (emb.get("model") if emb.get("provider") == "workers_ai" else None)


def check_account(prov, chat_models: list[str], embed_model: str | None) -> dict:
    out: dict = {"ok": False, "auth": "ok", "models": {}, "embedding": None}
    try:
        ids = prov.list_models()
    except (LLMError, RateLimited) as e:
        out["auth"] = why(e)
        return out
    allok = True
    for m in chat_models:
        if "*" not in ids and m not in ids:
            out["models"][m] = {"status": "MISSING"}
            allok = False
            continue
        try:
            r = prov.chat(m, PROBE, max_tokens=256, task="llmcheck")
            out["models"][m] = {"status": "OK", "tokens": r.total_tokens}
        except (LLMError, RateLimited) as e:
            out["models"][m] = {"status": "FAIL", "reason": why(e)}
            allok = False
    if embed_model:
        if "*" not in ids and embed_model not in ids:
            out["embedding"] = {"model": embed_model, "status": "MISSING"}
            allok = False
        else:
            try:
                res = prov._run(embed_model, {"text": ["test"]})
                good = isinstance(res.get("data"), list) and len(res["data"]) == 1
                out["embedding"] = {"model": embed_model, "status": "OK" if good else "BAD_ANSWER"}
                allok &= good
            except (LLMError, RateLimited) as e:
                out["embedding"] = {"model": embed_model, "status": "FAIL", "reason": why(e)}
                allok = False
    out["ok"] = allok
    return out


def run_account_check(settings: dict, providers: dict, now: datetime | None = None) -> dict:
    chat_models, embed_model = _models(settings)
    accounts = {}
    for name in expand_pool(providers, "workers_ai"):
        accounts[name[3:] if name.startswith("cf_") else name] = check_account(providers[name], chat_models, embed_model)
    ok = sum(1 for a in accounts.values() if a["ok"])
    return {"checked_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "models": chat_models, "embedding": embed_model, "accounts": accounts,
            "summary": {"accounts": len(accounts), "ok": ok, "failed": len(accounts) - ok}}


def write_report(report: dict, path: str | Path) -> None:
    from newsbot.state import _atomic_write
    _atomic_write(Path(path), json.dumps(report, sort_keys=True, indent=1, ensure_ascii=False) + "\n")
