import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

from newsbot import images
from newsbot.articles import ArticleResult, write_outputs


def _page(title="File:Joe Biden official portrait.jpg", mime="image/jpeg", width=2000, lic="CC BY-SA 4.0", artist="<a>Jane Doe</a>",
          desc="Joe Biden at a podium", nonfree=""):
    meta = {"LicenseShortName": {"value": lic}, "Artist": {"value": artist}, "ImageDescription": {"value": desc},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"}, "ObjectName": {"value": "Biden"}}
    if nonfree:
        meta["NonFree"] = {"value": nonfree}
    return {"title": title, "index": 1, "imageinfo": [{"mime": mime, "width": width, "height": 1000, "thumburl": "https://upload.wikimedia.org/x.jpg",
            "thumbwidth": 1200, "thumbheight": 630, "descriptionurl": "https://commons.wikimedia.org/wiki/File:x", "extmetadata": meta}]}


def test_names():
    assert set(images.candidate_names("Joe Biden meets Xi Jinping in Bali")[:2]) == {"Joe Biden", "Xi Jinping"}
    assert images.candidate_names("The Latest News") == []
    assert "NATO" in images.candidate_names("NATO expands")


def test_pick_accepts_free_licence_and_strips_html():
    got = images.pick({"1": _page()}, "Joe Biden")
    assert got["credit"] == "Jane Doe" and got["license"] == "CC BY-SA 4.0" and got["url"].startswith("https://upload.wikimedia.org")


def test_pick_rejects_bad_files():
    assert images.pick({"1": _page(lic="Fair use")}, "Joe Biden") is None
    assert images.pick({"1": _page(lic="CC BY-NC 4.0")}, "Joe Biden") is None
    assert images.pick({"1": _page(width=500)}, "Joe Biden") is None
    assert images.pick({"1": _page(mime="image/svg+xml")}, "Joe Biden") is None
    assert images.pick({"1": _page(artist="")}, "Joe Biden") is None
    assert images.pick({"1": _page(nonfree="true")}, "Joe Biden") is None
    assert images.pick({"1": _page(title="File:Other person.jpg", desc="somebody else")}, "Joe Biden") is None


def test_sensitive_and_errors_give_no_photo():
    ok = lambda url, params: {"query": {"pages": {"1": _page()}}}
    assert images.find_image("Joe Biden visits", [], ok)
    assert images.find_image("Joe Biden visits", ["sensitive:death"], ok) is None
    def boom(url, params):
        raise OSError("x")
    assert images.find_image("Joe Biden visits", [], boom) is None


def _res(lang, title, review=()):
    art = {"title": title, "summary": "s", "slug": f"s-{lang}", "category": "world", "timeline": [], "confirmed": [], "unclear": [],
           "disputed": [], "sources": [], "body_markdown": "b"}
    qa = SimpleNamespace(disposition="publish", failures=[], review=list(review), details={})
    return ArticleResult(lang, "publish", art, qa)


def test_story_image_uses_english_title_and_write_keeps_it_on_update():
    seen = []
    def get(url, params):
        seen.append(params["gsrsearch"]); return {"query": {"pages": {"1": _page()}}}
    res = {"bn": _res("bn", "বাইডেন"), "en": _res("en", "Joe Biden visits Bali")}
    img = images.story_image(res, get)
    assert img and "Joe Biden" in seen[0]
    fs = SimpleNamespace(story_id="x", version=1, facts=[])
    with tempfile.TemporaryDirectory() as d:
        write_outputs(d, fs, res, image=img)
        fm = yaml.safe_load(Path(d, "content/en/s-en.md").read_text().split("---", 2)[1])
        assert fm["image"]["credit"] == "Jane Doe"
        write_outputs(d, fs, res)                       # an update passes no image: the live one is kept
        fm = yaml.safe_load(Path(d, "content/bn/s-bn.md").read_text().split("---", 2)[1])
        assert fm["image"]["license"] == "CC BY-SA 4.0"
