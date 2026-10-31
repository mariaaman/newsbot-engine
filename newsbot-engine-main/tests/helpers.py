"""Test helpers. make_cfg() builds a plain-namespace config from the brain-template YAML plus
defaults mirroring newsbot/config.py, so M1+ logic can be tested without pydantic.
tests/test_config.py (pydantic) checks these defaults stay in sync."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "brain-template"
FEEDS = ROOT / "tests" / "fixtures" / "feeds"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

DEFAULTS = {
    "site": {"languages": ["bn", "en"], "default": "bn", "timezone_offset_hours": 6,
             "allowed_topics": [], "blocked_topics": ["gossip", "gambling"],
             "categories": ["bangladesh", "world", "politics", "economy", "technology", "sports", "science",
                            "entertainment", "environment", "education"]},
    "limits": {"max_new_per_day": 24, "max_updates_per_story_per_day": 3,
               "min_hours_between_updates": 2, "min_independent_sources": 3,
               "max_sources_per_story": 5, "routing_min_domains_per_language": 2,
               "max_items_per_run": 400, "stable_after_hours": 48, "closed_after_days": 7,
               "min_update_fact_words": 5, "insufficient_sources_cooldown_hours": 3},
    "publish": {"mode": "review"},
    "clustering": {"lexical_threshold": 0.2, "embedding_threshold": 0.78,
                   "story_match_threshold": 0.25, "min_shared_tokens": 2},
    "scoring": {"threshold": 0.6, "per_language_threshold": {},
                "weights": {"velocity": 0.3, "breadth": 0.25, "trends": 0.2, "recency": 0.1,
                            "topic_fit": 0.15, "saturation": 0.2, "global_reach": 0.0},
                "recency_half_life_hours": 6, "velocity_window_hours": 2, "velocity_norm": 3,
                "breadth_norm": 6, "trends_top_n": 20, "saturation_start": 8, "saturation_span": 8},
    "qa": {"min_words": {"bn": 450, "en": 500}, "max_ngram_overlap": 0.08, "ngram_size": 8,
           "title_len": {"bn": (15, 110), "en": (20, 100)}, "min_confirmed_facts": 1, "max_quote_words": 15,
           "duplicate_title_similarity": 0.6, "duplicate_embedding_threshold": 0.9,
           "recent_articles_checked": 300, "max_repair_attempts": 2, "banned_phrases": []},
    "write": {"own_source_chars": 1500, "max_tokens": 4000,
              "local_angle": {"bn": "Explain what this means for readers in Bangladesh.",
                              "en": "Give short background and an international view."}},
    "sensitive": {"keywords": {}},
    "ads": {"enabled": False, "publisher_id": "", "consent_required": False, "slots": {}},
    "notify": {"telegram": {"enabled": True, "language": "bn", "report_idle_runs": True, "include_summary": True,
                            "max_lines": 60, "approve_buttons": False, "edit_url": ""}},
}


PLAIN_DICTS = ("per_language_threshold", "min_words", "title_len", "local_angle", "keywords")


def ns(d):
    return SimpleNamespace(**{k: ns(v) if isinstance(v, dict) and k not in
                              PLAIN_DICTS else v for k, v in d.items()})


def make_cfg(**overrides):
    """overrides like limits={'max_new_per_day': 1}; sources come from brain-template."""
    d = copy.deepcopy(DEFAULTS)
    for section, vals in overrides.items():
        d[section].update(vals)
    feeds = []
    for lang in ("bn", "en"):
        data = yaml.safe_load((TEMPLATE / "config" / f"sources.{lang}.yaml").read_text(encoding="utf-8"))
        feeds += data["feeds"]
    cfg = ns(d)
    cfg.sources = [SimpleNamespace(kind="rss", **f) if "kind" not in f else SimpleNamespace(**f)
                   for f in feeds]
    return cfg


def mock_embedder(texts):
    """Cross-language stand-in: same event -> same vector; everything else one-hot unique."""
    dim = 2 + len(texts)
    out = []
    for i, t in enumerate(texts):
        low = t.lower()
        v = [0.0] * dim
        if "barun" in low or "বরুণ" in t:
            v[0] = 1.0
        elif "bank rate" in low or "policy rate" in low or "interest rate" in low:
            v[1] = 1.0
        else:
            v[2 + i] = 1.0
        out.append(v)
    return out
