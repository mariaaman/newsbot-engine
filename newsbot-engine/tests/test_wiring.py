import tempfile
from pathlib import Path

from newsbot.cli import run_pipeline
from tests.helpers import NOW, make_cfg, mock_embedder


def test_dry_run_pipeline_end_to_end_without_state_writes():
    with tempfile.TemporaryDirectory() as d:
        r = run_pipeline(make_cfg(), "dry-run", Path(d), now=NOW)
        assert r["mode"] == "dry-run" and r["NEW"] == 2 and r["SKIP"] >= 4 and r["stories"] >= 6
        assert not (Path(d) / "state").exists()          # dry-run never writes state


def test_review_mode_logs_decisions_ids_and_numbers_only():
    with tempfile.TemporaryDirectory() as d:
        # review mode would use the network; inject nothing and check dry path by fixtures instead
        import newsbot.discover as disc
        orig = disc.http_fetcher
        disc.http_fetcher = lambda: disc.fixture_fetcher(
            Path(__file__).parent / "fixtures" / "feeds", make_cfg().sources)
        try:
            r = run_pipeline(make_cfg(), "review", Path(d), now=NOW, embedder=mock_embedder)
        finally:
            disc.http_fetcher = orig
        log = (Path(d) / "state" / "decisions.jsonl").read_text(encoding="utf-8")
        assert r["NEW"] == 2 and log.count("\n") <= len(r["decisions"]) + 1
        assert "Barun" not in log and "http" not in log   # no titles or URLs in the log
