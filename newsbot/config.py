"""Pydantic config schema. All tunables live in the brain repo (config/*.yaml);
nothing here is hard-coded policy. Anything external is marked # VERIFY in the YAML."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Lang = Literal["bn", "en"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SiteCfg(_Base):
    languages: list[Lang] = ["bn", "en"]
    default: Lang = "bn"
    name: str = "TBD"
    timezone_offset_hours: float = 6  # Asia/Dhaka, no DST; defines the "day" for daily caps
    base_url: str = "https://example.pages.dev"
    ai_disclosure: dict[Lang, str] = {
        "bn": "এই নিবন্ধটি এআই-সহায়তায় তৈরি এবং আমাদের সম্পাদকীয় নীতি মেনে চলে।",
        "en": "This article is AI-assisted and follows our editorial policy.",
    }
    bengali_font: bool = False        # True once static/fonts/NotoSansBengali-subset.woff2 exists (owner step)
    categories: list[str] = ["bangladesh", "world", "politics", "economy", "technology",
                             "sports", "science", "entertainment", "environment", "education"]
    allowed_topics: list[str] = []
    blocked_topics: list[str] = []

    @model_validator(mode="after")
    def _default_in_languages(self) -> "SiteCfg":
        if self.default not in self.languages:
            raise ValueError("site.default must be one of site.languages")
        if len(self.categories) > 12:
            raise ValueError("at most 12 categories (file budget)")
        return self


class LimitsCfg(_Base):
    max_new_per_day: int = Field(24, ge=0)
    max_updates_per_story_per_day: int = Field(3, ge=0)
    min_hours_between_updates: float = Field(2, ge=0)
    min_independent_sources: int = Field(3, ge=1)
    max_sources_per_story: int = Field(5, ge=1)
    routing_min_domains_per_language: int = Field(2, ge=1)
    max_items_per_run: int = Field(400, ge=10)
    stable_after_hours: int = 48
    closed_after_days: int = 7
    min_update_fact_words: int = Field(5, ge=1)     # new facts shorter than this do not justify an update (tiny changes)
    insufficient_sources_cooldown_hours: float = Field(3, ge=0)   # 0 = retry every hour


class PublishCfg(_Base):
    mode: Literal["review", "auto"] = "review"


class Weights(_Base):
    velocity: float = Field(0.3, ge=0)
    breadth: float = Field(0.25, ge=0)
    trends: float = Field(0.2, ge=0)
    recency: float = Field(0.1, ge=0)
    topic_fit: float = Field(0.15, ge=0)
    saturation: float = Field(0.2, ge=0)


class ClusteringCfg(_Base):
    lexical_threshold: float = Field(0.2, ge=0, le=1)     # Jaccard on content tokens (fallback; embeddings are the real path)
    embedding_threshold: float = Field(0.78, ge=0, le=1)  # cosine, VERIFY per embedding model
    story_match_threshold: float = Field(0.25, ge=0, le=1)
    min_shared_tokens: int = Field(2, ge=1)


class ScoringCfg(_Base):
    threshold: float = Field(0.6, ge=0)
    per_language_threshold: dict[Lang, float] = {}
    weights: Weights = Weights()
    recency_half_life_hours: float = Field(6, gt=0)
    velocity_window_hours: float = Field(2, gt=0)
    velocity_norm: float = Field(3, gt=0)    # new trusted domains in window that give velocity 1.0
    breadth_norm: float = Field(6, gt=0)     # trusted domains that give breadth 1.0
    trends_top_n: int = Field(20, ge=1)
    saturation_start: int = Field(8, ge=1)   # domains before saturation penalty starts
    saturation_span: int = Field(8, ge=1)


class LLMBudget(_Base):
    groq_tokens_per_day: int = Field(180000, ge=0)  # VERIFY free-tier limit (spec starter key)
    groq_requests_per_day: int = Field(900, ge=0)   # VERIFY
    stop_fraction: float = Field(0.9, gt=0, le=1)
    tokens_per_day: dict[str, int] = {}             # "provider" or "provider/model"
    requests_per_day: dict[str, int] = {}


class TierEntry(_Base):
    provider: Literal["groq", "workers_ai", "gemini", "mock"]
    model: str


class RetryCfg(_Base):
    max_retries: int = Field(3, ge=0)
    base_delay_s: float = Field(2, ge=0)
    max_delay_s: float = Field(60, ge=0)


class LLMCfg(_Base):
    budget: LLMBudget = LLMBudget()
    tasks: dict[str, str] = {"extract": "small", "verify": "strong", "write_bn": "strong",
                             "write_en": "strong", "qa_check": "small"}
    tiers: dict[str, list[TierEntry]] = {}
    embedding: TierEntry | None = None
    retry: RetryCfg = RetryCfg()

    @model_validator(mode="after")
    def _tasks_have_tiers(self) -> "LLMCfg":
        if self.tiers:
            missing = {t for t in self.tasks.values() if t not in self.tiers}
            if missing:
                raise ValueError(f"tasks reference undefined tiers: {sorted(missing)}")
        return self


class QACfg(_Base):
    min_words: dict[Lang, int] = {"bn": 450, "en": 500}
    max_ngram_overlap: float = Field(0.08, ge=0, le=1)
    ngram_size: int = Field(8, ge=3)
    title_len: dict[Lang, tuple[int, int]] = {"bn": (15, 110), "en": (20, 100)}
    min_confirmed_facts: int = Field(1, ge=0)              # fewer confirmed facts -> pending (low confidence)
    max_quote_words: int = Field(15, ge=1)
    duplicate_title_similarity: float = Field(0.6, ge=0, le=1)       # Jaccard on title tokens
    duplicate_embedding_threshold: float = Field(0.9, ge=0, le=1)    # cosine, VERIFY per embedding model
    recent_articles_checked: int = Field(300, ge=1)
    banned_phrases: list[str] = []                         # added to the built-in list in qa.py


class WriteCfg(_Base):
    own_source_chars: int = Field(1500, ge=0)              # chars of each same-language source shown to the writer
    max_tokens: int = Field(4000, ge=500)
    local_angle: dict[Lang, str] = {
        "bn": "Explain what this means for readers in Bangladesh.",
        "en": "Give short background and an international view.",
    }


class SensitiveCfg(_Base):
    # category -> keywords (English matched on word boundaries, Bengali as substrings). Empty = built-in lists.
    keywords: dict[str, list[str]] = {}


class AdsCfg(_Base):
    enabled: bool = False
    publisher_id: str = ""            # AdSense id, "pub-1234567890123456" (a "ca-" prefix is accepted)
    consent_required: bool = False    # True: the consent banner gates loading the ad script (EU/UK visitors)
    slots: dict[str, str] = {}        # optional AdSense ad-unit ids: after_summary, mid_article, end_article


class SourceFeed(_Base):
    name: str
    url: str
    tier: Literal[1, 2, 3]
    lang: Lang
    kind: Literal["rss", "google_news", "google_trends"] = "rss"


class Config(_Base):
    site: SiteCfg = SiteCfg()
    limits: LimitsCfg = LimitsCfg()
    publish: PublishCfg = PublishCfg()
    clustering: ClusteringCfg = ClusteringCfg()
    scoring: ScoringCfg = ScoringCfg()
    llm: LLMCfg = LLMCfg()
    qa: QACfg = QACfg()
    write: WriteCfg = WriteCfg()
    sensitive: SensitiveCfg = SensitiveCfg()
    ads: AdsCfg = AdsCfg()
    sources: list[SourceFeed] = []

    @field_validator("sources")
    @classmethod
    def _unique_urls(cls, v: list[SourceFeed]) -> list[SourceFeed]:
        urls = [s.url for s in v]
        if len(urls) != len(set(urls)):
            raise ValueError("duplicate source urls")
        return v


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(brain_dir: str | Path) -> Config:
    """Load config/newsbot.yaml plus config/sources.<lang>.yaml from the brain dir."""
    cfg_dir = Path(brain_dir) / "config"
    if not cfg_dir.is_dir():
        raise FileNotFoundError(f"brain config dir not found: {cfg_dir}")
    data = _read_yaml(cfg_dir / "newsbot.yaml")
    llm_extra = _read_yaml(cfg_dir / "llm.yaml")
    if llm_extra:
        data["llm"] = deep_merge(data.get("llm") or {}, llm_extra.get("llm", llm_extra))
    sources: list[dict] = list(data.get("sources", []))
    for lang in ("bn", "en"):
        sources += _read_yaml(cfg_dir / f"sources.{lang}.yaml").get("feeds", [])
    data["sources"] = sources
    return Config.model_validate(data)
