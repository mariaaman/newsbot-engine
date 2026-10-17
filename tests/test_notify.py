"""Telegram notifications (offline: a fake transport stands in for api.telegram.org). Zero-arg tests, also run by pytest."""
import importlib.util
import json
import logging
import re
import shutil
import sys
import tempfile
import types
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from newsbot import notify as nt
from newsbot.cli import FIXTURES, main, run_pipeline
from newsbot.discover import fixture_fetcher
from newsbot.fetch import fixture_article_fetcher
from newsbot.llm import build_llm, load_llm_settings
from newsbot.llm.dryrun import dry_run_provider
from newsbot.logging_setup import redact
from newsbot.report import STATUSES, Report, collect, record_run
from tests.helpers import DEFAULTS, NOW, ROOT, TEMPLATE, make_cfg, mock_embedder
from tests.test_m8 import capture_logs, log_lines
from tests.test_workflows import WF_DIR, _script, _setup, _write_run_output, load, steps

CFG = make_cfg()
TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"          # fake, but token-shaped
CHAT = "-1001234567890"
ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT, "BRAIN_REPO": "owner/newsbot-brain",
       "GITHUB_SERVER_URL": "https://github.com", "RUN_URL": "https://github.com/owner/newsbot/actions/runs/9",
       "STEP_OUTCOMES": "run=success,commit=success,plan=success,build=skipped"}


class Fake:
    """Fake Telegram transport: records every call; `script` is a list of (code, body) answers, 200 afterwards."""
    def __init__(self, script=None):
        self.calls, self.script = [], list(script or [])

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        return self.script.pop(0) if self.script else (200, {"ok": True})

    @property
    def texts(self):
        return [p["text"] for _, p in self.calls]


def run_with_report(mode="review", fail_feed=False):
    llm = build_llm(load_llm_settings(TEMPLATE), None, dry_run=True, mock=dry_run_provider())
    base = fixture_fetcher(FIXTURES / "feeds", CFG.sources)
    bbc = next(s.url for s in CFG.sources if s.name == "BBC News World")

    def feeds(url):
        if fail_feed and url == bbc:
            raise OSError("down")
        return base(url)
    report = Report(mode)
    with tempfile.TemporaryDirectory() as d:
        brain = Path(d) / "brain"
        shutil.copytree(TEMPLATE, brain)
        with collect(report):
            r = run_pipeline(CFG, mode, brain, now=NOW, llm=llm, embedder=mock_embedder, feed_fetcher=feeds,
                             article_fetcher=fixture_article_fetcher(FIXTURES / "articles"), report=report)
    return r, report


def brain_with(tg: dict | None = None) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "config").mkdir()
    (d / "config" / "newsbot.yaml").write_text(yaml.safe_dump({"notify": {"telegram": tg or {}}}), encoding="utf-8")
    return d


def notify(report, env=None, brain=None, pr_url=None, transport=None, sleeps=None):
    tmp = Path(tempfile.mkdtemp())
    rp = tmp / "r.json"
    if report is not None:
        report.save(rp)
    pf = None
    if pr_url is not None:
        pf = tmp / "pr.txt"
        pf.write_text(pr_url + "\n", encoding="utf-8")
    tr = transport or Fake()
    rc = nt.notify_cmd(brain or brain_with(), str(rp), str(pf) if pf else None, "hourly", env=ENV if env is None else env, transport=tr,
                       sleep=(sleeps.append if sleeps is not None else (lambda s: None)), now=NOW)
    return rc, tr


def review(status, kind="new", lang="bn", title="Barun", reasons=(), slug="barun"):
    return {"story_id": "st-1", "lang": lang, "kind": kind, "status": status, "title": title, "summary": "Short summary.",
            "slug": slug, "reasons": list(reasons), "sources": [{"name": "BBC", "tier": 1}, {"name": "Daily Star", "tier": 2}]}


# ---------------- the run report ----------------

def test_run_report_has_an_ok_fail_error_skip_event_for_every_step():
    r, rep = run_with_report()
    ev = rep.events
    assert ev and all(e["status"] in STATUSES for e in ev)
    steps_seen = {e["step"] for e in ev}
    assert {"discover", "cluster", "score", "gate", "fetch", "extract", "verify", "facts", "write", "qa", "publish"} <= steps_seen
    assert rep.counts()["ok"] > 5 and rep.counts()["skip"] >= 2
    sid = rep.reviews[0]["story_id"]                                   # the cyclone story: sensitive -> pending
    assert {x["status"] for x in rep.reviews} == {"pending"} and any("sensitive:death" in x["reasons"] for x in rep.reviews)
    assert any(e["step"] == "verify" and e["story"] == sid for e in ev)          # log lines were attached to their story
    assert any(e["step"] == "extract" and e["story"] == sid for e in ev)
    other = [e for e in ev if e["step"] == "facts" and e["status"] == "skip"]
    assert other and other[0]["detail"] == "insufficient_sources" and other[0]["story"] != sid
    assert all(e["story"] == other[0]["story"] for e in ev if e["step"] == "fetch" and e["status"] == "skip")
    assert any(e["step"] == "qa" and "needs a human" in e["detail"] and e["lang"] in ("bn", "en") for e in ev)
    assert sum(rep.tokens.values()) > 0 and r["report"] is rep


def test_a_failed_feed_is_a_fail_event_and_dry_run_reports_too():
    _, rep = run_with_report(fail_feed=True)
    f = [e for e in rep.events if e["step"] == "discover" and e["status"] == "fail"]
    assert f and f[0]["detail"] == "BBC News World: OSError"
    _, rep = run_with_report(mode="dry-run")
    assert {x["status"] for x in rep.reviews} == {"pending"}           # sensitive stays pending in every mode


def test_idle_run_reports_every_step_that_did_not_run_as_skip():
    rep = Report("review")
    record_run(rep, mode="review", stats={"feeds_ok": 8, "feeds_failed": 0, "items": 3}, n_stories=2, decisions=[],
               articles={}, updates={})
    skipped = {e["step"] for e in rep.events if e["status"] == "skip"}
    assert {"fetch", "extract", "verify", "write", "qa", "publish"} <= skipped and rep.counts()["fail"] == 0
    assert nt.is_idle(rep, [("run", "ok")]) and not nt.is_idle(rep, [("run", "fail")])


def test_crash_and_invalid_config_are_error_events_without_exception_text():
    secret = "Zorblax draft text gsk_TESTSECRETvalue0123456789"
    fake = types.ModuleType("newsbot.config")
    import newsbot.cli as cli
    orig_run, orig_setup, old_mod = cli.run_pipeline, cli.setup_logging, sys.modules.get("newsbot.config")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "r.json"
        argv = ["run", "--mode", "dry-run", "--brain", str(TEMPLATE), "--report", str(path)]
        try:
            fake.load_config = lambda brain: CFG
            sys.modules["newsbot.config"] = fake
            cli.setup_logging = lambda level="INFO": None            # keep capture_logs' handler (setup_logging clears handlers)

            def boom(*a, **k):
                raise ValueError(secret)
            cli.run_pipeline = boom
            with capture_logs() as cap:
                assert main(argv) == 1
            rep = Report.load(path)
            err = [e for e in rep.events if e["step"] == "run"]
            assert err and err[0]["status"] == "error" and re.fullmatch(r"ValueError at test_notify.py:\d+", err[0]["detail"])
            assert "Zorblax" not in path.read_text(encoding="utf-8") and "Zorblax" not in "\n".join(log_lines(cap))
            cli.run_pipeline = orig_run

            def bad(brain):
                raise RuntimeError(secret)
            fake.load_config = bad
            assert main(argv) == 2
            rep = Report.load(path)
            assert [e["detail"] for e in rep.events] == ["config invalid: RuntimeError"] and "Zorblax" not in path.read_text(encoding="utf-8")
        finally:
            cli.run_pipeline, cli.setup_logging = orig_run, orig_setup
            if old_mod is None:
                sys.modules.pop("newsbot.config", None)
            else:
                sys.modules["newsbot.config"] = old_mod


# ---------------- messages ----------------

def test_report_message_has_workflow_and_engine_sections_in_both_languages():
    _, rep = run_with_report()
    steps_ = nt.parse_steps("checkout_engine=success,run=success,deploy=skipped,build=failure,record=cancelled")
    assert steps_ == [("checkout engine", "ok"), ("run", "ok"), ("deploy", "skip"), ("build", "fail"), ("record", "error")]
    bn = nt.format_report(rep, steps_, "bn", 6, 60, "hourly", ENV["RUN_URL"], NOW)
    en = nt.format_report(rep, steps_, "en", 6, 60, "hourly", ENV["RUN_URL"], NOW)
    for text in (bn, en):
        assert text.startswith("🔴") and "✅" in text and "❌" in text and "🛑" in text and "⏭" in text    # cancelled = error
        assert "<code>st-" in text and "2026-10-" in text and ENV["RUN_URL"] in text
    assert "Workflow-এর ধাপ" in bn and "ইঞ্জিনের ধাপ" in bn and "Workflow steps" in en and "Engine steps" in en
    assert "http" not in re.sub(r'<a href="[^"]+">', "", en).replace(ENV["RUN_URL"], "")        # no source URLs leak in
    assert nt.format_report(None, [], "en", 6, 60, "hourly", "", NOW).count("no report") == 1


def test_messages_escape_markup_cap_lines_and_stay_under_the_telegram_limit():
    evil = review("awaiting_review", title='<b>x</b> & <script>alert(1)</script> [S1](http://evil.test)', reasons=["a<b"])
    text, buttons, silent = nt.format_review(evil, "en", True, "https://github.com/o/r/pull/1", "")
    assert "<script" not in text and "&lt;script&gt;" in text and "&amp;" in text and text.count("<b>") == 2   # header + title only
    rep = Report("review")
    for i in range(300):
        rep.add("fetch", "skip", f"d{i}.example: HTTP 404")
    msg = nt.format_report(rep, [], "en", 6, 25, "hourly", "", NOW)
    assert msg.count("\n") <= 30 and "more lines" in msg
    long = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
    chunks = nt.split_text(long)
    assert len(chunks) > 3 and all(len(c) <= 4096 for c in chunks) and "\n".join(chunks) == long


def test_review_messages_by_status():
    pr = "https://github.com/owner/newsbot-brain/pull/7"
    t, b, silent = nt.format_review(review("awaiting_review"), "bn", True, pr, "")
    assert "রিভিউ দরকার: নতুন লেখা" in t and "Short summary." in t and "BBC (T1)" in t and not silent
    assert b == [[{"text": "PR খুলুন", "url": pr}, {"text": "লেখা দেখুন", "url": pr + "/files"}]]
    t, b, silent = nt.format_review(review("awaiting_review", kind="update"), "en", False, "", "")
    assert "update" in t and "Short summary." not in t and b == [] and "No PR link" in t
    draft = "https://github.com/owner/newsbot-brain/blob/main/pending/bn/barun.md"
    t, b, silent = nt.format_review(review("pending", reasons=["sensitive:death"]), "en", True, pr, draft)
    assert "sensitive:death" in t and not silent and b == [[{"text": "Open draft", "url": draft}]] and "content/bn/" in t
    for st in ("published", "dry_run"):
        assert nt.format_review(review(st), "en", True, pr, "")[2] is True          # no sound, no buttons
        assert nt.format_review(review(st), "en", True, pr, "")[1] == []


# ---------------- sending ----------------

def test_notify_sends_a_quiet_report_then_one_message_per_review_item():
    _, rep = run_with_report()
    rep.reviews.append(review("awaiting_review", title="Metro extension"))
    rep.reviews.append(review("published", title="Done"))
    with capture_logs() as cap:
        rc, tr = notify(rep, pr_url="https://github.com/owner/newsbot-brain/pull/7")
    assert rc == 0 and len(tr.calls) == 1 + len(rep.reviews) == 5          # one report + one message per review item
    first = tr.calls[0][1]
    assert first["chat_id"] == CHAT and first["parse_mode"] == "HTML" and first["disable_notification"] is True   # routine = silent
    pend = [p for _, p in tr.calls[1:] if "blob/main/pending/" in json.dumps(p.get("reply_markup", {}))]
    assert len(pend) == 2 and all(p["disable_notification"] is False for p in pend)
    pr_msg = next(p for _, p in tr.calls if "Metro extension" in p["text"])
    assert pr_msg["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("/pull/7") and pr_msg["disable_notification"] is False
    done = next(p for _, p in tr.calls if "Done" in p["text"])
    assert done["disable_notification"] is True
    assert TOKEN not in json.dumps(tr.calls) and TOKEN not in "\n".join(log_lines(cap))


def test_a_failing_workflow_step_makes_the_report_loud():
    rep = Report("review")
    record_run(rep, mode="review", stats={"feeds_ok": 8, "feeds_failed": 0, "items": 3}, n_stories=1, decisions=[],
               articles={}, updates={})
    _, tr = notify(rep, env={**ENV, "STEP_OUTCOMES": "run=success,build=failure"})
    assert tr.calls[0][1]["disable_notification"] is False and tr.texts[0].startswith("🟠")
    _, tr = notify(rep, env={**ENV, "STEP_OUTCOMES": "run=success,build=success"})
    assert tr.calls[0][1]["disable_notification"] is True and tr.texts[0].startswith("🟢")


def test_missing_engine_report_after_a_failed_run_step_is_reported_as_an_error():
    _, tr = notify(None, env={**ENV, "STEP_OUTCOMES": "run=failure,commit=skipped"})
    assert tr.texts[0].startswith("🔴") and "🛑" in tr.texts[0] and "❌ ব্যর্থ: run" in tr.texts[0]


def test_pr_and_run_links_must_stay_on_the_github_server():
    for bad in ("http://github.com/o/r/pull/1", "https://github.com.evil.test/o/r/pull/1", "https://evil.test/pull/1",
                "https://github.com/o/r/pull/1 https://evil.test", "javascript:alert(1)"):
        rep = Report("review")
        rep.reviews.append(review("awaiting_review"))
        _, tr = notify(rep, pr_url=bad, env={**ENV, "RUN_URL": bad})
        assert all("reply_markup" not in p for _, p in tr.calls) and "evil" not in json.dumps(tr.calls), bad
        assert any("PR-এর লিংক পাওয়া যায়নি" in x for x in tr.texts)


def test_off_without_secrets_or_when_disabled_and_idle_runs_can_be_muted():
    rep = Report("review")
    record_run(rep, mode="review", stats={"feeds_ok": 8, "feeds_failed": 0, "items": 3}, n_stories=1, decisions=[],
               articles={}, updates={})
    for env in ({}, {"TELEGRAM_BOT_TOKEN": TOKEN}, {"TELEGRAM_CHAT_ID": CHAT}):
        assert notify(rep, env=env)[1].calls == []
    assert notify(rep, brain=brain_with({"enabled": False}))[1].calls == []
    assert notify(rep, brain=brain_with({"report_idle_runs": False}))[1].calls == []
    assert len(notify(rep, brain=brain_with({"report_idle_runs": True}))[1].calls) == 1
    _, tr = notify(rep, brain=brain_with({"report_idle_runs": False}), env={**ENV, "STEP_OUTCOMES": "run=failure"})
    assert len(tr.calls) == 1                                          # but a failure is always reported
    _, tr = notify(rep, brain=brain_with({"language": "en", "include_summary": False}))
    assert "Engine steps" in tr.texts[0]
    assert nt.load_notify_settings("/nonexistent") == dict(nt.DEFAULTS, tz=6.0)


def test_sending_never_fails_the_run_and_never_logs_the_token():
    secret_text = f"Zorblax {TOKEN} https://api.telegram.org/bot{TOKEN}/sendMessage"
    rep = Report("review")
    rep.reviews.append(review("pending"))
    with capture_logs() as cap:
        rc, _ = notify(rep, transport=Fake([(500, {})] * 5))
        assert rc == 0

        def raising(method, payload):
            raise RuntimeError(secret_text)
        assert notify(rep, transport=raising)[0] == 0
        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError(secret_text))
        try:
            assert nt.Telegram(TOKEN, CHAT).send("hello") is False        # real transport path, network "down"
        finally:
            urllib.request.urlopen = orig
    out = "\n".join(log_lines(cap))
    assert "telegram send failed status=500" in out and "telegram notify failed: RuntimeError" in out
    assert "status=0" in out and TOKEN not in out and "Zorblax" not in out and "api.telegram.org" not in out


def test_rate_limit_is_waited_out_once_and_markup_errors_fall_back_to_plain_text():
    sleeps = []
    tg = nt.Telegram(TOKEN, CHAT, transport=(fk := Fake([(429, {"parameters": {"retry_after": 3}})])), sleep=sleeps.append, pause=0)
    assert tg.send("hi") and sleeps[0] == 3 and len(fk.calls) == 2
    tg = nt.Telegram(TOKEN, CHAT, transport=(fk := Fake([(429, {"parameters": {"retry_after": 1}}), (429, {})])), sleep=sleeps.append, pause=0)
    assert tg.send("hi") is False and len(fk.calls) == 2              # a second 429 is not retried again
    tg = nt.Telegram(TOKEN, CHAT, transport=(fk := Fake([(400, {})])), pause=0)
    assert tg.send("<b>bold</b> &amp; more") and "parse_mode" not in fk.calls[1][1] and fk.calls[1][1]["text"] == "bold & more"


# ---------------- wiring ----------------

def test_hourly_workflow_notifies_after_everything_and_reports_every_step():
    wf = load(WF_DIR / "hourly.yml")
    st = steps(wf)
    last = st[-1]
    assert last["if"] == "always()" and last["continue-on-error"] is True and "newsbot notify" in last["run"]
    assert re.fullmatch(r"\$\{\{ secrets\.TELEGRAM_BOT_TOKEN \}\}", last["env"]["TELEGRAM_BOT_TOKEN"])
    assert re.fullmatch(r"\$\{\{ secrets\.TELEGRAM_CHAT_ID \}\}", last["env"]["TELEGRAM_CHAT_ID"])
    ids = [s["id"] for s in st if "id" in s]
    named = re.findall(r"(\w+)=\$\{\{ steps\.(\w+)\.outcome \}\}", last["env"]["STEP_OUTCOMES"])
    assert all(a == b and b in ids for a, b in named) and len(named) == len(set(ids))      # every identified step, once
    for needed in ("run", "commit", "plan", "build", "file_count", "deploy"):
        assert needed in ids
    engine = next(s for s in st if s.get("id") == "run")
    assert '--report "$RUNNER_TEMP/run_report.json"' in engine["run"] and '--report "$RUNNER_TEMP/run_report.json"' in last["run"]
    assert "NEWSBOT_PR_URL_FILE" in next(s for s in st if s.get("id") == "commit")["run"]
    secret_steps = [s.get("name") for s in st if any("TELEGRAM" in k for k in (s.get("env") or {}))]
    assert secret_steps == [last["name"]]                                # the secrets reach only the notify step
    setup = " ".join(str(s.get("env")) for s in steps(load(WF_DIR / "setup.yml")))
    assert "TELEGRAM_BOT_TOKEN" in setup and "TELEGRAM_CHAT_ID" in setup


def test_brain_commit_writes_the_pr_url_to_a_file_and_never_prints_it():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _, brain, env = _setup(tmp)
        _write_run_output(brain)
        r = _script(brain, "review", {**env, "NEWSBOT_PR_URL_FILE": str(tmp / "pr.txt")})
        assert r.stdout.split() == ["committed", "pr-opened"] and "example.invalid" not in r.stdout + r.stderr
        assert (tmp / "pr.txt").read_text() == "https://example.invalid/pr/1\n"
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _, brain, env = _setup(tmp)
        _write_run_output(brain)
        assert _script(brain, "review", env).stdout.split() == ["committed", "pr-opened"] and not (tmp / "pr.txt").exists()


def _load_setup_check():
    spec = importlib.util.spec_from_file_location("setup_check", ROOT / "scripts" / "setup_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_setup_check_verifies_the_bot_and_sends_a_test_message():
    sc = _load_setup_check()
    calls = []

    def http(ok_me=True, ok_send=True):
        def f(method, url, headers, body=None):
            calls.append((method, url.rsplit("/", 1)[1], body))
            good = ok_me if url.endswith("getMe") else ok_send
            return (200, {"ok": True}) if good else (401, {})
        return f
    assert [s for s, _ in sc.check_telegram({}, http())] == ["SKIP"] and calls == []
    assert [s for s, _ in sc.check_telegram({"TELEGRAM_BOT_TOKEN": TOKEN}, http())] == ["FAIL"]
    assert [s for s, _ in sc.check_telegram({"TELEGRAM_CHAT_ID": CHAT}, http())] == ["FAIL"]
    res = sc.check_telegram(ENV, http())
    assert [s for s, _ in res] == ["OK", "OK"] and [c[1] for c in calls] == ["getMe", "sendMessage"]
    assert calls[1][2]["chat_id"] == CHAT
    assert [s for s, _ in sc.check_telegram(ENV, http(ok_me=False))] == ["FAIL"]               # no test message with a bad token
    assert [s for s, _ in sc.check_telegram(ENV, http(ok_send=False))] == ["OK", "FAIL"]
    printed = " ".join(n for _, n in sc.check_telegram(ENV, http(ok_send=False)))
    assert TOKEN not in printed and CHAT not in printed
    full = sc.run_all({}, TEMPLATE, lambda *a, **k: (200, {}))                  # run_all includes the (skipped) telegram check
    assert ("SKIP", "telegram (optional, secrets TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID not set)") in full


def test_bot_tokens_are_redacted_and_the_template_config_matches_the_defaults():
    out = redact(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage failed", [])
    assert TOKEN not in out and "api.telegram.org/bot***/sendMessage" in out
    cfg = yaml.safe_load((TEMPLATE / "config" / "newsbot.yaml").read_text(encoding="utf-8"))
    assert cfg["notify"]["telegram"] == DEFAULTS["notify"]["telegram"]
    doc = (ROOT / "docs" / "FIRST_RUN.md").read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN" in doc and "TELEGRAM_CHAT_ID" in doc and "BotFather" in doc
