"""Discovery: Google News RSS, Google Trends RSS (unofficial, tolerate failure), trusted RSS.
Stdlib XML parsing, no extra dependency. Every fetch failure is tolerated and counted."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

from newsbot.models import Item, Trend, domain_of

log = logging.getLogger("newsbot.discover")
USER_AGENT = "NewsBotEngine/0.1 (+https://example.invalid/about)"  # VERIFY: set a real contact URL

Fetcher = Callable[[str], bytes]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, name: str) -> str:
    for c in el:
        if _local(c.tag) == name and c.text:
            return c.text.strip()
    return ""


def _parse_date(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def domain_tiers(sources) -> dict[str, int]:
    """Map domain -> best tier from the non-aggregator feeds, used to tier Google News items."""
    out: dict[str, int] = {}
    for s in sources:
        if s.kind == "rss":
            d = domain_of(s.url)
            # feeds.bbci.co.uk -> bbc.co.uk style: also register the registrable tail
            for key in {d, ".".join(d.split(".")[-3:]), ".".join(d.split(".")[-2:])}:
                out[key] = min(out.get(key, 9), s.tier)
    return out


def _tier_for(domain: str, tiers: dict[str, int], default: int) -> int:
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        t = tiers.get(".".join(parts[i:]))
        if t:
            return t
    return default


_SUFFIX = re.compile(r"\s+[-–|]\s+[^-–|]{2,60}$")


def parse_feed(xml: bytes, source, tiers: dict[str, int] | None = None) -> list[Item]:
    """RSS 2.0 and Atom. For Google News, the real publisher comes from <source url=...>."""
    tiers = tiers or {}
    root = ET.fromstring(xml)
    entries = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    items: list[Item] = []
    for e in entries:
        title = _child_text(e, "title")
        link = _child_text(e, "link")
        if not link:
            for c in e:
                if _local(c.tag) == "link" and c.attrib.get("href"):
                    link = c.attrib["href"]
        pub = _parse_date(_child_text(e, "pubDate") or _child_text(e, "published")
                          or _child_text(e, "updated"))
        if not title or not link or pub is None:
            continue
        domain = domain_of(link)
        if source.kind == "google_news":
            for c in e:
                if _local(c.tag) == "source" and c.attrib.get("url"):
                    domain = domain_of(c.attrib["url"])
                    suffix = f" - {(c.text or '').strip()}"
                    if c.text and title.endswith(suffix):
                        title = title[: -len(suffix)]
        tier = _tier_for(domain, tiers, source.tier) if source.kind == "google_news" else source.tier
        items.append(Item(title=title, url=link, domain=domain, published=pub,
                          lang=source.lang, source_name=source.name, tier=tier, kind=source.kind))
    return items


def _traffic(raw: str) -> int:
    """'200K+' -> 200000, '1M+' -> 1000000, '500+' -> 500, '' -> 0."""
    m = re.match(r"\s*([\d,.]+)\s*([KkMm]?)", raw or "")
    if not m:
        return 0
    num = float(m.group(1).replace(",", ""))
    mult = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(num * mult)


def parse_trends(xml: bytes) -> list[Trend]:
    """Google Trends daily RSS (unofficial; VERIFY format). Matches by local tag name."""
    root = ET.fromstring(xml)
    out: list[Trend] = []
    for rank, e in enumerate((e for e in root.iter() if _local(e.tag) == "item"), start=1):
        kw = _child_text(e, "title")
        if kw:
            out.append(Trend(keyword=kw, rank=rank, traffic=_traffic(_child_text(e, "approx_traffic"))))
    return out


def http_fetcher(timeout: float = 10.0) -> Fetcher:
    import requests  # imported lazily so offline modules need nothing

    def fetch(url: str) -> bytes:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        return r.content
    return fetch


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def fixture_fetcher(fixture_dir: str | Path, sources) -> Fetcher:
    """Offline fetcher: maps feed url -> <fixture_dir>/<slug(name)>.xml."""
    by_url = {s.url: Path(fixture_dir) / f"{slug(s.name)}.xml" for s in sources}

    def fetch(url: str) -> bytes:
        return by_url[url].read_bytes()
    return fetch


def discover(cfg, fetch: Fetcher) -> tuple[list[Item], list[Trend], dict]:
    tiers = domain_tiers(cfg.sources)
    items: list[Item] = []
    trends: list[Trend] = []
    stats = {"feeds_ok": 0, "feeds_failed": 0}
    for src in cfg.sources:
        try:
            data = fetch(src.url)
            if src.kind == "google_trends":
                trends += parse_trends(data)
            else:
                items += parse_feed(data, src, tiers)
            stats["feeds_ok"] += 1
        except Exception as e:  # tolerate any single feed failure; log the type only
            stats["feeds_failed"] += 1
            log.warning("feed failed name=%s error=%s", src.name, type(e).__name__)
    seen: set[str] = set()
    uniq = []
    for it in sorted(items, key=lambda i: i.published, reverse=True):
        if it.url not in seen:
            seen.add(it.url)
            uniq.append(it)
    stats["items"] = len(uniq)
    return uniq[: cfg.limits.max_items_per_run], trends, stats
