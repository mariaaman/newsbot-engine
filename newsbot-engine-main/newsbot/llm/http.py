"""Tiny injectable HTTP transport so providers are testable offline.
post/get return (status, headers_lowercase, json_or_None). Errors never include request bodies."""
from __future__ import annotations

from typing import Any, Callable

Transport = Callable[..., tuple[int, dict, Any]]


def requests_transport(method: str, url: str, *, headers: dict, json: Any = None, timeout: float = 60):
    import requests
    try:
        r = requests.request(method, url, headers=headers, json=json, timeout=timeout)
    except requests.RequestException as e:
        from newsbot.llm.base import ProviderError
        raise ProviderError(f"network error: {type(e).__name__}") from None
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, {k.lower(): v for k, v in r.headers.items()}, body


def retry_after_s(headers: dict) -> float | None:
    v = headers.get("retry-after")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def raise_for_status(status: int, headers: dict) -> None:
    from newsbot.llm.base import ProviderError, RateLimited
    if status == 429:
        raise RateLimited(retry_after_s(headers))
    if status >= 500:
        raise RateLimited(retry_after_s(headers) or 5)   # transient: back off and retry
    if status >= 400:
        raise ProviderError(f"http {status}")
