"""Pydantic config schema. All tunables live in the brain repo (config/*.yaml);
nothing here is hard-coded policy. Anything external is marked # VERIFY in the YAML."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Lang = Literal["bn", "en"]      # languages the SITE publishes in (output)
# Source feeds may be in ANY language (ja, hi, ar, ...): stories are found in many languages and written up
# in the site languages. A feed language is a short ISO code, e.g. "ja", "zh", "pt".


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
    routing_min_domains_per_language: int = Field(2, ge=1)   # used only when routing_mode == "same_language"
    # cross_language: a story is written in EVERY site language once >= routing_min_orgs independent trusted
    # organisations (in any language) cover it. same_language: the old rule (>= N trusted domains in that language).
    routing_mode: Literal["cross_language", "same_language"] = "cross_language"
    routing_min_orgs: int = Field(2, ge=1)
    max_new_per_run: int = Field(1, ge=1)            # a full article costs minutes and many tokens; the rest wait for the next hour
    max_item_age_hours: float = Field(48, gt=0)       # feed items older than this are ignored
    max_items_per_feed: int = Field(40, ge=1)         # newest N per feed, so one big feed cannot crowd out the others
    feed_workers: int = Field(8, ge=1, le=32)         # feeds are fetched in parallel
    # Variety: the newest stories already written (published or pending) count against candidates from the same lead
    # language, so one country (e.g. Bangladesh) cannot fill every slot while Japanese or Chinese stories wait.
    diversity_window: int = Field(8, ge=1)
    diversity_penalty: float = Field(0.0, ge=0, le=1)   # 0 = off
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
    global_reach: float = Field(0.0, ge=0)    # bonus for stories covered by outlets in several languages (0 = off)


class ClusteringCfg(_Base):
    lexical_threshold: float = Field(0.2, ge=0, le=1)     # Jaccard on content tokens (fallback; embeddings are the real path)
    embedding_threshold: float = Field(0.78, ge=0, le=1)  # cosine, VERIFY per embedding model
    story_match_threshold: float = Field(0.25, ge=0, le=1)
    min_shared_tokens: int = Field(2, ge=1)


class ResearchCfg(_Base):
    """How deep the engine digs for ONE selected story (more sources, more text, more claims = a fuller article)."""
    attach_threshold: float = Field(0.70, ge=0, le=1)   # extra related items (cosine to the story centroid); 0 = off
    max_extra_items: int = Field(8, ge=0)
    window_hours: float = Field(48, gt=0)
    source_chars: int = Field(6000, ge=1000, le=30000)  # chars of each source page kept for extraction
    claims_per_source: int = Field(25, ge=5, le=60)


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
    stop_fraction: float = Field(0.9, gt=0, le=1)
    neurons_per_day_per_account: int = Field(10000, ge=0)   # Cloudflare free allowance of EACH account (VERIFY); 0 = no limit
    neurons_per_day: dict[str, int] = {}            # per-account override, key = cf_<label>
    tokens_per_day: dict[str, int] = {}             # "provider" or "provider/model"
    requests_per_day: dict[str, int] = {}


class TierEntry(_Base):
    provider: Literal["workers_ai", "mock"]         # workers_ai = the pool of ALL Cloudflare accounts
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
    neuron_rates: dict[str, dict[str, float]] = {}  # model -> {in, out}: neurons per million tokens ("default" = unknown models)
    model_params: dict[str, dict[str, Any]] = {}    # model -> extra request fields, e.g. {reasoning_effort: low}

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
    max_repair_attempts: int = Field(2, ge=0, le=3)         # rewrites after a failed gate (names, years, ages, length, citations...); 0 = off
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
    network: Literal["adsense", "adsterra"] = "adsense"   # adsterra = any network that gives you script URLs / ad-unit code
    publisher_id: str = ""            # AdSense only: "pub-1234567890123456" (a "ca-" prefix is accepted)
    consent_required: bool = False    # True: the consent banner gates loading the ad scripts (EU/UK visitors)
    slots: dict[str, str] = {}        # adsense: ad-unit ids; adsterra: ad-unit HTML snippets. Names: after_summary, mid_article, end_article
    scripts: list[str] = []           # adsterra: site-wide script URLs (e.g. a Social Bar); https only
    txt_lines: list[str] = []         # adsterra: lines for ads.txt from your dashboard, e.g. "adsterra.com, 1234567, DIRECT, <id>"


class SourceFeed(_Base):
    name: str
    url: str
    tier: Literal[1, 2, 3]
    lang: str = Field(pattern=r"^[a-z]{2,3}$")   # any language; ISO 639 code
    kind: Literal["rss", "google_news", "google_trends"] = "rss"


class TelegramCfg(_Base):
    enabled: bool = True                  # also needs the TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets
    language: Literal["bn", "en"] = "bn"
    report_idle_runs: bool = True         # a quiet message every run, even when nothing was covered
    include_summary: bool = True          # review messages carry the summary (it passes through Telegram's servers)
    max_lines: int = Field(60, ge=10, le=200)
    approve_buttons: bool = False         # Approve / Reject buttons on pending drafts; needs the approve Worker (docs/TELEGRAM_APPROVE.md)
    edit_url: str = Field("", pattern=r"^(https://[A-Za-z0-9.-]+(:[0-9]+)?/?)?$")   # the approve Worker's address: pending drafts get a private view-and-edit link


class NotifyCfg(_Base):
    telegram: TelegramCfg = TelegramCfg()


class Config(_Base):
    site: SiteCfg = SiteCfg()
    notify: NotifyCfg = NotifyCfg()
    limits: LimitsCfg = LimitsCfg()
    publish: PublishCfg = PublishCfg()
    clustering: ClusteringCfg = ClusteringCfg()
    research: ResearchCfg = ResearchCfg()
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
    """Load config/newsbot.yaml plus every config/sources.<lang>.yaml (any language) from the brain dir."""
    cfg_dir = Path(brain_dir) / "config"
    if not cfg_dir.is_dir():
        raise FileNotFoundError(f"brain config dir not found: {cfg_dir}")
    data = _read_yaml(cfg_dir / "newsbot.yaml")
    llm_extra = _read_yaml(cfg_dir / "llm.yaml")
    if llm_extra:
        data["llm"] = deep_merge(data.get("llm") or {}, llm_extra.get("llm", llm_extra))
    sources: list[dict] = list(data.get("sources", []))
    for path in sorted(cfg_dir.glob("sources.*.yaml")):
        sources += _read_yaml(path).get("feeds", [])
    data["sources"] = sources
    return Config.model_validate(data)
