"""Telegram notifications: one chat for every step result (ok / fail / error / skip) and for review requests.

Best effort by design: a problem here is logged as a status code or an exception TYPE and never fails a run.
The bot token sits inside the Telegram API URL, so exception text is never logged (it would contain the URL).
The chat itself is private and may carry titles and summaries; workflow logs (public) never do.
stdlib only (PyYAML is optional) so the notify step still works when the engine's dependencies failed to install."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsbot.report import STATUSES, STEP_ORDER, Report

log = logging.getLogger("newsbot.notify")
API = "https://api.telegram.org"          # VERIFY
LIMIT = 3800                              # characters per message (Telegram allows 4096)
ICON = {"ok": "✅", "fail": "❌", "error": "🛑", "skip": "⏭"}
OUTCOME = {"success": "ok", "failure": "fail", "skipped": "skip", "cancelled": "error"}    # GitHub step outcome -> status
DEFAULTS = {"enabled": True, "language": "bn", "report_idle_runs": True, "include_summary": True, "max_lines": 60,
            "approve_buttons": False, "edit_url": ""}

T = {
    "bn": {"ok": "সফল", "fail": "ব্যর্থ", "error": "ত্রুটি", "skip": "বাদ", "workflow": "Workflow-এর ধাপ",
           "engine": "ইঞ্জিনের ধাপ", "review_new": "রিভিউ দরকার: নতুন লেখা", "review_update": "রিভিউ দরকার: আপডেট",
           "pending": "অপেক্ষমাণ খসড়া, মানুষের সিদ্ধান্ত দরকার", "published": "প্রকাশিত হয়েছে", "dry": "ড্রাই-রান, কিছুই প্রকাশ হয়নি",
           "reasons": "কারণ", "sources": "সূত্র", "open_pr": "PR খুলুন", "files": "লেখা দেখুন", "open_draft": "খসড়া খুলুন",
           "no_pr": "PR-এর লিংক পাওয়া যায়নি: brain repo-র Pull requests দেখুন", "more": "… আরও {n}টি লাইন",
           "run_link": "Run দেখুন", "story": "খবর", "no_report": "ইঞ্জিন কোনো রিপোর্ট বানায়নি",
           "pending_hint": "প্রকাশ করতে: ফাইলটি content/{lang}/-এ সরান আর pending: অংশ মুছুন",
           "approve": "✅ অনুমোদন", "reject": "❌ বাতিল", "pending_buttons": "নিচের বাটনে অনুমোদন বা বাতিল করুন (অনুমোদন করলেই প্রকাশ হবে)",
           "pending_gate": "মান-যাচাইয়ে ব্যর্থ: অনুমোদনের বাটন নেই, শুধু বাতিল করা যায়",
           "edit": "👁 দেখুন ও সম্পাদনা", "pending_edit": "লিংকে খুলে পড়ুন, দরকারে সম্পাদনা করে সংরক্ষণ করুন, তারপর অনুমোদন দিন"},
    "en": {"ok": "ok", "fail": "fail", "error": "error", "skip": "skip", "workflow": "Workflow steps",
           "engine": "Engine steps", "review_new": "Review needed: new article", "review_update": "Review needed: update",
           "pending": "Pending draft, needs a human decision", "published": "Published", "dry": "Dry run, nothing published",
           "reasons": "Reasons", "sources": "Sources", "open_pr": "Open PR", "files": "View article", "open_draft": "Open draft",
           "no_pr": "No PR link available: see Pull requests in the brain repo", "more": "… {n} more lines",
           "run_link": "View run", "story": "Story", "no_report": "the engine produced no report",
           "pending_hint": "To publish: move the file to content/{lang}/ and delete the pending: block",
           "approve": "✅ Approve", "reject": "❌ Reject", "pending_buttons": "Use the buttons below to approve or reject (approving publishes it)",
           "pending_gate": "Failed a quality gate: no Approve button, you can only reject",
           "edit": "👁 View and edit", "pending_edit": "Open the link to read, edit and save if needed, then approve"},
}


# ---------------- settings ----------------

def load_notify_settings(brain: str | Path) -> dict:
    """notify.telegram from the brain's config/newsbot.yaml merged over DEFAULTS, plus site.timezone_offset_hours."""
    out = dict(DEFAULTS, tz=6.0)
    try:
        import yaml
        cfg = yaml.safe_load((Path(brain) / "config" / "newsbot.yaml").read_text(encoding="utf-8")) or {}
    except Exception:                                   # no PyYAML or unreadable: defaults are fine
        return out
    tg = ((cfg.get("notify") or {}).get("telegram")) or {}
    for k in DEFAULTS:
        if k in tg:
            out[k] = tg[k]
    try:
        out["tz"] = float((cfg.get("site") or {}).get("timezone_offset_hours", 6))
    except (TypeError, ValueError):
        pass
    if out["language"] not in T:
        out["language"] = "bn"
    return out


# ---------------- Telegram client ----------------

def split_text(text: str, limit: int = LIMIT) -> list[str]:
    """Split on line boundaries (every HTML tag lives inside one line, so tags stay balanced)."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        line = line[:limit]
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    return chunks + [cur] if cur else chunks


class Telegram:
    def __init__(self, token: str, chat_id: str, transport=None, sleep=time.sleep, pause: float = 1.0):
        self.token, self.chat_id, self.transport, self._sleep, self.pause = token, chat_id, transport, sleep, pause

    def _call(self, method: str, payload: dict) -> tuple[int, dict]:
        if self.transport is not None:
            return self.transport(method, payload)
        req = urllib.request.Request(f"{API}/bot{self.token}/{method}", data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:             # never log e: its text can carry the URL with the token
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except Exception:
            return 0, {}

    def _send_one(self, payload: dict) -> bool:
        for attempt in (0, 1):
            code, body = self._call("sendMessage", payload)
            if code == 200 and body.get("ok", True):
                self._sleep(self.pause)                 # Telegram allows about one message per second per chat
                return True
            if code == 429 and attempt == 0:
                self._sleep(min(int((body.get("parameters") or {}).get("retry_after", 5)), 30))
                continue
            if code == 400 and attempt == 0 and "parse_mode" in payload:     # markup problem: resend as plain text
                payload = {k: v for k, v in payload.items() if k != "parse_mode"}
                payload["text"] = html.unescape(re.sub(r"</?[a-z][^>]*>", "", payload["text"]))
                continue
            log.warning("telegram send failed status=%s", code)
            return False
        return False

    def send(self, text: str, buttons: list | None = None, silent: bool = False) -> bool:
        chunks = split_text(text)
        ok = bool(chunks)
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                       "disable_web_page_preview": True, "disable_notification": silent}
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            ok = self._send_one(payload) and ok
        return ok


# ---------------- formatting ----------------

def _e(s: object, n: int = 0) -> str:
    s = str(s)
    return html.escape(s[:n] + "…" if n and len(s) > n else s, quote=False)


def parse_steps(spec: str) -> list[tuple[str, str]]:
    """'checkout=success,run=failure,deploy=skipped' -> [(name, status)]; unknown outcomes count as skip."""
    out = []
    for part in (spec or "").split(","):
        if "=" in part:
            name, outcome = part.split("=", 1)
            out.append((name.strip().replace("_", " "), OUTCOME.get(outcome.strip(), "skip")))
    return out


def _local(now: datetime, tz: float) -> str:
    return (now.astimezone(timezone.utc) + timedelta(hours=tz)).strftime("%Y-%m-%d %H:%M")


def is_idle(report: Report | None, steps: list[tuple[str, str]]) -> bool:
    bad = any(s in ("fail", "error") for _, s in steps)
    if report is None:
        return not bad
    c = report.counts()
    return not (bad or c["fail"] or c["error"] or report.reviews or report.covered_stories())


def format_report(report: Report | None, steps: list[tuple[str, str]], lang: str, tz: float, max_lines: int,
                  workflow: str, run_url: str, now: datetime) -> str:
    t = T[lang]
    counts = {s: 0 for s in STATUSES}
    for _, s in steps:
        counts[s] += 1
    if report is not None:
        for s, n in report.counts().items():
            counts[s] += n
    icon = "🔴" if counts["error"] else "🟠" if counts["fail"] else "🟢"
    mode = report.mode if report is not None and report.mode else "-"
    lines = [f"{icon} <b>NewsBot</b> · {_e(workflow)} · {_e(mode)}", f"🕒 {_local(now, tz)}",
             " · ".join(f"{ICON[s]} {counts[s]}" for s in STATUSES)]
    if steps:
        lines += ["", f"⚙️ <b>{t['workflow']}</b>"]
        for s in ("error", "fail", "ok", "skip"):
            names = [n for n, st in steps if st == s]
            if names:
                lines.append(f"{ICON[s]} {t[s]}: {_e(', '.join(names))}")
    lines += ["", f"🔎 <b>{t['engine']}</b>"]
    if report is None:
        lines.append(f"{ICON['skip']} run — {t['no_report']}")
    else:
        order = {s: i for i, s in enumerate(STEP_ORDER)}
        titles = {r["story_id"]: r["title"] for r in report.reviews}

        def line(e, indent=""):
            lang_tag = f"[{_e(e['lang'])}]" if e["lang"] else ""
            detail = f" — {_e(e['detail'], 160)}" if e["detail"] else ""
            return f"{indent}{ICON[e['status']]} {_e(e['step'])}{lang_tag}{detail}"
        global_ev = sorted((e for e in report.events if not e["story"]), key=lambda e: order.get(e["step"], 99))
        lines += [line(e) for e in global_ev]
        for sid in sorted({e["story"] for e in report.events if e["story"]}):
            info = report.stories.get(sid, {})
            head = f"📰 <code>{_e(sid)}</code> {_e(info.get('action', ''))}"
            if sid in titles:
                head += f" · {_e(titles[sid], 70)}"
            lines.append("")
            lines.append(head)
            lines += [line(e, "└ ") for e in sorted((e for e in report.events if e["story"] == sid),
                                                     key=lambda e: order.get(e["step"], 99))]
    if len(lines) > max_lines:
        extra = len(lines) - (max_lines - 1)
        lines = lines[:max_lines - 1] + [t["more"].format(n=extra)]
    if run_url:
        lines += ["", f'<a href="{html.escape(run_url, quote=True)}">{t["run_link"]}</a>']
    return "\n".join(lines)


def draft_ref(slug: str) -> str:
    """Short, stable id of a pending draft for Telegram callback_data (limit 64 bytes). The approve Worker computes the
    same value (SHA-1 of the slug, first 10 hex chars) from the file names in pending/<lang>/."""
    return hashlib.sha1(slug.encode("utf-8")).hexdigest()[:10]


def edit_link(base: str, token: str, lang: str, ref: str) -> str:
    """Private link to the approve Worker's page for one draft. The signature is an HMAC of "lang|ref" keyed from the bot
    token, which the Worker also holds, so no extra secret is needed. It opens one draft only and dies when it is handled."""
    key = hashlib.sha256(("newsbot-edit|" + token).encode("utf-8")).digest()
    sig = hmac.new(key, f"{lang}|{ref}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{base.rstrip('/')}/d/{lang}/{ref}?s={sig}"


def _safe_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    return url if re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]+)?", url) else ""


def approvable(item: dict) -> bool:
    """Only drafts held back for being sensitive may be approved from the phone. A draft that failed a quality gate
    (unsupported name, figure, quote, too short ...) can be rejected or opened, never approved here."""
    reasons = item.get("reasons") or []
    return bool(reasons) and all(str(r).startswith("sensitive:") for r in reasons)


def format_review(item: dict, lang: str, include_summary: bool, pr_url: str, draft_url: str,
                  approve: bool = False, edit_url: str = "") -> tuple[str, list, bool]:
    """Returns (text, buttons, silent). Actionable items make a sound; published/dry-run ones do not."""
    t, st = T[lang], item["status"]
    if st == "awaiting_review":
        head, silent = f"📝 <b>{t['review_update'] if item['kind'] == 'update' else t['review_new']}</b>", False
    elif st == "pending":
        head, silent = f"⚠️ <b>{t['pending']}</b>", False
    elif st == "published":
        head, silent = f"🟢 <b>{t['published']}</b>", True
    else:
        head, silent = f"🧪 <b>{t['dry']}</b>", True
    lines = [f"{head} [{_e(item['lang'])}]", f"<b>{_e(item['title'], 200)}</b>"]
    if include_summary and item.get("summary"):
        lines.append(_e(item["summary"], 300))
    if item.get("reasons"):
        lines.append(f"{t['reasons']}: {_e(', '.join(item['reasons']), 200)}")
    srcs = [f"{s['name']} (T{s['tier']})" for s in item.get("sources", [])[:6] if s.get("name")]
    if srcs:
        lines.append(f"{t['sources']}: {_e(', '.join(srcs), 300)}")
    buttons: list = []
    if st == "awaiting_review":
        if pr_url:
            buttons = [[{"text": t["open_pr"], "url": pr_url}, {"text": t["files"], "url": pr_url + "/files"}]]
        else:
            lines.append(t["no_pr"])
    elif st == "pending":
        view = [[{"text": t["edit"], "url": edit_url}]] if edit_url else [[{"text": t["open_draft"], "url": draft_url}]] if draft_url else []
        if approve and item.get("slug") and re.fullmatch(r"[a-z]{2,3}", str(item.get("lang", ""))):
            ref, can = draft_ref(item["slug"]), approvable(item)
            row = ([{"text": t["approve"], "callback_data": f"a|{item['lang']}|{ref}"}] if can else []) \
                + [{"text": t["reject"], "callback_data": f"r|{item['lang']}|{ref}"}]
            buttons = [row] + view
            lines.append(t["pending_gate"] if not can else t["pending_edit"] if edit_url else t["pending_buttons"])
        else:
            buttons = view
            lines.append(t["pending_edit"] if edit_url else t["pending_hint"].format(lang=item["lang"]))
    return "\n".join(lines), buttons, silent


# ---------------- the command ----------------

def _safe_url(url: str, server: str) -> str:
    url = (url or "").strip()
    return url if url.startswith(server + "/") and not re.search(r"\s", url) else ""


def notify_cmd(brain: str | Path, report_path: str | None, pr_url_file: str | None, workflow: str,
               env=os.environ, transport=None, sleep=time.sleep, now: datetime | None = None) -> int:
    """Sends the run report (quiet) and one message per review item. Always returns 0: never fails the workflow."""
    try:
        s = load_notify_settings(brain)
        token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
        if not s["enabled"] or not token or not chat:
            log.info("telegram off")
            return 0
        now = now or datetime.now(timezone.utc)
        server = (env.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
        steps = parse_steps(env.get("STEP_OUTCOMES", ""))
        report = Report.load(report_path)
        if report is None and any(n == "run" and st in ("fail", "error") for n, st in steps):
            report = Report()
            report.add("run", "error", T[s["language"]]["no_report"])
        pr_url = ""
        if pr_url_file:
            try:
                pr_url = _safe_url(Path(pr_url_file).read_text(encoding="utf-8"), server)
            except OSError:
                pass
        repo = env.get("BRAIN_REPO", "")
        tg = Telegram(token, chat, transport=transport, sleep=sleep, pause=0 if transport is not None else 1.0)
        sent = 0
        if s["report_idle_runs"] or not is_idle(report, steps):
            run_url = _safe_url(env.get("RUN_URL", ""), server)
            text = format_report(report, steps, s["language"], float(s["tz"]), int(s["max_lines"]), workflow, run_url, now)
            loud = any(st in ("fail", "error") for _, st in steps) or bool(report and (report.counts()["error"] or report.counts()["fail"]))
            sent += tg.send(text, silent=not loud)
        for item in (report.reviews if report else []):
            draft = f"{server}/{repo}/blob/main/pending/{item['lang']}/{item['slug']}.md" if repo and item["status"] == "pending" else ""
            edit = ""
            if item["status"] == "pending" and item.get("slug") and _safe_base(s.get("edit_url", "")) \
                    and re.fullmatch(r"[a-z]{2,3}", str(item.get("lang", ""))):
                edit = edit_link(_safe_base(s["edit_url"]), token, item["lang"], draft_ref(item["slug"]))
            text, buttons, silent = format_review(item, s["language"], bool(s["include_summary"]), pr_url, draft,
                                                    bool(s.get("approve_buttons")), edit)
            sent += tg.send(text, buttons, silent)
        log.info("telegram sent=%d", sent)
    except Exception as e:                              # type only: the text may carry the token
        log.warning("telegram notify failed: %s", type(e).__name__)
    return 0
