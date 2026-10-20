"""Fetch trusted sources politely (robots.txt, timeout, size cap, UA), extract main text, truncate.
All fetched text is untrusted data. Fetcher is injectable so everything runs offline on fixtures."""
from __future__ import annotations

import codecs
import html as htmllib
import json
import logging
import os
import re
import urllib.robotparser
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from newsbot.models import Cluster, Item, Source, iso
from newsbot.prompts import strip_injection

log = logging.getLogger("newsbot.fetch")
USER_AGENT = f"NewsBotEngine/0.1 (+{os.environ.get('NEWSBOT_CONTACT_URL', 'https://example.invalid/about')})"  # set NEWSBOT_CONTACT_URL to your site's about page
MAX_CHARS = 6000
MAX_BYTES = 1_500_000
TIMEOUT_S = 10


@dataclass
class FetchResult:
    status: int
    content_type: str
    body: bytes


Fetcher = Callable[[str], FetchResult]


def http_fetcher(timeout: float = TIMEOUT_S, max_bytes: int = MAX_BYTES) -> Fetcher:
    def fetch(url: str) -> FetchResult:
        import requests
        try:
            with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, stream=True) as r:
                buf = b""
                for chunk in r.iter_content(65536):
                    buf += chunk
                    if len(buf) >= max_bytes:
                        buf = buf[:max_bytes]
                        break
                return FetchResult(r.status_code, r.headers.get("content-type", ""), buf)
        except requests.RequestException as e:
            raise OSError(type(e).__name__) from None
    return fetch


def fixture_article_fetcher(fixture_dir: str | Path) -> Fetcher:
    d = Path(fixture_dir)
    idx = json.loads((d / "index.json").read_text(encoding="utf-8"))

    def fetch(url: str) -> FetchResult:
        if url.endswith("/robots.txt"):
            return FetchResult(404, "text/plain", b"")
        if url not in idx:
            return FetchResult(404, "text/html", b"")
        return FetchResult(200, "text/html; charset=utf-8", (d / idx[url]).read_bytes())
    return fetch


class RobotsCache:
    """RFC 9309: 4xx robots.txt = allow all; 5xx or network failure = disallow (be polite)."""

    def __init__(self, fetcher: Fetcher, agent: str = "NewsBotEngine"):
        self._f, self._agent, self._cache = fetcher, agent, {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._cache:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self._f(f"{base}/robots.txt")
                if r.status >= 500:
                    rp.disallow_all = True
                elif r.status >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(r.body.decode("utf-8", "replace").splitlines())
            except OSError:
                rp.disallow_all = True
            self._cache[base] = rp
        return self._cache[base].can_fetch(self._agent, url)


_CHARSET_HDR = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.I)
_CHARSET_META = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.:-]+)", re.I)


def decode_body(body: bytes, content_type: str = "") -> str:
    """Decode with the page's own charset (header, then <meta>): Japanese, Chinese, Russian and Turkish sites are
    often not UTF-8. Falls back to UTF-8 with replacement characters."""
    names = []
    m = _CHARSET_HDR.search(content_type or "")
    if m:
        names.append(m.group(1))
    m = _CHARSET_META.search(body[:4096])
    if m:
        names.append(m.group(1).decode("ascii", "ignore"))
    for name in names:
        try:
            codecs.lookup(name)
            return body.decode(name, "replace")
        except (LookupError, ValueError):
            continue
    return body.decode("utf-8", "replace")


_SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript", "iframe", "svg"}


class _Text(HTMLParser):
    """Stdlib fallback extractor: paragraph text outside boilerplate containers."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth_skip, self.in_p, self.paras, self.cur = 0, False, [], []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.depth_skip += 1
        elif tag == "p" and not self.depth_skip:
            self.in_p, self.cur = True, []

    def handle_endtag(self, tag):
        if tag in _SKIP and self.depth_skip:
            self.depth_skip -= 1
        elif tag == "p" and self.in_p:
            t = re.sub(r"\s+", " ", "".join(self.cur)).strip()
            if t:
                self.paras.append(t)
            self.in_p = False

    def handle_data(self, data):
        if self.in_p and not self.depth_skip:
            self.cur.append(data)


def extract_text(html: str, max_chars: int = MAX_CHARS) -> str:
    text = ""
    try:
        import trafilatura
        text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    except ImportError:
        pass
    if not text:
        p = _Text()
        p.feed(html)
        text = "\n".join(p.paras)
    text = htmllib.unescape(re.sub(r"[ \t]+", " ", text)).strip()
    return text[:max_chars]


def pick_items(cluster: Cluster, n: int) -> list[Item]:
    """Distinct trusted organisations only (tier 1-2, direct RSS, never aggregators), best tier first."""
    from newsbot.verify import org_key
    seen, out = set(), []
    for it in sorted(cluster.items, key=lambda i: (i.tier, i.published)):
        if it.tier > 2 or it.kind != "rss":
            continue
        key = org_key(it.domain)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if len(out) == n:
            break
    return out


def fetch_sources(cluster: Cluster, cfg, fetcher: Fetcher, min_chars: int = 150) -> list[Source]:
    """Fetch up to max_sources_per_story pages from distinct organisations. Candidates beyond that number are kept
    in reserve: a blocked, failed or too-short page is replaced by the next organisation."""
    robots = RobotsCache(fetcher)
    sources: list[Source] = []
    want = cfg.limits.max_sources_per_story
    chars = getattr(getattr(cfg, "research", None), "source_chars", MAX_CHARS)
    for it in pick_items(cluster, want * 3):
        if len(sources) >= want:
            break
        try:
            if not robots.allowed(it.url):
                log.info("robots disallow domain=%s", it.domain)
                continue
            r = fetcher(it.url)
        except OSError as e:
            log.info("fetch failed domain=%s error=%s", it.domain, type(e).__name__)
            continue
        if r.status != 200 or not re.search(r"html|xml|text", r.content_type, re.I):
            log.info("fetch skipped domain=%s status=%d", it.domain, r.status)
            continue
        text, removed = strip_injection(extract_text(decode_body(r.body, r.content_type), chars))
        if removed:
            log.warning("injection-like sentences removed domain=%s count=%d", it.domain, removed)
        if len(text) < min_chars:
            log.info("extract too short domain=%s", it.domain)
            continue
        sources.append(Source(f"S{len(sources) + 1}", it.source_name, it.tier, it.url, it.domain, it.lang,
                              iso(it.published), text))
    return sources
