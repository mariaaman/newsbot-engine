"""M5 site tests. Hugo is not required: these lint the layouts/i18n/content contract and test the two scripts.
The real `hugo --minify` build runs in CI (ci.yml)."""
import importlib.util
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
LAYOUTS = SITE / "layouts"
TEMPLATE = ROOT / "brain-template"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = load_script("gen_site_config")
cfc = load_script("check_file_count")


def front(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    return yaml.safe_load(text.split("---\n", 2)[1])


def brain_with(config_extra: str, tmp: Path) -> Path:
    (tmp / "config").mkdir(parents=True)
    base = (TEMPLATE / "config" / "newsbot.yaml").read_text(encoding="utf-8")
    base = re.sub(r"\nads:.*", "", base, flags=re.S)       # ads section is last in the template
    (tmp / "config" / "newsbot.yaml").write_text(base + "\n" + config_extra, encoding="utf-8")
    return tmp


# ---------- gen_site_config ----------
def test_gen_reads_template_defaults():
    d = gen.load_site_cfg(TEMPLATE)
    assert d["languages"] == ["bn", "en"] and d["default"] == "bn"
    assert d["ads"] == {"enabled": False, "network": "adsense", "publisher_id": "", "consent_required": True, "slots": {},
                        "scripts": [], "txt_lines": []}
    assert d["ai_disclosure"] == gen.DEFAULT_DISCLOSURE and d["base_url"].endswith("/")


def test_ads_txt_disabled_has_no_publisher_line():
    with tempfile.TemporaryDirectory() as t:
        b = brain_with("ads: {enabled: false, publisher_id: 'pub-1234567890123456'}\n", Path(t))
        d = gen.load_site_cfg(b)
        assert d["ads"]["publisher_id"] == "" and "google.com" not in gen.ads_txt(d)


def test_ads_txt_enabled_strips_ca_prefix():
    with tempfile.TemporaryDirectory() as t:
        b = brain_with("ads: {enabled: true, publisher_id: 'ca-pub-1234567890123456', slots: {mid_article: '42', bogus: '1', end_article: 'x'}}\n", Path(t))
        d = gen.load_site_cfg(b)
        assert gen.ads_txt(d).startswith("google.com, pub-1234567890123456, DIRECT, ")
        assert d["ads"]["slots"] == {"mid_article": "42"}      # unknown slot names and non-numeric ids dropped


def test_ads_enabled_without_valid_id_fails_loudly():
    for pub in ("''", "'abc'", "'pub-12'"):
        with tempfile.TemporaryDirectory() as t:
            b = brain_with(f"ads: {{enabled: true, publisher_id: {pub}}}\n", Path(t))
            try:
                gen.load_site_cfg(b)
            except ValueError:
                continue
            raise AssertionError(f"accepted bad publisher id {pub}")


def test_write_site_files_and_robots():
    with tempfile.TemporaryDirectory() as t:
        site = Path(t)
        d = gen.load_site_cfg(TEMPLATE)
        gen.write_site_files(d, site)
        assert yaml.safe_load((site / "data/site.yaml").read_text(encoding="utf-8"))["name"] == d["name"]
        assert "Sitemap: https://example.pages.dev/sitemap.xml" in (site / "static/robots.txt").read_text()
        assert (site / "static/ads.txt").read_text().startswith("#")


def test_committed_generated_files_match_template():
    d = gen.load_site_cfg(TEMPLATE)
    assert yaml.safe_load((SITE / "data/site.yaml").read_text(encoding="utf-8")) == d
    assert (SITE / "static/ads.txt").read_text() == gen.ads_txt(d)
    assert (SITE / "static/robots.txt").read_text() == gen.robots_txt(d)


def test_sync_content_replaces_samples_keeps_default_pages_and_overrides():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        site, brain = tmp / "site", tmp / "brain"
        for lang in ("bn", "en"):
            (site / "content" / lang / "pages").mkdir(parents=True)
            (site / "content" / lang / "sample.md").write_text("old")
            (site / "content" / lang / "pages" / "about.md").write_text("default about")
            (site / "content" / lang / "pages" / "terms.md").write_text("default terms")
        (brain / "content/en/pages").mkdir(parents=True)
        (brain / "content/bn").mkdir(parents=True)
        (brain / "content/en/story-a.md").write_text("A")
        (brain / "content/en/pages/about.md").write_text("owner about")
        counts = gen.sync_content(brain, site)
        assert counts == {"bn": 0, "en": 1}
        assert not (site / "content/en/sample.md").exists() and not (site / "content/bn/sample.md").exists()
        assert (site / "content/en/story-a.md").read_text() == "A"
        assert (site / "content/en/pages/about.md").read_text() == "owner about"
        assert (site / "content/en/pages/terms.md").read_text() == "default terms"
        assert (site / "content/bn/pages/about.md").read_text() == "default about"


def test_placeholder_warning_detects_owner_markers():
    left = gen.unfilled_placeholders(SITE)
    assert any(p.endswith("about.md") for p in left)          # shipped trust pages still need the owner


# ---------- check_file_count ----------
def _make_files(root: Path, n: int):
    for i in range(n):
        (root / f"f{i}.html").write_text("x")


def test_file_count_thresholds():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        _make_files(root, 5)
        assert cfc.check(root, warn=10, fail=20)[:2] == ("ok", 5)
        assert cfc.check(root, warn=4, fail=20)[:2] == ("warn", 5)
        assert cfc.check(root, warn=2, fail=4)[:2] == ("fail", 5)


def test_file_count_exit_codes_and_missing_dir():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        _make_files(root, 5)
        assert cfc.main([str(root), "--warn", "4", "--fail", "20"]) == 0     # warn does not block the deploy
        assert cfc.main([str(root), "--warn", "2", "--fail", "4"]) == 1
        try:
            cfc.check(root / "nope")
        except FileNotFoundError:
            return
        raise AssertionError("missing build dir must raise")


def test_file_count_counts_nested_files_and_default_limits():
    assert (cfc.WARN, cfc.FAIL) == (12000, 16000)
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        (root / "a/b").mkdir(parents=True)
        (root / "a/b/x.html").write_text("x")
        (root / "y.html").write_text("y")
        assert cfc.check(root)[1] == 2


# ---------- content contract ----------
def test_sample_front_matter_matches_writer_contract():
    from newsbot.write import front_matter
    art = {"title": "t", "summary": "s", "slug": "x", "category": "world", "timeline": [], "confirmed": [],
           "unclear": [], "disputed": [], "sources": []}
    expected = set(front_matter(art, SimpleNamespace(story_id="st-1", version=1), "en"))
    for f in sorted((SITE / "content").glob("*/*.md")):
        assert set(front(f)) == expected, f.name


def test_sample_articles_are_consistent():
    cats = set(gen.load_site_cfg(TEMPLATE)["categories"])
    arts = {lang: {f.stem: front(f) for f in (SITE / "content" / lang).glob("*.md")} for lang in ("bn", "en")}
    keys = {lang: {a["translationKey"] for a in v.values()} for lang, v in arts.items()}
    assert keys["bn"] & keys["en"], "need one story in both languages (hreflang acceptance)"
    assert keys["en"] - keys["bn"], "need one en-only story (no hreflang)"
    for lang, v in arts.items():
        for stem, fm in v.items():
            assert fm["lang"] == lang and fm["slug"] == stem and fm["story_id"] == fm["translationKey"]
            assert fm["categories"][0] in cats and fm["ai_assisted"] is True
            ids = {s["id"] for s in fm["sources"]}
            body = (SITE / "content" / lang / f"{stem}.md").read_text(encoding="utf-8").split("---\n", 2)[2]
            assert body.count("\n## ") + body.startswith("## ") == 2        # exactly two H2 sections
            for m in re.findall(r"\[(S\d+)\]", body):
                assert m in ids, (stem, m)
            assert all(re.match(r"\d{4}-\d\d-\d\dT", e["time"]) for e in fm["timeline"])


def test_trust_pages_exist_in_both_languages():
    names = {"about", "contact", "privacy", "editorial", "corrections", "ads", "terms"}
    for lang in ("bn", "en"):
        got = {f.stem for f in (SITE / "content" / lang / "pages").glob("*.md")}
        assert got == names, lang
        for f in (SITE / "content" / lang / "pages").glob("*.md"):
            fm = front(f)
            assert fm["type"] == "policy" and fm["title"] and fm["translationKey"] == f"page-{f.stem}"


# ---------- layouts / i18n lint (Hugo is not available here) ----------
def _templates():
    return sorted(p for p in LAYOUTS.rglob("*") if p.is_file())


def test_template_blocks_balanced():
    opener = re.compile(r"\{\{-?\s*(?:if|range|with|define|block)\b")
    closer = re.compile(r"\{\{-?\s*end\b")
    for p in _templates():
        t = p.read_text(encoding="utf-8")
        assert len(opener.findall(t)) == len(closer.findall(t)), p.name
        assert t.count("{{") == t.count("}}"), p.name


def test_partials_referenced_exist():
    have = {p.name for p in (LAYOUTS / "partials").glob("*.html")}
    for p in _templates():
        for name in re.findall(r'partial "([^"]+)"', p.read_text(encoding="utf-8")):
            assert name in have, (p.name, name)


def test_i18n_keys_complete_and_symmetric():
    bn = yaml.safe_load((SITE / "i18n/bn.yaml").read_text(encoding="utf-8"))
    en = yaml.safe_load((SITE / "i18n/en.yaml").read_text(encoding="utf-8"))
    assert set(bn) == set(en)
    assert all(v["other"].strip() for v in list(bn.values()) + list(en.values()))
    used = {k for p in _templates() for k in re.findall(r'i18n "([a-z_]+)"', p.read_text(encoding="utf-8"))}
    assert used <= set(bn), used - set(bn)
    for c in gen.load_site_cfg(TEMPLATE)["categories"]:
        assert f"cat_{c}" in bn and f"cat_{c}" in en, c


def test_untrusted_params_never_marked_safe():
    for p in _templates():
        for action in re.findall(r"\{\{.*?\}\}", p.read_text(encoding="utf-8"), flags=re.S):
            assert not (".Params" in action and re.search(r"safeHTML|safeJS|safeURL", action)), (p.name, action)


def test_cite_partial_escapes_before_marking_safe():
    t = (LAYOUTS / "partials/cite.html").read_text(encoding="utf-8")
    assert t.index("htmlEscape") < t.index("safeHTML")


def test_article_layout_has_required_trust_blocks():
    t = (LAYOUTS / "_default/single.html").read_text(encoding="utf-8")
    for needle in ('i18n "summary"', 'i18n "updated"', 'partial "timeline.html"', 'i18n "source_analysis"', '"confirmed"',
                   '"unclear"', '"disputed"', 'id="source-{{ .id }}"', 'i18n "corrections"', "ai_disclosure",
                   '"after_summary"', '"mid_article"', '"end_article"', "#source-${1}"):
        assert needle in t, needle


def test_jsonld_newsarticle_fields():
    t = (LAYOUTS / "partials/jsonld.html").read_text(encoding="utf-8")
    for needle in ("NewsArticle", '"headline"', '"datePublished"', '"dateModified"', '"author"', '"publisher"',
                   "Organization", "application/ld+json"):
        assert needle in t, needle


def test_head_has_canonical_and_conditional_hreflang():
    t = (LAYOUTS / "partials/head.html").read_text(encoding="utf-8")
    assert 'rel="canonical"' in t
    block = t[t.index("if .Translations"):]
    assert block.index('hreflang="{{ .Language.Lang }}"') < block.index("range .Translations")   # self link first
    assert t.count('<link rel="alternate" hreflang=') == 2 and "x-default" not in t


def test_no_ads_on_policy_pages():
    assert 'ne .Type "policy"' in (LAYOUTS / "partials/adson.html").read_text(encoding="utf-8")
    pol = (LAYOUTS / "policy/single.html").read_text(encoding="utf-8")
    assert "adslot" not in pol and "adsbygoogle" not in pol
    for part in ("adhead.html", "adslot.html", "consent.html"):
        assert 'partial "adson.html"' in (LAYOUTS / "partials" / part).read_text(encoding="utf-8"), part


def test_consent_hook_gates_ad_script():
    head = (LAYOUTS / "partials/adhead.html").read_text(encoding="utf-8")
    assert "not $d.ads.consent_required" in head
    consent = (LAYOUTS / "partials/consent.html").read_text(encoding="utf-8")
    assert "$d.ads.consent_required" in consent and "adsbygoogle.js" in consent


def test_hugo_config_file_budget_rules():
    cfg = yaml.safe_load((SITE / "hugo.yaml").read_text(encoding="utf-8"))
    assert cfg["defaultContentLanguage"] == "bn" and cfg["defaultContentLanguageInSubdir"] is True
    assert set(cfg["languages"]) == {"bn", "en"}
    assert cfg["taxonomies"] == {"category": "categories"}                 # categories only: no tags
    assert "section" in cfg["disableKinds"]
    assert cfg["pagination"]["pagerSize"] == 20
    assert set(cfg["outputs"]) == {"home", "term", "taxonomy", "page"}
    assert cfg["outputs"]["page"] == ["HTML"] and cfg["outputs"]["term"] == ["HTML", "RSS"]
    assert cfg["markup"]["goldmark"]["renderer"]["unsafe"] is False
    assert cfg["enableRobotsTXT"] is False


def test_static_files_for_pages_hosting():
    assert "404" in (SITE / "static/404.html").read_text(encoding="utf-8")     # root 404 (Pages would act as an SPA otherwise)
    assert "noindex" in (SITE / "static/404.html").read_text(encoding="utf-8")
    assert (SITE / "static/_headers").is_file() and (SITE / "assets/css/main.css").is_file()


# ---------- ad network: adsterra (script URLs + ad-unit snippets from the dashboard) ----------
ADSTERRA = """ads:
  enabled: true
  network: adsterra
  consent_required: true
  scripts: ["//pl000.example.com/abc/social.js"]
  txt_lines: ["adsterra.com, 1234567, DIRECT, abcdef0123456789"]
  slots:
    mid_article: |
      <script async data-cfasync="false" src="//pl000.example.com/abc/invoke.js"></script>
      <div id="container-abc"></div>
    bogus: "<b>dropped</b>"
"""


def _adsterra(extra=ADSTERRA):
    with tempfile.TemporaryDirectory() as t:
        return gen.load_site_cfg(brain_with(extra, Path(t)))


def test_adsterra_config_is_cleaned_and_makes_its_own_ads_txt():
    d = _adsterra()
    ads = d["ads"]
    assert ads["network"] == "adsterra" and ads["publisher_id"] == "" and ads["consent_required"] is True
    assert ads["scripts"] == ["https://pl000.example.com/abc/social.js"]                  # protocol-relative -> https
    assert set(ads["slots"]) == {"mid_article"}                                             # unknown slot dropped
    assert 'src="https://pl000.example.com/abc/invoke.js"' in ads["slots"]["mid_article"]
    assert gen.ads_txt(d) == "adsterra.com, 1234567, DIRECT, abcdef0123456789\n" and "google.com" not in gen.ads_txt(d)


def test_adsterra_without_ad_code_or_with_unsafe_code_fails_loudly():
    bad = ["ads: {enabled: true, network: adsterra}\n",
           "ads: {enabled: true, network: adsterra, scripts: ['http://x.example/a.js']}\n",
           "ads: {enabled: true, network: adsterra, scripts: ['https://x.example/a.js\"onload=1']}\n",
           "ads: {enabled: true, network: adsterra, slots: {mid_article: '<div></template><script>x</script>'}}\n",
           "ads: {enabled: true, network: adsterra, slots: {mid_article: '<script src=\"http://x.example/a.js\"></script>'}}\n",
           "ads: {enabled: true, network: adsterra, scripts: ['https://x.example/a.js'], txt_lines: ['not a line']}\n",
           "ads: {enabled: true, network: nope}\n"]
    for extra in bad:
        try:
            _adsterra(extra)
        except ValueError:
            continue
        raise AssertionError(f"accepted: {extra}")


def test_adsterra_disabled_or_missing_txt_line_is_harmless():
    d = _adsterra("ads: {enabled: false, network: adsterra, scripts: ['https://x.example/a.js']}\n")
    assert d["ads"]["scripts"] == [] and gen.ads_txt(d).startswith("# ads.txt: ads are disabled")
    d = _adsterra("ads: {enabled: true, network: adsterra, scripts: ['https://x.example/a.js']}\n")
    assert gen.ads_txt(d).startswith("# ads.txt: add the line(s)") and "google.com" not in gen.ads_txt(d)


def test_adsense_path_is_unchanged_next_to_adsterra():
    d = _adsterra("ads: {enabled: true, publisher_id: 'pub-1234567890123456', slots: {mid_article: '42'}}\n")
    assert d["ads"]["network"] == "adsense" and d["ads"]["scripts"] == [] and d["ads"]["slots"] == {"mid_article": "42"}
    assert gen.ads_txt(d).startswith("google.com, pub-1234567890123456, DIRECT, ")


def test_ad_templates_branch_on_network_and_gate_snippets_behind_consent():
    head = (LAYOUTS / "partials/adhead.html").read_text(encoding="utf-8")
    slot = (LAYOUTS / "partials/adslot.html").read_text(encoding="utf-8")
    consent = (LAYOUTS / "partials/consent.html").read_text(encoding="utf-8")
    for t in (head, slot, consent):
        assert 'eq $d.ads.network "adsterra"' in t
    assert "range $d.ads.scripts" in head and "adsbygoogle.js" in head                      # adsense script still there
    assert '<template class="ad-snippet">' in slot and "safeHTML" in slot and "adsbygoogle" in slot
    assert 'querySelectorAll("template.ad-snippet")' in consent and "n.async=false" in consent
    assert "}}" not in consent.replace("}}", "", consent.count("{{"))                        # no stray JS braces read as template end
