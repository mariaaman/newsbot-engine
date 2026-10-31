#!/usr/bin/env python3
"""Go-live scorecard for readiness.yml. Prints ONLY counts, status words and reason codes (the engine repo is public:
no titles, no article text, no keys). Usage: readiness.py <brain_dir> [--engine-dir .]

Inputs from the environment (all optional): STEP_OUTCOMES="tests=success,validate=failure,..." from the workflow steps,
BRAIN_REPO + GH_TOKEN (brain PR statistics), RUN_REPO + RUN_TOKEN (recent hourly runs).
Exit 0 = no blockers (ready for review mode), 1 = at least one blocker. The AUTO verdict is printed separately."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

NOW = datetime.now(timezone.utc)
rows: list[tuple[str, str, str]] = []          # (PASS|WARN|FAIL|INFO, name, detail)


def add(level: str, name: str, detail: str = "") -> None:
    rows.append((level, name, detail))


def front_matter(path: Path) -> dict:
    try:
        parts = path.read_text(encoding="utf-8").split("---", 2)
        return yaml.safe_load(parts[1]) or {} if len(parts) >= 3 else {}
    except (OSError, yaml.YAMLError):
        return {}


def gh_json(args: list[str], token: str | None) -> list | None:
    if not token:
        return None
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "GH_TOKEN": token})
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def check_steps() -> None:
    labels = {"tests": "full test suite (pytest)", "validate": "brain config validates",
              "health": "model IDs exist at providers", "llmtest": "live model answers",
              "dryrun": "dry-run pipeline on the real config", "build": "Hugo build with the brain content",
              "filecount": "site file count under the limit"}
    got = dict(p.split("=", 1) for p in os.environ.get("STEP_OUTCOMES", "").split(",") if "=" in p)
    for key, label in labels.items():
        o = got.get(key, "missing")
        soft = key in ("llmtest", "dryrun")      # a rate-limited probe or a fixture-only dry run is a warning, not a blocker
        add("PASS" if o == "success" else ("INFO" if o == "skipped" else ("WARN" if soft else "FAIL")), label, o)


def check_brain(brain: Path, engine: Path) -> dict:
    cfg = yaml.safe_load((brain / "config" / "newsbot.yaml").read_text(encoding="utf-8")) or {}
    site, ads, mode = cfg.get("site", {}), cfg.get("ads", {}), (cfg.get("publish") or {}).get("mode")
    langs = site.get("languages") or ["bn", "en"]
    add("PASS" if mode == "review" else "INFO", "publish.mode in brain config", str(mode))
    base = str(site.get("base_url", ""))
    add("PASS" if base.startswith("https://") and "example" not in base else "FAIL", "site.base_url is a real https URL")
    add("PASS" if site.get("name") and "example" not in str(site.get("name")).lower() else "FAIL", "site.name set")
    network = str(ads.get("network") or "adsense").lower()
    add("INFO", "ads", f"enabled ({network})" if ads.get("enabled") else "disabled (turn on only after your ad network approves the site)")
    if ads.get("enabled"):
        if network == "adsterra":
            add("PASS" if (ads.get("scripts") or ads.get("slots")) else "FAIL", "ads.enabled needs ads.scripts or ads.slots (your ad code)")
            add("PASS" if ads.get("txt_lines") else "WARN", "ads.txt_lines has the line from the ad network dashboard")
        elif not str(ads.get("publisher_id", "")).startswith("pub-"):
            add("FAIL", "ads.enabled needs ads.publisher_id (pub-...)")
    # trust pages: the brain override wins over the engine's shipped page
    left = 0
    for lang in langs:
        for page in ("about", "ads", "contact", "corrections", "editorial", "privacy", "terms"):
            f = brain / "content" / lang / "pages" / f"{page}.md"
            f = f if f.exists() else engine / "site" / "content" / lang / "pages" / f"{page}.md"
            if f.exists():
                left += len(re.findall(r"\[OWNER", f.read_text(encoding="utf-8")))
    add("PASS" if left == 0 else "FAIL", "trust pages have no [OWNER: ...] placeholder left", f"{left} left")
    # articles and pending
    published = {l: len([p for p in (brain / "content" / l).glob("*.md")]) for l in langs}
    add("INFO", "published articles", ", ".join(f"{l}={n}" for l, n in published.items()))
    reasons, pend = Counter(), 0
    for l in langs:
        for p in (brain / "pending" / l).glob("*.md"):
            pend += 1
            fm = (front_matter(p).get("pending") or {})
            reasons.update(fm.get("failures") or [])
            reasons.update(fm.get("review") or [])
    add("INFO", "pending drafts waiting for a human", str(pend))
    if reasons:
        add("INFO", "pending reasons", ", ".join(f"{k}={v}" for k, v in reasons.most_common(8)))
    # state size
    dec = brain / "state" / "decisions.jsonl"
    mb = dec.stat().st_size / 1e6 if dec.exists() else 0
    add("PASS" if mb < 5 else "WARN", "decisions.jsonl size", f"{mb:.1f} MB")
    dep = brain / "state" / "deployed.json"
    if dep.exists():
        try:
            age = NOW - datetime.fromisoformat(json.loads(dep.read_text())["deployed_at"])
            add("PASS" if age < timedelta(days=7) else "WARN", "last deploy age", f"{age.days} d {age.seconds // 3600} h")
        except (ValueError, KeyError):
            add("WARN", "deployed.json readable")
    else:
        add("WARN", "site never deployed (no state/deployed.json)")
    # model budgets used today
    use = brain / "state" / "usage.json"
    if use.exists():
        try:
            c = json.loads(use.read_text()).get("counters", {})
            add("INFO", "tokens used today", ", ".join(f"{k.split('/')[0]}={v.get('tokens', 0)}" for k, v in sorted(c.items())))
        except ValueError:
            pass
    return {"published": sum(published.values()), "left": left, "pending": pend}


def check_actions() -> dict:
    out = {"recent_ok": None}
    runs = gh_json(["run", "list", "--repo", os.environ.get("RUN_REPO", ""), "--workflow", "hourly.yml", "--limit", "20",
                    "--json", "conclusion,status"], os.environ.get("RUN_TOKEN"))
    if runs is None:
        add("WARN", "recent hourly runs", "not readable")
    else:
        done = [r for r in runs if r.get("status") == "completed"]
        ok = sum(r.get("conclusion") == "success" for r in done)
        out["recent_ok"] = (ok, len(done), bool(done) and done[0].get("conclusion") == "success")
        add("PASS" if done and done[0].get("conclusion") == "success" else "FAIL",
            "latest hourly run succeeded", f"{ok}/{len(done)} of the last runs ok")
    prs = gh_json(["pr", "list", "--repo", os.environ.get("BRAIN_REPO", ""), "--state", "all", "--limit", "100",
                   "--json", "state,mergedAt,closedAt,title"], os.environ.get("GH_TOKEN"))
    if prs is None:
        add("WARN", "brain pull request statistics", "not readable")
    else:
        rev = [p for p in prs if str(p.get("title", "")).startswith("Review:")]
        merged = sum(p.get("state") == "MERGED" for p in rev)
        closed = sum(p.get("state") == "CLOSED" for p in rev)
        out["merged"], out["closed"] = merged, closed
        add("INFO", "review pull requests", f"merged={merged} closed_unmerged={closed} open={len(rev) - merged - closed}")
    return out


def main(argv: list[str]) -> int:
    brain = Path(argv[1] if len(argv) > 1 else "brain")
    engine = Path(argv[argv.index("--engine-dir") + 1]) if "--engine-dir" in argv else Path(".")
    check_steps()
    b = check_brain(brain, engine)
    a = check_actions()
    width = max(len(n) for _, n, _ in rows)
    for level, name, detail in rows:
        print(f"{level:4}  {name.ljust(width)}  {detail}")
    blockers = [n for l, n, _ in rows if l == "FAIL"]
    print()
    print("REVIEW-MODE VERDICT:", "READY" if not blockers else f"NOT READY ({len(blockers)} blocker(s))")
    for n in blockers:
        print("  blocker:", n)
    merged, closed = a.get("merged", 0), a.get("closed", 0)
    rate = merged / (merged + closed) if merged + closed else 0
    auto = (not blockers and merged >= 20 and rate >= 0.9 and bool(a.get("recent_ok")) and a["recent_ok"][2])
    print("AUTO-MODE VERDICT:", "READY" if auto else "NOT YET",
          f"(needs >=20 merged review PRs with >=90% approval; now merged={merged} rate={rate:.0%})")
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
