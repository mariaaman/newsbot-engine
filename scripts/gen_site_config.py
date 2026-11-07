#!/usr/bin/env python3
"""Brain config -> Hugo site inputs. Stdlib + PyYAML only (no pydantic needed).

Writes site/data/site.yaml, site/static/ads.txt and site/static/robots.txt from <brain>/config/newsbot.yaml.
--sync-content also copies <brain>/content/<lang>/*.md (articles) and <brain>/content/<lang>/pages/*.md
(trust-page overrides) into site/content/, replacing the sample articles. Prints no config values except
what --print-base-url asks for (the engine repo is public: keep logs boring)."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from newsbot import langs as LG  # noqa: E402  (stdlib only: the output-language catalog)

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
DEFAULT_DISCLOSURE = {
    "bn": "এই নিবন্ধটি এআই-সহায়তায় তৈরি এবং আমাদের সম্পাদকীয় নীতি মেনে চলে।",
    "en": "This article is AI-assisted and follows our editorial policy.",
}
DEFAULT_CATEGORIES = ["bangladesh", "world", "politics", "economy", "technology", "sports", "science",
                      "entertainment", "environment", "education"]
PUB_RE = re.compile(r"^(?:ca-)?(pub-\d{10,20})$")
GOOGLE_CERT_ID = "f08c47fec0942fa0"   # VERIFY: Google's TAG ID in ads.txt lines (stable for years)
SLOTS = ("after_summary", "mid_article", "end_article")
NETWORKS = ("adsense", "adsterra")
SCRIPT_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s\"'<>]*)?$")
TXT_LINE_RE = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}, ?[A-Za-z0-9_.-]+, ?(?:DIRECT|RESELLER)(?:, ?[0-9a-fA-F]{6,32})?$")
MAX_SNIPPET = 4000
_PROTO_REL_SRC = re.compile(r"""(src\s*=\s*["'])//""", re.I)


def _https(url: str) -> str:
    """Ad networks hand out protocol-relative URLs (//host/x.js); the site is https only."""
    url = url.strip()
    return "https:" + url if url.startswith("//") else url


def _ad_snippet(raw: str) -> str:
    """An ad-unit snippet pasted from a network dashboard: http(s) only, no way to break out of its wrapper."""
    snippet = _PROTO_REL_SRC.sub(r"\1https://", str(raw).strip())
    low = snippet.lower()
    if len(snippet) > MAX_SNIPPET or "</template" in low or "<!--" in snippet:
        raise ValueError("an ads.slots snippet is too long or contains </template or an HTML comment")
    if re.search(r"""src\s*=\s*["']http://""", low):
        raise ValueError("ads.slots snippets must load scripts over https")
    return snippet


def load_site_cfg(brain: Path) -> dict:
    path = Path(brain) / "config" / "newsbot.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    site, ads = raw.get("site") or {}, raw.get("ads") or {}
    langs = list(site.get("languages") or ["bn", "en"])
    default = site.get("default") or langs[0]
    if default not in langs:
        raise ValueError("site.default must be one of site.languages")
    unknown = [l for l in langs if l not in LG.LANGUAGES]
    if unknown:
        raise ValueError(f"site.languages has unsupported language(s) {unknown}; supported: {sorted(LG.LANGUAGES)}")
    disclosure = {**{l: LG.meta(l)["disclosure"] for l in langs}, **DEFAULT_DISCLOSURE, **(site.get("ai_disclosure") or {})}
    pub = str(ads.get("publisher_id") or "").strip()
    enabled = bool(ads.get("enabled"))
    network = str(ads.get("network") or "adsense").strip().lower()
    if network not in NETWORKS:
        raise ValueError(f"ads.network must be one of {', '.join(NETWORKS)}")
    scripts: list[str] = []
    txt_lines: list[str] = []
    if network == "adsense":
        if enabled:
            m = PUB_RE.match(pub)
            if not m:
                raise ValueError("ads.enabled is true but ads.publisher_id is missing or not like pub-1234567890123456")
            pub = m.group(1)
        slots = {k: str(v) for k, v in (ads.get("slots") or {}).items() if k in SLOTS and str(v).strip().isdigit()}
    else:                                                  # adsterra (or any network that gives you script/snippet code)
        pub = ""
        for u in ads.get("scripts") or []:
            u = _https(str(u))
            if not SCRIPT_URL_RE.match(u):
                raise ValueError("every ads.scripts entry must be an https URL")
            scripts.append(u)
        slots = {k: _ad_snippet(v) for k, v in (ads.get("slots") or {}).items() if k in SLOTS and str(v).strip()}
        for line in ads.get("txt_lines") or []:
            line = str(line).strip()
            if not TXT_LINE_RE.match(line):
                raise ValueError("ads.txt_lines entries look like: adsterra.com, 1234567, DIRECT, <id from your dashboard>")
            txt_lines.append(line)
        if enabled and not (scripts or slots):
            raise ValueError("ads.enabled is true but ads.scripts and ads.slots are both empty: paste your ad code first")
    sites = {}
    raw_sites = dict(site.get("sites") or {})
    if site.get("auto_sites") and not raw_sites:               # same rule as newsbot.config.SiteCfg
        pre = str(site["auto_sites"]).strip().lower()
        raw_sites = {l: {"base_url": f"https://{pre}-{l}.pages.dev", "pages_project": f"{pre}-{l}"} for l in langs}
    for l, t in raw_sites.items():
        sites[str(l)] = {"base_url": str((t or {}).get("base_url") or "").rstrip("/") + "/",
                         "pages_project": str((t or {}).get("pages_project") or ""),
                         "analytics_token": str((t or {}).get("analytics_token") or "")}
    if sites and set(sites) != set(langs):
        raise ValueError("site.sites needs exactly one entry per language in site.languages")
    return {
        "sites": sites, "separate": bool(sites), "analytics_token": str(site.get("analytics_token") or ""),
        "name": str(site.get("name") or "TBD"),
        "base_url": str(site.get("base_url") or "https://example.pages.dev").rstrip("/") + "/",
        "languages": langs, "default": default, "rtl": [l for l in langs if LG.meta(l)["rtl"]],
        "categories": list(site.get("categories") or DEFAULT_CATEGORIES),
        "bengali_font": bool(site.get("bengali_font", False)),
        "ai_disclosure": disclosure,
        "ads": {"enabled": enabled, "network": network, "publisher_id": pub if enabled else "",
                "consent_required": bool(ads.get("consent_required", False)), "slots": slots,
                "scripts": scripts if enabled else [], "txt_lines": txt_lines},
    }


def ads_txt(data: dict) -> str:
    ads = data["ads"]
    if not ads["enabled"]:
        return "# ads.txt: ads are disabled (ads.enabled=false in the brain config).\n"
    if ads.get("network", "adsense") == "adsterra":
        if not ads.get("txt_lines"):
            return "# ads.txt: add the line(s) from your ad network dashboard to ads.txt_lines in the brain config.\n"
        return "\n".join(ads["txt_lines"]) + "\n"
    return f"google.com, {ads['publisher_id']}, DIRECT, {GOOGLE_CERT_ID}\n"


def robots_txt(data: dict) -> str:
    return f"User-agent: *\nAllow: /\n\nSitemap: {data['base_url']}sitemap.xml\n"


SHIPPED_LANGS = ("bn", "en")      # languages whose trust pages and UI labels ship with the engine


def sync_content(brain: Path, site: Path, languages=SHIPPED_LANGS) -> dict:
    """Replace site articles with the brain's; brain trust pages override the shipped defaults. A language added later has
    no shipped trust pages: until the brain has its own (content/<lang>/pages/), the English pages stand in for it."""
    counts = {}
    for lang in languages:
        dest = site / "content" / lang
        dest.mkdir(parents=True, exist_ok=True)
        for old in dest.glob("*.md"):
            old.unlink()                      # sample or stale articles (never touches pages/)
        src = Path(brain) / "content" / lang
        n = 0
        for f in sorted(src.glob("*.md")) if src.is_dir() else []:
            shutil.copy2(f, dest / f.name)
            n += 1
        if (src / "pages").is_dir():
            (dest / "pages").mkdir(exist_ok=True)
            for f in sorted((src / "pages").glob("*.md")):
                shutil.copy2(f, dest / "pages" / f.name)
        counts[lang] = n
    english = site / "content" / "en" / "pages"
    for lang in languages:
        if lang in SHIPPED_LANGS or not english.is_dir():
            continue
        dest = site / "content" / lang / "pages"
        dest.mkdir(parents=True, exist_ok=True)
        for f in sorted(english.glob("*.md")):
            if not (dest / f.name).exists():
                shutil.copy2(f, dest / f.name)
    return counts


HUGO_BEGIN = "# BEGIN GENERATED LANGUAGES"
HUGO_END = "# END GENERATED LANGUAGES"


def hugo_languages_block(langs: list[str], default: str, subdir: bool = True) -> str:
    """The language part of hugo.yaml: every site language, the default one first."""
    order = [default] + [l for l in langs if l != default]
    lines = [f"{HUGO_BEGIN} (scripts/gen_site_config.py rewrites this block from site.languages in newsbot.yaml: edit that, not this)",
             f"defaultContentLanguage: {default}",
             "defaultContentLanguageInSubdir: true    # /bn/ and /en/ ...; the root only redirects (and serves static/404.html)" if subdir
             else "defaultContentLanguageInSubdir: false   # one-language site (site.sites): served at its root",
             "disableDefaultLanguageRedirect: false", "languages:"]
    for weight, l in enumerate(order, 1):
        m = LG.meta(l)
        lines += [f"  {l}:", f"    languageName: {json.dumps(m['native'], ensure_ascii=False)}", f"    languageCode: {m['locale']}",
                  f"    weight: {weight}", f"    contentDir: content/{l}"]
    lines.append(HUGO_END)
    return "\n".join(lines)


def i18n_yaml(lang: str) -> str:
    rows = [f"{k}: {{other: {json.dumps(v, ensure_ascii=False)}}}" for k, v in LG.ui_strings(lang).items()]
    return "# GENERATED by scripts/gen_site_config.py from newsbot/langs.py (English labels where the language has no translation yet).\n" \
           "# To use your own wording: delete this line and edit the file.\n" + "\n".join(rows) + "\n"


def not_found_page(langs: list[str], sites: dict | None = None) -> str:
    links = " &middot; ".join(f'<a href="{(sites or {}).get(l, {}).get("url", "/" + l + "/")}">{LG.meta(l)["native"]}</a>' for l in (sites or langs))
    return ('<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex"><title>404</title>\n'
            '<style>body{font:1.1rem/1.6 system-ui,"Noto Sans Bengali","Nirmala UI",sans-serif;max-width:36rem;margin:4rem auto;padding:0 1rem}</style></head>\n'
            f'<body><h1>404</h1><p>Page not found. পাতাটি পাওয়া যায়নি।</p>\n<p>{links}</p></body></html>\n')


def write_language_files(data: dict, site: Path) -> list[str]:
    """hugo.yaml languages, a UI-label file per added language, the root 404 page. Returns what changed (names only)."""
    changed = []
    hugo = site / "hugo.yaml"
    if hugo.exists():
        text = hugo.read_text(encoding="utf-8")
        new = re.sub(re.escape(HUGO_BEGIN) + r".*?" + re.escape(HUGO_END), lambda _: hugo_languages_block(data["languages"], data["default"], not data.get("separate")),
                     text, count=1, flags=re.S)
        if new == text and HUGO_BEGIN not in text:
            raise ValueError("site/hugo.yaml has no GENERATED LANGUAGES block")
        if new != text:
            hugo.write_text(new, encoding="utf-8")
            changed.append("hugo.yaml")
    (site / "i18n").mkdir(parents=True, exist_ok=True)
    for lang in data["languages"]:
        f = site / "i18n" / f"{lang}.yaml"
        if f.exists() and not f.read_text(encoding="utf-8").startswith("# GENERATED"):
            continue                                   # a shipped or hand-edited file is never overwritten
        text = i18n_yaml(lang)
        if not f.exists() or f.read_text(encoding="utf-8") != text:
            f.write_text(text, encoding="utf-8")
            changed.append(f"i18n/{lang}.yaml")
    (site / "static").mkdir(parents=True, exist_ok=True)
    page = not_found_page(data["languages"], data.get("sites_public"))
    nf = site / "static" / "404.html"
    if not nf.exists() or nf.read_text(encoding="utf-8") != page:
        nf.write_text(page, encoding="utf-8")
        changed.append("404.html")
    return changed


def unfilled_placeholders(site: Path) -> list[str]:
    return sorted(f"{p.relative_to(site)}" for p in (site / "content").rglob("*.md")
                  if "[OWNER:" in p.read_text(encoding="utf-8"))


def target_data(data: dict, lang: str) -> dict:
    """The data for ONE separate website: only its language, its own address, and the other sites for the language switcher."""
    t = data["sites"][lang]
    d = dict(data)
    d.update({"languages": [lang], "default": lang, "rtl": [lang] if LG.meta(lang)["rtl"] else [], "base_url": t["base_url"],
              "analytics_token": t["analytics_token"], "separate": True,
              "sites_public": {l: {"url": x["base_url"], "name": LG.meta(l)["native"]} for l, x in data["sites"].items()}})
    return d


def alts_map(brain: Path, data: dict) -> dict:
    """translationKey -> {lang: url of that language's article}, from the published articles in the brain (separate sites only)."""
    out: dict[str, dict[str, str]] = {}
    for lang, t in data["sites"].items():
        for f in sorted((Path(brain) / "content" / lang).glob("*.md")):
            try:
                head = f.read_text(encoding="utf-8").split("---", 2)
                fm = yaml.safe_load(head[1]) if len(head) >= 3 else {}
            except (OSError, yaml.YAMLError):
                continue
            key, slug = (fm or {}).get("translationKey"), (fm or {}).get("slug") or f.stem
            if key:
                out.setdefault(str(key), {})[lang] = f"{t['base_url']}{slug}/"
    return {k: v for k, v in out.items() if len(v) > 1}


def write_site_files(data: dict, site: Path) -> None:
    (site / "data").mkdir(parents=True, exist_ok=True)
    (site / "static").mkdir(parents=True, exist_ok=True)
    public = {k: v for k, v in data.items() if k not in ("sites", "sites_public")}
    public["sites"] = data.get("sites_public") or {}        # the language switcher; pages_project names stay out of the site
    (site / "data" / "site.yaml").write_text(
        "# GENERATED by scripts/gen_site_config.py from the brain config. Do not edit.\n"
        + yaml.safe_dump(public, allow_unicode=True, sort_keys=True), encoding="utf-8")
    (site / "static" / "ads.txt").write_text(ads_txt(data), encoding="utf-8")
    (site / "static" / "robots.txt").write_text(robots_txt(data), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", required=True)
    ap.add_argument("--site", default=str(SITE_DIR))
    ap.add_argument("--sync-content", action="store_true")
    ap.add_argument("--print-base-url", action="store_true", help="print only the base URL (for hugo --baseURL)")
    a = ap.parse_args(argv)
    data = load_site_cfg(Path(a.brain))
    if a.print_base_url:
        print(data["base_url"])
        return 0
    site = Path(a.site)
    write_site_files(data, site)
    write_language_files(data, site)
    msg = f"site config written ads={'on' if data['ads']['enabled'] else 'off'} languages={','.join(data['languages'])}"
    if a.sync_content:
        msg += f" synced={sync_content(Path(a.brain), site, data['languages'])}"
    left = unfilled_placeholders(site)
    if left:
        msg += f" WARNING owner placeholders remain in {len(left)} page(s)"
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
