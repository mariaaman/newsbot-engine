"""Telegram approve / reject buttons and the private view-and-edit link for pending drafts (docs/TELEGRAM_APPROVE.md)."""
from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from newsbot import notify as nt
from newsbot.report import Report

TOKEN = "123:abc"
ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42", "BRAIN_REPO": "owner/newsbot-brain",
       "GITHUB_SERVER_URL": "https://github.com", "STEP_OUTCOMES": "run=success"}
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
WORKER = "https://newsbot-approve.me.workers.dev"


def item(status="pending", reasons=("sensitive:death",), slug="tom-skerritt-dies-93", lang="en"):
    return {"story_id": "st-1", "lang": lang, "kind": "new", "status": status, "title": "Tom Skerritt dies", "summary": "S.",
            "slug": slug, "reasons": list(reasons), "sources": [{"name": "BBC", "tier": 1}]}


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        return 200, {"ok": True}


def send(items, tg):
    import yaml
    d = Path(tempfile.mkdtemp())
    (d / "config").mkdir()
    (d / "config" / "newsbot.yaml").write_text(yaml.safe_dump({"notify": {"telegram": {"report_idle_runs": False, **tg}}}), encoding="utf-8")
    rep = Report("review")
    rep.reviews.extend(items)
    rp = d / "r.json"
    rep.save(rp)
    fake = Fake()
    nt.notify_cmd(d, str(rp), None, "hourly", env=ENV, transport=fake, sleep=lambda s: None, now=NOW)
    return [p for _, p in fake.calls if "Tom Skerritt" in p.get("text", "")], fake


def buttons(msg):
    return [b for row in msg["reply_markup"]["inline_keyboard"] for b in row]


def test_draft_ref_and_edit_link_match_the_worker_values():
    assert nt.draft_ref("tom-skerritt-dies-93") == "7c775f641c"
    assert nt.draft_ref("প্রধানমন্ত্রী-রাজশাহী") == "a9802abe76"
    link = nt.edit_link(WORKER + "/", TOKEN, "en", "7c775f641c")
    assert link == f"{WORKER}/d/en/7c775f641c?s=134bb1f077f361ca9cbfae8761be5bd7ed2d6f83a1843758872d8537a88e6c7a"


def test_approvable_means_sensitive_only():
    assert nt.approvable(item(reasons=("sensitive:death", "sensitive:minors")))
    assert not nt.approvable(item(reasons=("sensitive:death", "unsupported_name")))
    assert not nt.approvable(item(reasons=("min_words",)))
    assert not nt.approvable(item(reasons=()))


def test_sensitive_draft_gets_approve_reject_and_a_private_link():
    msgs, fake = send([item()], {"approve_buttons": True, "edit_url": WORKER, "language": "en"})
    assert len(msgs) == 1
    rows = msgs[0]["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == ["✅ Approve", "❌ Reject"]
    assert [b["callback_data"] for b in rows[0]] == ["a|en|7c775f641c", "r|en|7c775f641c"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in rows[0])                       # Telegram's limit
    assert re.fullmatch(rf"{re.escape(WORKER)}/d/en/7c775f641c\?s=[0-9a-f]{{64}}", rows[1][0]["url"])
    assert TOKEN not in json.dumps(fake.calls)                                                 # only an HMAC of it travels
    assert msgs[0]["disable_notification"] is False


def test_gate_failed_draft_can_only_be_rejected_or_opened():
    msgs, _ = send([item(reasons=("unsupported_name", "sensitive:death"))], {"approve_buttons": True, "edit_url": WORKER, "language": "en"})
    cb = [b["callback_data"] for b in buttons(msgs[0]) if "callback_data" in b]
    assert cb == ["r|en|7c775f641c"]
    assert "Failed a quality gate" in msgs[0]["text"]


def test_defaults_keep_the_old_behaviour_exactly():
    msgs, _ = send([item()], {"language": "en"})
    b = buttons(msgs[0])
    assert len(b) == 1 and b[0]["url"].endswith("/blob/main/pending/en/tom-skerritt-dies-93.md") and "callback_data" not in b[0]
    assert "To publish: move the file" in msgs[0]["text"]


def test_link_only_mode_and_bad_edit_url():
    msgs, _ = send([item()], {"edit_url": WORKER, "language": "en"})
    b = buttons(msgs[0])
    assert len(b) == 1 and "/d/en/7c775f641c?s=" in b[0]["url"] and "callback_data" not in b[0]
    for bad in ("http://insecure.example", "https://x.dev/path?q=1", "javascript:alert(1)", "https://a b.dev"):
        msgs, _ = send([item()], {"edit_url": bad, "language": "en"})
        assert "/d/" not in json.dumps(msgs[0]["reply_markup"])


def test_other_review_kinds_are_unchanged_and_odd_slugs_never_break_a_message():
    msgs, _ = send([item("awaiting_review")], {"approve_buttons": True, "edit_url": WORKER, "language": "en"})
    assert "callback_data" not in json.dumps(msgs[0].get("reply_markup", {}))
    msgs, _ = send([item(slug="")], {"approve_buttons": True, "edit_url": WORKER, "language": "en"})
    assert "callback_data" not in json.dumps(msgs[0].get("reply_markup", {}))
