"""Run diagnostics: public logs get codes/counts/timings; names, figures and claims go only to the private brain."""
import copy
import json
import logging
import shutil
import tempfile
from pathlib import Path

from newsbot.cli import FIXTURES, run_pipeline
from newsbot.diag import DIAG, Diag, detail_counts
from newsbot.discover import fixture_fetcher
from newsbot.fetch import fixture_article_fetcher
from newsbot.llm import build_llm, load_llm_settings
from newsbot.llm.dryrun import dry_run_provider
from tests.helpers import NOW, TEMPLATE, make_cfg


def test_detail_counts_never_contains_text():
    s = detail_counts({"unsupported_number": {"numbers": ["2014", "Zorblax"], "n": 2}, "min_words": {"words": 120},
                       "unsupported_claims": {"unsupported": [{"claim": "Quimby opened it"}]}, "title_length": 4,
                       "quote": {"text": "secret words"}, "ngram_overlap_max": 0.31})
    assert s == "unsupported_number.numbers.2.n.2|min_words.words.120|unsupported_claims.unsupported.1|title_length.n.4|quote|ngram_overlap_max.n.0.31"
    assert "Zorblax" not in s and "Quimby" not in s and "secret" not in s


def test_report_has_every_stage_and_marks_failures():
    d = Diag()
    d.start("review", "2026-10-31T00:00:00")
    d.story = "st-1"
    d.add("fetch", domain="a.example", outcome="http_403")
    d.add("extract", source="S1", domain="a.example", claims=0, chars=300)
    d.add("llm", task="verify", provider="cf_a", model="m", ok=False, secs=60.0, reason="network_error_readtimeout")
    d.add("llm", task="verify", provider="cf_b", model="m", ok=True, tokens=900, secs=13.0, finish="stop")
    d.add("gate", lang="en", stage="draft", failures=["unsupported_number"], review=["sensitive:death"],
          offending={"names": ["Zorblax"], "numbers": ["2014"]})
    d.add("article", lang="en", status="pending")
    d.mark("facts")
    md = d.render_md()
    assert "## st-1" in md and "http_403" in md and "FAILED" in md and "network_error_readtimeout" in md
    assert "FAIL unsupported_number" in md and "Zorblax" in md and "review=sensitive:death" in md
    s = d.summary()
    assert s["llm"] == {"calls": 2, "ok": 1, "failed": 1, "tokens": 900, "secs": 73} and s["articles"] == {"st-1": {"en": "pending"}}
    assert "llm_calls=2" in d.public_summary() and "Zorblax" not in d.public_summary()


def test_inactive_diag_records_nothing_and_write_never_raises():
    d = Diag()
    d.add("fetch", domain="x")
    assert d.events == []
    d.start("review")
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "afile"
        f.write_text("x")
        d.write(f)                                  # state/diagnostics cannot be created under a file: swallowed
        d.write(None)


def test_runs_file_keeps_only_the_last_300_runs():
    with tempfile.TemporaryDirectory() as t:
        d = Diag()
        for i in range(305):
            d.start("review", f"run{i}")
            d.write(t)
        lines = (Path(t) / "state/diagnostics/runs.jsonl").read_text().splitlines()
        assert len(lines) == 300 and json.loads(lines[-1])["started"] == "run304" and json.loads(lines[0])["started"] == "run5"


def _real_run(writer=None):
    cfg = make_cfg()
    mock = dry_run_provider()
    if writer:
        mock.responses["write_en"] = writer
        mock.responses["write_bn"] = writer
    llm = build_llm(load_llm_settings(TEMPLATE), None, dry_run=True, mock=mock)
    d = tempfile.mkdtemp()
    brain = Path(d) / "brain"
    shutil.copytree(TEMPLATE, brain)
    return run_pipeline(cfg, "review", brain, now=NOW, embedder=None, llm=llm,
                        article_fetcher=fixture_article_fetcher(FIXTURES / "articles"),
                        feed_fetcher=fixture_fetcher(FIXTURES / "feeds", cfg.sources)), brain


def test_a_real_run_writes_the_private_report_and_keeps_names_out_of_the_public_log():
    good = dry_run_provider().responses["write_en"]

    def bad(msgs):
        art = copy.deepcopy(good(msgs))
        art["body_markdown"] = art["body_markdown"].replace("\n\n## ", "\n\nZorblax Quimby opened it in 2014 [S1].\n\n## ", 1)
        return art
    records: list[str] = []

    class H(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())
    h = H()
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)
    try:
        r, brain = _real_run(bad)
    finally:
        logging.getLogger().removeHandler(h)
    DIAG.write(brain)
    md = (brain / "state/diagnostics/last_run.md").read_text(encoding="utf-8")
    for needle in ("**fetch**", "**extract**", "**verify**", "**llm**", "**gate**", "**article**", "offending"):
        assert needle in md, needle
    assert "Zorblax" in md and "2014" in md                              # private detail is there ...
    public = "\n".join(records)
    assert "Zorblax" not in public and "Quimby" not in public            # ... and absent from the public log
    assert "run summary" in public and "gate story=" in public and "verdict=" in public
    assert (brain / "state/diagnostics/runs.jsonl").is_file()
