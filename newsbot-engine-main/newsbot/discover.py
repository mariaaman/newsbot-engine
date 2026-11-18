"""Discovery: Google News RSS, Google Trends RSS (unofficial, tolerate failure), trusted RSS.
Stdlib XML parsing, no extra dependency. Every fetch failure is tolerated and counted."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

from newsbot.models import Item, Trend, domain_of

log = logging.getLogger("newsbot.discover")
MAX_FEED_BYTES = 5_000_000      # a feed bigger than this is cut (and then fails to parse): never read unbounded data

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
                          or _child_text(e, "updated") or _child_text(e, "date"))     # "date" = dc:date (RSS 1.0 / RDF feeds)
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


def http_fetcher(timeout: float = 8.0) -> Fetcher:
    from newsbot.netsafe import safe_get

    def fetch(url: str) -> bytes:
        status, _ctype, body = safe_get(url, timeout, MAX_FEED_BYTES)
        if status >= 400:
            raise OSError(f"http_{status}")
        return body
    return fetch


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def fixture_fetcher(fixture_dir: str | Path, sources) -> Fetcher:
    """Offline fetcher: maps feed url -> <fixture_dir>/<slug(name)>.xml."""
    by_url = {s.url: Path(fixture_dir) / f"{slug(s.name)}.xml" for s in sources}

    def fetch(url: str) -> bytes:
        return by_url[url].read_bytes()
    return fetch


def discover(cfg, fetch: Fetcher, now: datetime | None = None) -> tuple[list[Item], list[Trend], dict]:
    """Many feeds in many languages: fetched in parallel (a dead feed costs one timeout, not one per feed).
    now (optional): items older than limits.max_item_age_hours are dropped."""
    tiers = domain_tiers(cfg.sources)
    lim = cfg.limits
    items: list[Item] = []
    trends: list[Trend] = []
    stats = {"feeds_ok": 0, "feeds_failed": 0}

    def one(src):
        try:
            data = fetch(src.url)
            if src.kind == "google_trends":
                return src, parse_trends(data), None
            got = sorted(parse_feed(data, src, tiers), key=lambda i: i.published, reverse=True)
            return src, got[: getattr(lim, "max_items_per_feed", 10**6)], None
        except Exception as e:  # tolerate any single feed failure; log the type only
            return src, None, e

    with ThreadPoolExecutor(max_workers=max(1, getattr(lim, "feed_workers", 1))) as pool:
        results = list(pool.map(one, cfg.sources))       # keeps feed order: runs stay reproducible
    for src, got, err in results:
        if err is not None:
            stats["feeds_failed"] += 1
            log.warning("feed failed name=%s error=%s", src.name, type(err).__name__)
            continue
        stats["feeds_ok"] += 1
        if src.kind == "google_trends":
            trends += got
        else:
            items += got
    if now is not None:
        oldest = now - timedelta(hours=getattr(lim, "max_item_age_hours", 10**6))
        items = [i for i in items if i.published >= oldest]
    seen: set[str] = set()
    uniq = []
    for it in sorted(items, key=lambda i: i.published, reverse=True):
        if it.url not in seen:
            seen.add(it.url)
            uniq.append(it)
    stats["items"] = len(uniq)
    return uniq[: cfg.limits.max_items_per_run], trends, stats
