"""Compare writer models: python tools/eval_writers.py [--dry-run] [--brain DIR] [--stories FILE]
Runs the 10 saved stories through every configured model of write_bn and write_en and prints a table:
gate pass rate (deterministic gates; the qa_check LLM pass is skipped to save tokens), 'review' = passed but
sensitive/low-confidence, Bengali fluency flags (heuristic, a human should still read samples), tokens per
article. Real runs use your provider keys and spend free-tier tokens (NOT recorded in state/usage.json)."""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from newsbot.config import load_config                      # noqa: E402
from newsbot.evaluate import STORIES, format_table, load_stories, run_eval, targets_from_settings   # noqa: E402
from newsbot.llm import build_providers, load_llm_settings   # noqa: E402
from newsbot.llm.dryrun import dry_run_provider              # noqa: E402

TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "brain-template"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", default=str(TEMPLATE))
    ap.add_argument("--stories", default=str(STORIES))
    ap.add_argument("--dry-run", action="store_true", help="offline: mock provider, checks the plumbing only")
    args = ap.parse_args(argv)
    cfg = load_config(args.brain)
    stories = load_stories(args.stories)
    settings = load_llm_settings(args.brain)
    if args.dry_run:
        providers = {"mock": dry_run_provider()}
        targets = [("write_bn", "mock", "mock-strong"), ("write_en", "mock", "mock-strong")]
    else:
        providers = build_providers()
        targets = targets_from_settings(settings, providers)
        if not targets:
            print("no write model is usable: add at least one Cloudflare LLM account in the dashboard (page LLM)")
            return 3
    print(format_table(run_eval(cfg, args.brain, stories, targets, providers, settings.get("retry"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
