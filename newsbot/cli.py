"""CLI: python -m newsbot run --mode auto|review|dry-run [--brain DIR]."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from typing import TYPE_CHECKING

from newsbot import __version__
from newsbot.logging_setup import setup_logging

if TYPE_CHECKING:
    from newsbot.config import Config

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
TEMPLATE = Path(__file__).resolve().parent.parent / "brain-template"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="newsbot")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the hourly pipeline once")
    run.add_argument("--mode", choices=["auto", "review", "dry-run"], default="dry-run")
    run.add_argument("--brain", default=os.environ.get("NEWSBOT_BRAIN_DIR", "brain"))
    run.add_argument("--log-level", default="INFO")
    run.add_argument("--report", default=os.environ.get("NEWSBOT_REPORT"),
                     help="write the step-by-step run report (JSON) here, for the notify step")
    val = sub.add_parser("validate", help="validate the brain config and exit")
    val.add_argument("--brain", default=os.environ.get("NEWSBOT_BRAIN_DIR", "brain"))
    h = sub.add_parser("health", help="verify configured LLM model IDs against the providers (needs keys)")
    h.add_argument("--brain", default=os.environ.get("NEWSBOT_BRAIN_DIR", "brain"))
    h.add_argument("--log-level", default="INFO")
    dn = sub.add_parser("deploy-needed", help="print deploy=true|false (site content/config changed since last deploy)")
    dn.add_argument("--brain", default=os.environ.get("NEWSBOT_BRAIN_DIR", "brain"))
    md = sub.add_parser("mark-deployed", help="record the content fingerprint after a successful deploy")
    md.add_argument("--brain", default=os.environ.get("NEWSBOT_BRAIN_DIR", "brain"))
    nt = sub.add_parser("notify", help="send the run report and review requests to Telegram (never fails the run)")
    nt.add_argument("--brain", default=os.environ.get("NEWSBOT_BRAIN_DIR", "brain"))
    nt.add_argument("--report", default=os.environ.get("NEWSBOT_REPORT"))
    nt.add_argument("--pr-url-file", default=os.environ.get("NEWSBOT_PR_URL_FILE"))
    nt.add_argument("--workflow", default="hourly")
    nt.add_argument("--log-level", default="INFO")
    return p


# The sample feeds and pages are dated around this moment; a dry-run run at the real time would find every story stale
# and skip it, which shows nothing. Real modes always use the real clock.
DRY_RUN_NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def run_pipeline(cfg: "Config", mode: str, brain: Path, now=None, embedder=None, llm=None,
                 article_fetcher=None, feed_fetcher=None, report=None) -> dict:
    """discover -> cluster -> score -> gate -> fetch -> extract -> verify (M3) -> write -> qa (M4). Later
    milestones add publish (commit/PR/deploy) and story-state updates. Dry-run reads fixtures, uses the mock LLM and never touches state."""
    from newsbot.cluster import cluster_items
    from newsbot.discover import discover, fixture_fetcher, http_fetcher
    from newsbot.gate import decide
    from newsbot.models import utcnow
    from newsbot.score import score_all
    from newsbot.state import append_decisions, load_stories, local_day_start, new_today

    log = logging.getLogger("newsbot.run")
    dry = mode == "dry-run"
    now = now or (DRY_RUN_NOW if dry else utcnow())     # dry-run lives on the fixtures' clock, or every sample story looks old
    effective = mode          # the CLI/workflow mode wins; publish.mode in config is only the default for the workflow
    log.info("run start mode=%s sources=%d", effective, len(cfg.sources))
    if embedder is None and not dry:   # Workers AI multilingual embeddings when credentials exist
        from newsbot.llm import build_embedder, load_llm_settings
        embedder = build_embedder(load_llm_settings(brain))
    fetch = feed_fetcher or (fixture_fetcher(FIXTURES / "feeds", cfg.sources) if dry else http_fetcher())
    items, trends, stats = discover(cfg, fetch)
    state_dir = brain / "state"
    if not dry:
        from newsbot.publish import reconcile
        reconcile(brain, now)
    known = {} if dry else load_stories(state_dir)
    clusters = cluster_items(items, cfg, embedder, known)
    scored = score_all(clusters, trends, cfg, now)
    if not dry:
        from newsbot.state import save_stories
        from newsbot.stories import refresh_statuses
        if any(refresh_statuses(known, scored, cfg, now).values()):     # last_news clock, stable / closed / reopened
            save_stories(state_dir, known)
    day_start = local_day_start(now, cfg.site.timezone_offset_hours)
    decisions = decide(scored, known, cfg, now, day_start, new_today(known, day_start))
    if not dry:
        append_decisions(state_dir, now, [d.log_dict() for d in decisions])
    todo = [d for d in decisions if d.action in ("NEW", "UPDATE") and not d.queued]
    if todo and llm is None:
        llm = build_run_llm(brain, mode, state_dir)
        if llm is None and report is not None:
            report.add("facts", "fail", "no LLM provider is usable (check the API keys)")
    factsheets = gather_story_facts(decisions, scored, cfg, brain, mode, llm, article_fetcher, state_dir, now) if llm else {}
    articles = write_story_articles(decisions, factsheets, cfg, brain, mode, llm, embedder, now)
    if not dry and articles:
        from newsbot.publish import record_new_stories
        record_new_stories(state_dir, decisions, {s.cluster.story_id: s.cluster for s in scored}, factsheets,
                           articles, now, effective)
    from newsbot.update import run_updates
    updates = run_updates(decisions, scored, cfg, brain, mode, llm, article_fetcher or _article_fetcher(mode), embedder, now) \
        if llm else {}
    n = {a: sum(d.action == a for d in decisions) for a in ("NEW", "UPDATE", "SKIP")}
    log.info("run done items=%d stories=%d new=%d update=%d skip=%d feeds_failed=%d", len(items),
             len(clusters), n["NEW"], n["UPDATE"], n["SKIP"], stats["feeds_failed"])
    if report is not None:
        from newsbot.report import record_run
        record_run(report, mode=effective, stats=stats, n_stories=len(clusters), decisions=decisions,
                   articles=articles, updates=updates)
    return {"mode": effective, "stories": len(clusters), "decisions": decisions, "factsheets": factsheets, "articles": articles,
            "updates": updates, "report": report, **n}


def build_run_llm(brain: Path, mode: str, state_dir):
    """Real LLM (keys from env) or the dry-run mock; None when no provider chain is usable."""
    from newsbot.llm import LLMError, build_llm, load_llm_settings
    log = logging.getLogger("newsbot.facts")
    try:
        if mode == "dry-run":
            from newsbot.llm.dryrun import dry_run_provider
            settings = load_llm_settings(brain) or load_llm_settings(TEMPLATE)   # dry-run works with no brain
            llm = build_llm(settings, None, dry_run=True, mock=dry_run_provider())
        else:
            llm = build_llm(load_llm_settings(brain), state_dir)
        llm.chain("extract")
        llm.chain("verify")
        return llm
    except LLMError as e:
        log.warning("llm unavailable, facts stage skipped: %s", e)
        return None


def _article_fetcher(mode: str):
    from newsbot.fetch import fixture_article_fetcher, http_fetcher as article_http
    return fixture_article_fetcher(FIXTURES / "articles") if mode == "dry-run" else article_http()


def gather_story_facts(decisions, scored, cfg, brain: Path, mode: str, llm, article_fetcher, state_dir, now=None) -> dict:
    """Stage 5-7 for NEW/UPDATE decisions. Hours with no NEW/UPDATE make zero LLM calls and no network."""
    from newsbot.facts import gather_facts, save_fact_sheet
    log = logging.getLogger("newsbot.facts")
    todo = [d for d in decisions if d.action == "NEW" and not d.queued]     # UPDATE has its own path (newsbot.update)
    if not todo or llm is None:
        return {}
    dry = mode == "dry-run"
    fetcher = article_fetcher or _article_fetcher(mode)
    by_id = {s.cluster.story_id: s.cluster for s in scored}
    out = {}
    from datetime import timedelta
    from newsbot.models import utcnow
    from newsbot.state import load_cooldowns, save_cooldowns
    now = now or utcnow()
    hours = cfg.limits.insufficient_sources_cooldown_hours
    cooldowns = {} if dry else load_cooldowns(state_dir)
    before = dict(cooldowns)
    for d in todo:
        if cooldowns.get(d.story_id, now) > now:        # could not be verified recently: no fetch, no LLM
            d.reason = f"{d.reason}|facts:cooldown"
            log.info("facts story=%s status=cooldown", d.story_id)
            continue
        fs, status = gather_facts(by_id[d.story_id], cfg, brain, llm, fetcher)
        if status == "insufficient_sources" and hours > 0:
            cooldowns[d.story_id] = now + timedelta(hours=hours)
        else:
            cooldowns.pop(d.story_id, None)
        d.reason = f"{d.reason}|facts:{status}"
        log.info("facts story=%s status=%s", d.story_id, status)
        if fs is not None:
            out[d.story_id] = fs
            if not dry:
                save_fact_sheet(state_dir, fs)
        if status == "queued_budget":
            d.queued = True
    if not dry and cooldowns != before:
        save_cooldowns(state_dir, cooldowns, now)
    return out


def write_story_articles(decisions, factsheets, cfg, brain: Path, mode: str, llm, embedder, now) -> dict:
    """Stage 8-9: one article per routed language, QA gates, then content/ or pending/ (never in dry-run)."""
    from newsbot.articles import produce_articles, write_outputs
    log = logging.getLogger("newsbot.articles")
    out = {}
    for d in decisions:
        fs = factsheets.get(d.story_id)
        if fs is None or llm is None or d.action != "NEW":      # UPDATE: newsbot.update.run_updates
            continue
        results = produce_articles(fs, d.route, cfg, brain, llm, embedder=embedder)
        if mode != "dry-run":
            write_outputs(brain, fs, results, now)
        for r in results.values():
            d.reason += f"|{r.lang}:{r.status}"
        out[d.story_id] = results
        log.info("story=%s articles=%s", d.story_id, ",".join(f"{l}:{r.status}" for l, r in results.items()))
    return out


def health_cmd(brain: Path) -> int:
    """Fail loudly (exit 3) if any configured model ID is missing at its provider. Prints IDs and status only."""
    from newsbot.llm import HealthCheckError, build_providers, check_models, load_llm_settings
    log = logging.getLogger("newsbot.health")
    settings = load_llm_settings(brain)
    try:
        results = check_models(settings, build_providers())
    except HealthCheckError as e:
        log.error("%s", e)
        return 3
    log.info("health OK models=%d", sum(r["status"] == "OK" for r in results))
    return 0


def _where(e: BaseException) -> str:
    """file:line of the innermost frame: safe to log (no data), enough to find the bug."""
    tb = e.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    return f"{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}" if tb is not None else "?"


def main(argv: list[str] | None = None) -> int:
    """Entry point. An unexpected error is logged as type + location only: the exception text and the
    traceback can echo source, draft or config text, and the workflow logs are public.
    Set NEWSBOT_DEBUG=1 on your own machine to get the normal traceback."""
    try:
        return _main(argv)
    except Exception as e:
        if os.environ.get("NEWSBOT_DEBUG") == "1":
            raise
        logging.getLogger("newsbot.cli").error("run failed: %s at %s", type(e).__name__, _where(e))
        return 1


def _main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(getattr(args, "log_level", "INFO"))
    log = logging.getLogger("newsbot.cli")
    brain = Path(args.brain)
    if args.cmd == "notify":                                  # stdlib only: works even if the engine's dependencies are missing
        from newsbot.notify import notify_cmd
        return notify_cmd(brain, args.report, args.pr_url_file, args.workflow)
    if args.cmd == "run" and args.mode == "dry-run" and not brain.is_dir():
        brain = TEMPLATE  # dry-run must work offline with no brain repo checked out
    if args.cmd == "health":
        return health_cmd(brain)
    if args.cmd in ("deploy-needed", "mark-deployed"):      # no config or pydantic needed
        from newsbot import publish
        if args.cmd == "deploy-needed":
            print(f"deploy={'true' if publish.deploy_needed(brain) else 'false'}")
        else:
            publish.mark_deployed(brain)
            log.info("deploy recorded")
        return 0
    from newsbot.models import utcnow
    from newsbot.report import Report, collect
    report = Report(getattr(args, "mode", ""), utcnow().isoformat()) if args.cmd == "run" else None
    try:
        from newsbot.config import load_config
        cfg = load_config(brain)
    except Exception as e:  # fail loudly, but never print config contents
        log.error("config invalid: %s", type(e).__name__)
        if report is not None:
            report.add("run", "error", f"config invalid: {type(e).__name__}")
            report.save(args.report)
        return 2
    if args.cmd == "validate":
        log.info("config OK sources=%d", len(cfg.sources))
        return 0
    try:
        with collect(report):
            run_pipeline(cfg, args.mode, brain, report=report)
    except Exception as e:
        report.add("run", "error", f"{type(e).__name__} at {_where(e)}")
        raise
    finally:
        report.save(args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
