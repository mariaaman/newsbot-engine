"""A free, correctly licensed lead photo for an article, found on Wikimedia Commons. LINK ONLY: nothing is downloaded or stored.

Why Commons and nowhere else: every file there carries a machine-readable licence and author, and Wikimedia allows
showing its images straight from upload.wikimedia.org. Photos of other news sites / agencies are NOT used: crediting
the owner does not replace their permission.

Rules (all must hold, else no photo and the page shows its plain banner):
  * the story is not sensitive (death, crime, minors, ...): a wrong or tasteless picture there does real harm;
  * a proper name (two capitalised words, or an acronym) is found in the title, and the file's Commons title or
    description contains that WHOLE name (so "Joe Biden" never returns some other Joe);
  * the licence is CC0, public domain, CC BY or CC BY-SA, the file is a JPEG/PNG at least MIN_WIDTH wide,
    and the author and licence link are known (they are shown under the picture).
Never raises: any network or parsing problem simply means "no photo"."""
from __future__ import annotations

import html
import logging
import re

log = logging.getLogger("newsbot.images")

API = "https://commons.wikimedia.org/w/api.php"
MIN_WIDTH = 800
THUMB_WIDTH = 1200
_OK_LICENCE = re.compile(r"^(cc0|public domain|pd\b|pdm|cc[ -]by(?:[ -]sa)?\b)", re.I)
_NAME = re.compile(r"\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+)+|[A-Z]{2,6})\b")
_STOP = {"The", "This", "That", "New", "Live", "Update", "Breaking", "News"}


def candidate_names(title: str, limit: int = 2) -> list[str]:
    """Proper names in an (English) title, longest first."""
    seen: list[str] = []
    for m in _NAME.finditer(title or ""):
        n = m.group(1)
        if n.split()[0] in _STOP or n in seen:
            continue
        seen.append(n)
    return sorted(seen, key=lambda s: (-len(s.split()), -len(s)))[:limit]


def _plain(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _meta(info: dict, key: str) -> str:
    return _plain(((info.get("extmetadata") or {}).get(key) or {}).get("value", ""))


def pick(pages: dict, name: str) -> dict | None:
    """Best acceptable file among API 'pages' for this name, or None."""
    low, best = name.lower(), None
    for p in sorted(pages.values(), key=lambda p: p.get("index", 99)):
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png") or int(info.get("width") or 0) < MIN_WIDTH:
            continue
        lic = _meta(info, "LicenseShortName")
        if not _OK_LICENCE.match(lic) or re.search(r"\b(nc|nd)\b", lic, re.I) or _meta(info, "NonFree") or _meta(info, "Restrictions"):
            continue
        hay = (p.get("title", "") + " " + _meta(info, "ObjectName") + " " + _meta(info, "ImageDescription")).lower()
        if low not in hay.replace("_", " "):
            continue
        author = _meta(info, "Artist") or _meta(info, "Credit")
        if not author or len(author) > 120 or not (info.get("thumburl") or info.get("url")):
            continue
        best = {
            "url": info.get("thumburl") or info["url"],
            "width": int(info.get("thumbwidth") or info.get("width")),
            "height": int(info.get("thumbheight") or info.get("height")),
            "alt": _meta(info, "ObjectName") or name,
            "credit": author,
            "license": lic,
            "license_url": _meta(info, "LicenseUrl"),
            "page": info.get("descriptionurl", ""),
        }
        break
    return best


def search(name: str, get) -> dict | None:
    r = get(API, {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": 12,
                  "gsrsearch": f'"{name}" filetype:bitmap', "prop": "imageinfo",
                  "iiprop": "url|size|mime|extmetadata", "iiurlwidth": THUMB_WIDTH,
                  "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|Credit|ObjectName|ImageDescription|NonFree|Restrictions"})
    return pick((r.get("query") or {}).get("pages") or {}, name)


def _http_get(url: str, params: dict) -> dict:
    from newsbot.netsafe import safe_get
    import json
    from urllib.parse import urlencode
    status, _ctype, body = safe_get(f"{url}?{urlencode(params)}", 15, 2_000_000)
    if status != 200:
        raise OSError(f"http_{status}")
    return json.loads(body.decode("utf-8"))


def find_image(title: str, review: list[str] | None = None, get=_http_get) -> dict | None:
    if any(str(r).startswith("sensitive:") for r in (review or [])):
        return None
    for name in candidate_names(title):
        try:
            img = search(name, get)
        except Exception as e:                                   # noqa: BLE001 - a photo is optional, never fail the run
            log.warning("image lookup failed name_len=%d error=%s", len(name), type(e).__name__)
            return None
        if img:
            log.info("image found name_len=%d license=%s", len(name), img["license"])
            return img
    return None


def story_image(results: dict, get=_http_get) -> dict | None:
    """One photo for the whole story (shared by every language), from the English title, else any Latin-script title."""
    arts = {l: r.article for l, r in results.items() if r.article is not None and r.qa is not None}
    if not arts:
        return None
    review = [x for r in results.values() if r.qa is not None for x in r.qa.review]
    for lang in ["en"] + [l for l in arts if l != "en"]:
        a = arts.get(lang)
        if a and candidate_names(a["title"]):
            return find_image(a["title"], review, get)
    return None
