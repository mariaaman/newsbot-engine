#!/usr/bin/env python3
"""Brain config -> Hugo site inputs. Stdlib + PyYAML only (no pydantic needed).

Writes site/data/site.yaml, site/static/ads.txt and site/static/robots.txt from <brain>/config/newsbot.yaml.
--sync-content also copies <brain>/content/<lang>/*.md (articles) and <brain>/content/<lang>/pages/*.md
(trust-page overrides) into site/content/, replacing the sample articles. Prints no config values except
what --print-base-url asks for (the engine repo is public: keep logs boring)."""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import yaml

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
    disclosure = {**DEFAULT_DISCLOSURE, **(site.get("ai_disclosure") or {})}
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
    return {
        "name": str(site.get("name") or "TBD"),
        "base_url": str(site.get("base_url") or "https://example.pages.dev").rstrip("/") + "/",
        "languages": langs, "default": default,
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


def sync_content(brain: Path, site: Path) -> dict:
    """Replace site articles with the brain's; brain trust pages override the shipped defaults."""
    counts = {}
    for lang in ("bn", "en"):
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
    return counts


def unfilled_placeholders(site: Path) -> list[str]:
    return sorted(f"{p.relative_to(site)}" for p in (site / "content").rglob("*.md")
                  if "[OWNER:" in p.read_text(encoding="utf-8"))


def write_site_files(data: dict, site: Path) -> None:
    (site / "data").mkdir(parents=True, exist_ok=True)
    (site / "static").mkdir(parents=True, exist_ok=True)
    (site / "data" / "site.yaml").write_text(
        "# GENERATED by scripts/gen_site_config.py from the brain config. Do not edit.\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")
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
    msg = f"site config written ads={'on' if data['ads']['enabled'] else 'off'}"
    if a.sync_content:
        msg += f" synced={sync_content(Path(a.brain), site)}"
    left = unfilled_placeholders(site)
    if left:
        msg += f" WARNING owner placeholders remain in {len(left)} page(s)"
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
