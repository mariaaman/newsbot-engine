"""More output languages: the catalog, QA limits, the site generator (Hugo languages, UI labels, RTL, 404, trust pages) and the
dashboard's copy of the catalog. Zero-arg functions (tempfile), so they run under pytest and the offline runner."""
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from newsbot import langs as L
from newsbot.llm.base import LLM, LLMError
from newsbot.llm.dryrun import dry_run_provider
from newsbot.qa import run_gates
from newsbot.write import build_messages, write_article
from tests.helpers import TEMPLATE, make_cfg
from tests.test_m4 import STORIES, dry_llm

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = load_script("gen_site_config")
NEW = ["bn", "en", "hi", "ar", "ja", "de", "ne"]


def test_catalog_is_complete_and_the_shipped_english_labels_are_the_catalog_labels():
    assert len(L.UI_KEYS) == 40 and len(L.EN_UI) == 40
    for code, m in L.LANGUAGES.items():
        assert code == m["code"] and L.CODE_RE.match(code)
        assert m["ui"] is None or list(m["ui"]) == list(L.UI_KEYS), code
        assert m["name"] and m["native"] and m["locale"] and m["update_note"] and m["disclosure"], code
        lo, hi = m["title_len"]
        assert 0 < lo < hi and m["min_words"] > 0
    assert {"bn", "en", "hi", "ur", "ar", "ja", "zh", "ko", "de", "es", "fr"} <= set(L.LANGUAGES)
    for code in ("en", "bn"):                                  # the shipped i18n files and the catalog must say the same
        shipped = yaml.safe_load((SITE / "i18n" / f"{code}.yaml").read_text(encoding="utf-8"))
        assert list(shipped) == list(L.UI_KEYS) or set(shipped) == set(L.UI_KEYS), code
    en = yaml.safe_load((SITE / "i18n" / "en.yaml").read_text(encoding="utf-8"))
    assert all(en[k]["other"] == v for k, v in L.ui_strings("en").items())
    assert [c for c, m in L.LANGUAGES.items() if m["rtl"]] == ["ur", "ar", "fa"]
    assert [c for c, m in L.LANGUAGES.items() if m["spaceless"]] == ["ja", "zh", "th"]


def test_the_dashboards_copy_of_the_catalog_is_current():
    gen_js = load_script("gen_dashboard_langs")
    assert (ROOT / "dashboard" / "src" / "langs.js").read_text(encoding="utf-8") == gen_js.render(), \
        "run: python scripts/gen_dashboard_langs.py and commit dashboard/src/langs.js"


def test_defaults_apply_for_a_language_without_config_values_and_config_values_win():
    cfg = make_cfg()
    assert "hi" not in cfg.qa.min_words and L.min_words(cfg.qa, "hi") == 450 and L.min_words(cfg.qa, "en") == 500
    assert L.title_len(cfg.qa, "ja") == (10, 60) and L.title_len(cfg.qa, "bn") == tuple(cfg.qa.title_len["bn"])
    assert "Hindi" in L.local_angle(cfg.write, "hi") and L.local_angle(cfg.write, "bn") == cfg.write.local_angle["bn"]
    cfg.qa.min_words["hi"] = 777
    assert L.min_words(cfg.qa, "hi") == 777
    assert L.min_words(cfg.qa, "zz") == 450 and L.meta("zz")["names"]           # a code outside the catalog never raises
    assert L.update_note("hi") != L.update_note("en") and L.disclosure(cfg.site, "ar").startswith("أُعدّت")


def test_length_is_counted_in_letters_for_scripts_without_spaces():
    ja = "これは日本語の文章です。" * 100
    assert L.length_units(ja, "ja", ja.split()) > 900 and len(ja.split()) == 1
    assert L.length_units("a b c d", "en", ["a", "b", "c", "d"]) == 4
    assert L.unit_name("ja") == "characters" and L.unit_name("hi") == "words"


def test_every_catalog_language_writes_an_article_that_passes_every_hard_gate_in_the_dry_run():
    cfg = make_cfg()
    for lang in sorted(L.LANGUAGES):
        art, _ = write_article(dry_llm(), None, cfg, STORIES["ev-metro"], lang)
        r = run_gates(art, STORIES["ev-metro"], lang, cfg, recent=[], run_llm_check=False)
        assert r.failures == [] and r.review == [], (lang, r.failures, r.review)


def test_the_capitalised_name_check_runs_for_english_only_because_other_languages_translate_names():
    assert [c for c, m in L.LANGUAGES.items() if m["names"]] == ["bn", "en"]
    cfg = make_cfg()
    art, _ = write_article(dry_llm(), None, cfg, STORIES["ev-metro"], "es")
    art = {**art, "body_markdown": art["body_markdown"].replace("\n\n## ", "\n\nEl presidente de Estados Unidos y Naciones Unidas.\n\n## ", 1)}
    for lang in ("es", "fr", "de", "pt", "id"):          # real log 2026-11-02: es/fr drafts failed again and again on translated place names
        assert "unsupported_name" not in run_gates(art, STORIES["ev-metro"], lang, cfg, recent=[], run_llm_check=False).failures, lang
    assert "unsupported_name" in run_gates(art, STORIES["ev-metro"], "en", cfg, recent=[], run_llm_check=False).failures
    bad = {**art, "body_markdown": art["body_markdown"].replace("\n\n## ", "\n\nHubo 98765 casos.\n\n## ", 1)}
    for lang in ("es", "de", "ja"):                         # the number check stays on everywhere
        assert "unsupported_number" in run_gates(bad, STORIES["ev-metro"], lang, cfg, recent=[], run_llm_check=False).failures, lang


def test_writer_prompt_names_the_language_the_local_angle_and_the_length_unit():
    cfg = make_cfg()
    system = build_messages(STORIES["ev-metro"], "ja", cfg, None)[0]["content"]
    assert "ja (Japanese)" in system and "900 characters (letters, no spaces)" in system
    assert "Hindi" in build_messages(STORIES["ev-metro"], "hi", cfg, None)[0]["content"]


def test_a_writer_task_of_a_new_language_uses_the_default_writer_tier_and_unknown_tasks_still_fail():
    from newsbot.llm import load_llm_settings
    s = load_llm_settings(TEMPLATE)
    llm = LLM({**s, "tasks": {**s["tasks"], "write_default": "strong"}}, {})
    assert llm.tier_of("write_hi") == "strong" and llm.tier_of("write_bn") == s["tasks"]["write_bn"]
    assert LLM(s, {}).tier_of("write_hi") == s["tasks"]["write_en"]
    try:
        LLM(s, {}).tier_of("bogus")
        assert False
    except LLMError:
        pass


def _site_copy(tmp: Path) -> Path:
    site = tmp / "site"
    shutil.copytree(SITE, site, ignore=shutil.ignore_patterns("public", "resources"))
    return site


def test_the_site_generator_builds_hugo_languages_labels_rtl_404_and_fallback_pages_for_added_languages():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        site, brain = _site_copy(tmp), tmp / "brain"
        shutil.copytree(TEMPLATE, brain)
        cfg = yaml.safe_load((brain / "config/newsbot.yaml").read_text(encoding="utf-8"))
        cfg["site"]["languages"] = NEW
        (brain / "config/newsbot.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        (brain / "content/hi").mkdir(parents=True, exist_ok=True)
        (brain / "content/hi/story-h.md").write_text("H")
        rc = gen.main(["--brain", str(brain), "--site", str(site), "--sync-content"])
        assert rc in (0, None)
        data = yaml.safe_load((site / "data/site.yaml").read_text(encoding="utf-8"))
        assert data["languages"] == NEW and data["rtl"] == ["ar"]
        assert set(data["ai_disclosure"]) >= set(NEW) and data["ai_disclosure"]["hi"] == L.LANGUAGES["hi"]["disclosure"]
        hugo = yaml.safe_load((site / "hugo.yaml").read_text(encoding="utf-8"))
        assert hugo["defaultContentLanguage"] == "bn" and hugo["defaultContentLanguageInSubdir"] is True
        assert list(hugo["languages"]) == ["bn", "en", "hi", "ar", "ja", "de", "ne"]
        assert hugo["languages"]["ar"] == {"languageName": "العربية", "languageCode": "ar", "weight": 4, "contentDir": "content/ar"}
        assert hugo["timeZone"] == "Asia/Dhaka" and "taxonomies" in hugo                          # the rest of the file is untouched
        keys = set(yaml.safe_load((SITE / "i18n/en.yaml").read_text(encoding="utf-8")))
        for lang in ("hi", "ar", "ja", "de", "ne"):
            labels = yaml.safe_load((site / "i18n" / f"{lang}.yaml").read_text(encoding="utf-8"))
            assert set(labels) == keys, lang
            assert labels["home"]["other"] == L.ui_strings(lang)["home"]
        assert yaml.safe_load((site / "i18n/ne.yaml").read_text(encoding="utf-8"))["home"]["other"] == "Home"      # no translation yet: English
        assert yaml.safe_load((site / "i18n/hi.yaml").read_text(encoding="utf-8"))["home"]["other"] == "मुखपृष्ठ"
        assert (site / "i18n/bn.yaml").read_text(encoding="utf-8") == (SITE / "i18n/bn.yaml").read_text(encoding="utf-8")   # shipped files stay
        nf = (site / "static/404.html").read_text(encoding="utf-8")
        assert all(f'href="/{l}/"' in nf for l in NEW) and "noindex" in nf
        assert (site / "content/hi/story-h.md").read_text() == "H"
        assert (site / "content/ja/pages/about.md").exists() and (site / "content/ar/pages").is_dir()               # English trust pages stand in
        assert (site / "content/ja/pages/about.md").read_text(encoding="utf-8") == (site / "content/en/pages/about.md").read_text(encoding="utf-8")
        # a hand-edited label file is never overwritten, a generated one is refreshed
        f = site / "i18n/hi.yaml"
        f.write_text("home: {other: \"मेरा घर\"}\n" + "\n".join(f"{k}: {{other: x}}" for k in keys if k != "home"), encoding="utf-8")
        gen.write_language_files(data, site)
        assert "मेरा घर" in f.read_text(encoding="utf-8")
        # removing a language again leaves a valid config
        data["languages"], data["rtl"] = ["bn", "en"], []
        gen.write_language_files(data, site)
        assert list(yaml.safe_load((site / "hugo.yaml").read_text(encoding="utf-8"))["languages"]) == ["bn", "en"]


def test_an_unknown_language_in_the_config_is_refused_with_a_clear_message():
    with tempfile.TemporaryDirectory() as t:
        brain = Path(t) / "brain"
        shutil.copytree(TEMPLATE, brain)
        cfg = yaml.safe_load((brain / "config/newsbot.yaml").read_text(encoding="utf-8"))
        cfg["site"]["languages"] = ["bn", "en", "xx"]
        (brain / "config/newsbot.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        try:
            gen.load_site_cfg(brain)
            assert False
        except ValueError as e:
            assert "xx" in str(e) and "supported" in str(e)


def test_the_default_hugo_yaml_is_what_the_generator_writes_for_bn_and_en():
    assert gen.hugo_languages_block(["bn", "en"], "bn") in (SITE / "hugo.yaml").read_text(encoding="utf-8")
    nodes = yaml.safe_load((SITE / "hugo.yaml").read_text(encoding="utf-8"))
    assert list(nodes["languages"]) == ["bn", "en"]


def test_layouts_mark_right_to_left_languages():
    base = (SITE / "layouts/_default/baseof.html").read_text(encoding="utf-8")
    assert 'dir="rtl"' in base and "site.Data.site.rtl" in base
    assert "[dir=rtl]" in (SITE / "assets/css/main.css").read_text(encoding="utf-8")
