from pathlib import Path

import pytest
from pydantic import ValidationError

from newsbot.config import Config, load_config

TEMPLATE = Path(__file__).resolve().parent.parent / "brain-template"


def test_template_loads_with_spec_defaults():
    cfg = load_config(TEMPLATE)
    assert cfg.limits.max_new_per_day == 24
    assert cfg.limits.max_updates_per_story_per_day == 3
    assert cfg.publish.mode == "review"
    assert cfg.scoring.threshold == 0.6
    assert cfg.qa.min_words == {"bn": 450, "en": 500}
    assert cfg.ads.enabled is False
    assert {s.lang for s in cfg.sources} == {"bn", "en"}
    assert set(cfg.llm.tasks.values()) <= set(cfg.llm.tiers) and cfg.llm.embedding.provider == "workers_ai"
    assert cfg.llm.budget.tokens_per_day["groq/openai/gpt-oss-120b"] == 180000


def test_missing_config_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path)


def test_unknown_key_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({"limits": {"max_new_per_day": 5, "bogus": 1}})


def test_negative_weight_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({"scoring": {"weights": {"velocity": -1}}})


def test_default_language_must_be_listed():
    with pytest.raises(ValidationError):
        Config.model_validate({"site": {"languages": ["en"], "default": "bn"}})


def test_too_many_categories_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({"site": {"categories": [f"c{i}" for i in range(13)]}})


def test_duplicate_source_urls_rejected():
    feed = {"name": "a", "url": "https://x/rss", "tier": 1, "lang": "en"}
    with pytest.raises(ValidationError):
        Config.model_validate({"sources": [feed, feed]})


def test_test_helper_defaults_match_schema():
    """tests/helpers.DEFAULTS mirrors Config defaults so non-pydantic tests stay honest."""
    from tests.helpers import DEFAULTS
    d = Config().model_dump()
    for section in ("limits", "clustering", "scoring", "qa", "write", "sensitive", "ads"):
        for k, v in DEFAULTS[section].items():
            assert d[section][k] == v, (section, k)
    assert d["site"]["timezone_offset_hours"] == DEFAULTS["site"]["timezone_offset_hours"]
    assert d["notify"]["telegram"] == DEFAULTS["notify"]["telegram"]


def test_llm_task_with_undefined_tier_rejected():
    with pytest.raises(ValidationError):
        Config.model_validate({"llm": {"tasks": {"extract": "tiny"}, "tiers": {"small": []}}})


def test_site_script_defaults_match_schema():
    """scripts/gen_site_config.py is stdlib-only, so its defaults are duplicated: keep them in sync with Config."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("gen_site_config", TEMPLATE.parent / "scripts" / "gen_site_config.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    cfg = Config()
    assert gen.DEFAULT_DISCLOSURE == cfg.site.ai_disclosure
    assert gen.DEFAULT_CATEGORIES == cfg.site.categories
    assert set(gen.SLOTS) >= set(load_config(TEMPLATE).ads.slots)
    assert cfg.ads.consent_required is False and cfg.site.bengali_font is False
