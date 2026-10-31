"""Git-friendly state: JSON / JSONL files in brain/state. Atomic writes, stable key order.
Decisions log holds IDs and numbers only (the engine repo is public; keep logs clean)."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsbot.models import Story, iso, parse_ts


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_stories(state_dir: str | Path) -> dict[str, Story]:
    path = Path(state_dir) / "stories.jsonl"
    out: dict[str, Story] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                st = Story.from_dict(json.loads(line))
                out[st.story_id] = st
    return out


def save_stories(state_dir: str | Path, stories: dict[str, Story]) -> None:
    lines = [json.dumps(s.to_dict(), ensure_ascii=False, sort_keys=True)
             for s in sorted(stories.values(), key=lambda s: s.story_id)]
    _atomic_write(Path(state_dir) / "stories.jsonl", "\n".join(lines) + ("\n" if lines else ""))


DECISIONS_MAX_BYTES = 1_000_000        # the log is write-only (nothing reads it back); it must not grow with the feed volume
DECISIONS_KEEP_DAYS = 3
# Routine "nothing to do" reasons: thousands per day. They are written as ONE summary line per run (counts only).
NOISY_SKIP = {"too_few_trusted_sources", "below_threshold", "run_cap_reached", "no_language_with_enough_sources"}


def compact_decisions(path: Path, now: datetime, keep_days: int = DECISIONS_KEEP_DAYS) -> None:
    """Shrink the log: routine SKIP lines are dropped (their counts live in the per-run summary lines); other lines
    are kept for keep_days, except NEW / UPDATE lines (the audit trail of what was written), kept for 60 days.
    Damaged lines are dropped."""
    cutoff = iso(now - timedelta(days=keep_days))
    keep_all_after = iso(now - timedelta(days=60))
    kept: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            ts = str(d.get("ts", ""))
        except ValueError:
            continue
        if d.get("action") == "SKIP" and d.get("reason") in NOISY_SKIP:
            continue
        if ts >= cutoff or (d.get("action") in ("NEW", "UPDATE") and ts >= keep_all_after):
            kept.append(line)
    _atomic_write(path, "\n".join(kept) + ("\n" if kept else ""))


def append_decisions(state_dir: str | Path, now: datetime, decisions: list[dict]) -> None:
    path = Path(state_dir) / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > DECISIONS_MAX_BYTES:
        compact_decisions(path, now)
    noisy: dict[str, int] = {}
    with path.open("a", encoding="utf-8") as f:
        for d in decisions:
            if d.get("action") == "SKIP" and d.get("reason") in NOISY_SKIP:
                noisy[d["reason"]] = noisy.get(d["reason"], 0) + 1
                continue
            f.write(json.dumps({"ts": iso(now), **d}, ensure_ascii=False, sort_keys=True) + "\n")
        if noisy:
            f.write(json.dumps({"ts": iso(now), "action": "SKIP_SUMMARY", "counts": noisy}, sort_keys=True) + "\n")


def load_cooldowns(state_dir: str | Path) -> dict[str, datetime]:
    """story_id -> time until which the story is not fetched again (insufficient_sources). IDs and times only."""
    path = Path(state_dir) / "cooldowns.json"
    if not path.exists():
        return {}
    try:
        return {k: parse_ts(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
    except (ValueError, TypeError, AttributeError):
        return {}                          # a damaged file only means one extra fetch


def save_cooldowns(state_dir: str | Path, cooldowns: dict[str, datetime], now: datetime) -> None:
    live = {k: iso(v) for k, v in sorted(cooldowns.items()) if v > now}       # expired entries are pruned
    path = Path(state_dir) / "cooldowns.json"
    if live or path.exists():
        _atomic_write(path, json.dumps(live, sort_keys=True) + "\n")


def local_day_start(now: datetime, offset_hours: float) -> datetime:
    """Start of the site's local day, as an aware UTC datetime."""
    tz = timezone(timedelta(hours=offset_hours))
    local = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def _version_counts_today(v: dict, day_start: datetime) -> bool:
    state = v.get("state", "published")         # missing state = published (M1-era data)
    if state in ("awaiting_review", "pending"):  # drafts also use up the daily cap, or an unreviewed PR queue is unbounded
        return True
    if state == "rejected":
        return False
    return bool(v.get("published_at")) and parse_ts(v["published_at"]) >= day_start


def new_today(stories: dict[str, Story], day_start: datetime) -> int:
    return sum(1 for s in stories.values()
               if parse_ts(s.first_seen) >= day_start
               and any(_version_counts_today(v, day_start) for v in s.versions.values()))


def updates_today(story: Story, day_start: datetime) -> int:
    return sum(1 for u in story.updates if parse_ts(u) >= day_start)
