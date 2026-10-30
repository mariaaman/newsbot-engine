"""Safe outbound HTTP for feeds and article pages.

Two problems this fixes:
* The crawler used to announce itself as ``https://example.invalid/about`` because nothing ever set
  NEWSBOT_CONTACT_URL. Many publishers block or throttle anonymous bots, so the agent string is now built at call
  time from that variable, or from the brain's ``site.base_url`` (see ``set_contact_from_config``).
* ``requests`` follows redirects anywhere. Every hop is now checked: http(s) only, and the host must resolve to
  public addresses only (no localhost, private, link-local or cloud-metadata ranges). Bodies are size-capped.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urljoin, urlparse

MAX_REDIRECTS = 5
_FALLBACK_CONTACT = "https://example.invalid/about"


def user_agent() -> str:
    return f"NewsBotEngine/0.1 (+{os.environ.get('NEWSBOT_CONTACT_URL') or _FALLBACK_CONTACT})"


def set_contact_from_config(cfg) -> None:
    """Use the site's own address as the crawler contact unless the owner set NEWSBOT_CONTACT_URL explicitly."""
    if os.environ.get("NEWSBOT_CONTACT_URL"):
        return
    base = str(getattr(getattr(cfg, "site", None), "base_url", "") or "").strip()
    if base.startswith("https://") and "example" not in base:
        os.environ["NEWSBOT_CONTACT_URL"] = base.rstrip("/") + "/"


def is_public_host(host: str) -> bool:
    """True only if EVERY address the name resolves to is a global (public) address."""
    if not host or host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith((".local", ".internal")):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addrs = {i[4][0].split("%")[0] for i in infos}
    return bool(addrs) and all(ipaddress.ip_address(a).is_global for a in addrs)


def check_url(url: str, resolver=is_public_host) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise OSError("blocked_scheme")
    if not resolver(p.hostname):
        raise OSError("blocked_host")


def safe_get(url: str, timeout: float, max_bytes: int, *, session=None, resolver=is_public_host):
    """GET with per-hop checks. Returns (status, content_type, body). Raises OSError (type name only, no URL)."""
    import requests
    get = (session or requests).get
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url, resolver)
        try:
            with get(url, headers={"User-Agent": user_agent()}, timeout=timeout, stream=True, allow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf += chunk
                    if len(buf) >= max_bytes:
                        del buf[max_bytes:]
                        break
                return r.status_code, r.headers.get("content-type", ""), bytes(buf)
        except requests.RequestException as e:
            raise OSError(type(e).__name__) from None
    raise OSError("too_many_redirects")
