"""Plain dataclasses shared by pipeline stages (stdlib only, so they run anywhere)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


@dataclass(frozen=True)
class Item:
    title: str
    url: str
    domain: str
    published: datetime
    lang: str
    source_name: str
    tier: int = 3
    kind: str = "rss"


@dataclass(frozen=True)
class Trend:
    keyword: str
    rank: int
    traffic: int = 0


@dataclass
class Cluster:
    story_id: str
    items: list[Item]
    known: bool = False
    tokens: frozenset[str] = frozenset()


@dataclass
class Story:
    story_id: str
    status: str = "developing"          # developing | stable | closed
    first_seen: str = ""
    last_updated: str = ""
    route: str = "bn"                   # bn | en | both
    fact_sheet_version: int = 0
    versions: dict = field(default_factory=dict)   # lang -> {url, published_at, last_updated}
    urls: list = field(default_factory=list)       # source urls already used
    timeline: list = field(default_factory=list)   # [{ts, text}]
    keywords: list = field(default_factory=list)
    updates: list = field(default_factory=list)    # ISO timestamps of updates (published or parked) for the daily cap
    last_news: str = ""                            # last time a new trusted item about the story was seen (status clock)
    lead_lang: str = ""                            # language most of its trusted sources are in (variety rule)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Story":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Source:
    id: str                 # S1, S2 ... (used as [S1] citation markers)
    name: str
    tier: int
    url: str
    domain: str
    lang: str
    published_at: str
    text: str = ""          # first 6,000 chars of extracted main text (untrusted data)

    def public_dict(self) -> dict:
        d = asdict(self)
        d.pop("text")
        return d


@dataclass
class Claim:
    id: str                 # S1.C3
    source_id: str
    kind: str               # who | what | when | where | number | quote
    text: str


@dataclass
class Fact:
    id: str                 # F1
    kind: str
    statement: str
    status: str             # confirmed | reported | disputed
    claim_ids: list = field(default_factory=list)
    source_ids: list = field(default_factory=list)
    note: str = ""


@dataclass
class FactSheet:
    story_id: str
    version: int
    facts: list
    sources: list           # Source.public_dict() entries
    source_texts: dict = field(default_factory=dict, repr=False)   # source id -> fetched text; never serialised

    def to_dict(self) -> dict:
        return {"story_id": self.story_id, "version": self.version, "sources": self.sources,
                "facts": [asdict(f) for f in self.facts]}
