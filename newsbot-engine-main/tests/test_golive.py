"""Go-live fixes: name gate, one repair attempt, decisions log size, readiness scorecard, helper workflows."""
import copy
import json
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from newsbot.articles import produce_articles
from newsbot.llm import build_llm, load_llm_settings
from newsbot.llm.dryrun import dry_run_provider
from newsbot.llm.mock import MockProvider
from newsbot.qa import offending_terms, unsupported_names
from newsbot.state import NOISY_SKIP, append_decisions, compact_decisions
from tests.helpers import NOW, ROOT, TEMPLATE, make_cfg
from tests.test_m4 import STORIES, base
from tests.test_workflows import WF_DIR, load, steps

CFG = make_cfg()


# ---- name gate
def test_title_case_and_office_words_are_not_names_but_real_names_still_are():
    vocab = {"japan", "prices"}
    text = "Japan Prices Exceed Target\nThe Senator said prices exceed the target. Katharina Reiche spoke later."
    assert unsupported_names(text, vocab) == ["Katharina", "Reiche"]


def test_hyphen_compounds_are_judged_by_their_parts_or_as_a_whole():
    assert unsupported_names("Fans of Paris Saint\u2010Germain cheered.", {"paris", "saint-germain", "fans", "of", "cheered"}) == []
    assert unsupported_names("A Detroit\u2010born actor died.", {"actor", "died"}) == ["Detroit"]


# ---- one repair attempt
def _mock(first_extra, second_extra=""):
    calls = []

    def writer(msgs):
        art = copy.deepcopy(dry_run_provider().responses["write_en"](msgs))
        repair = any("QA REJECTED" in m["content"] for m in msgs if m["role"] == "user")
        calls.append(repair)
        extra = second_extra if repair else first_extra
        if extra:
            art["body_markdown"] = art["body_markdown"].replace("\n\n## ", f"\n\n{extra}\n\n## ", 1)
        return art
    return MockProvider({**dry_run_provider().responses, "write_en": writer}), calls


def _run(mock):
    llm = build_llm(load_llm_settings(TEMPLATE), None, dry_run=True, mock=mock)
    return produce_articles(STORIES["ev-metro"], ["en"], CFG, None, llm, recent_by_lang={"en": []})["en"]


def test_repair_cures_an_invented_name_once():
    mock, calls = _mock("Zorblax Quimby opened the line [S1].")
    r = _run(mock)
    assert calls == [False, True] and r.status in ("publish", "pending") and "unsupported_name" not in r.qa.failures
    assert "Zorblax" not in r.article["body_markdown"]


def test_failed_repair_keeps_the_gate_closed_and_asks_at_most_max_repair_attempts_times():
    mock, calls = _mock("Zorblax Quimby opened the line [S1].", "Zorblax Quimby opened the line [S1].")
    r = _run(mock)
    assert calls == [False, True, True] and "unsupported_name" in r.qa.failures and r.status == "pending"   # 1 write + 2 repairs
    off = make_cfg(qa={"max_repair_attempts": 0})
    mock, calls = _mock("Zorblax Quimby opened the line [S1].")
    llm = build_llm(load_llm_settings(TEMPLATE), None, dry_run=True, mock=mock)
    r = produce_articles(STORIES["ev-metro"], ["en"], off, None, llm, recent_by_lang={"en": []})["en"]
    assert calls == [False] and r.status == "pending"                                                       # repair switched off


def test_offending_terms_lists_names_and_numbers():
    art = copy.deepcopy(base())
    art["body_markdown"] += "\n\nOfficials said Zorblax counted 98765 people [S1]."
    names, nums = offending_terms(art, STORIES["ev-metro"])
    assert "Zorblax" in names and "98765" in nums


# ---- decisions log
def test_decisions_log_stays_small_and_old_files_are_compacted():
    with tempfile.TemporaryDirectory() as d:
        noisy = sorted(NOISY_SKIP)[0]
        append_decisions(d, NOW, [{"action": "SKIP", "reason": noisy, "story_id": f"st-{i}"} for i in range(500)]
                         + [{"action": "NEW", "reason": "x", "story_id": "st-new"}])
        lines = (Path(d) / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2 and json.loads(lines[1])["counts"] == {noisy: 500}
        old = (NOW - timedelta(days=9)).isoformat()
        p = Path(d) / "decisions.jsonl"
        p.write_text("\n".join(json.dumps(x) for x in (
            {"ts": old, "action": "SKIP", "reason": "blocked_topic"}, {"ts": old, "action": "SKIP", "reason": noisy},
            {"ts": old, "action": "NEW", "reason": "x"}, {"ts": NOW.isoformat(), "action": "SKIP", "reason": "blocked_topic"})) + "\n")
        compact_decisions(p, NOW)
        kept = [json.loads(l) for l in p.read_text().splitlines()]
        assert [(k["action"], k["reason"]) for k in kept] == [("NEW", "x"), ("SKIP", "blocked_topic")]


# ---- readiness scorecard
def test_readiness_scorecard_prints_counts_only_and_blocks_on_missing_steps():
    with tempfile.TemporaryDirectory() as d:
        brain = Path(d)
        shutil_tree = __import__("shutil").copytree
        shutil_tree(TEMPLATE, brain, dirs_exist_ok=True)
        (brain / "state").mkdir(exist_ok=True)
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "readiness.py"), str(brain), "--engine-dir", str(ROOT)],
                           capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=60)
        assert r.returncode == 1 and "REVIEW-MODE VERDICT: NOT READY" in r.stdout and "AUTO-MODE VERDICT: NOT YET" in r.stdout
        assert "://" not in r.stdout          # counts and status words only, no URLs


# ---- helper workflows
def test_readiness_workflow_is_manual_read_only_and_scoped():
    wf = load(WF_DIR / "readiness.yml")
    assert list(wf["on"]) == ["workflow_dispatch"] and wf["permissions"] == {"contents": "read", "actions": "read"}
    assert not any(k for k in wf["jobs"]["check"].get("env", {}))
    assert any("scripts/readiness.py" in s.get("run", "") for s in steps(wf))
    for s in steps(wf):
        if s.get("id") in ("tests", "validate", "health", "llmtest", "dryrun", "build", "filecount"):
            assert s["continue-on-error"] is True          # every check runs; the scorecard decides


def test_unzip_helper_runs_only_for_root_zips_and_never_on_its_own_commit():
    wf = load(WF_DIR / "unzip.yml")
    assert wf["on"]["push"]["paths"] == ["*.zip"] and wf["permissions"] == {"contents": "write"}
    job = wf["jobs"]["unzip"]
    assert "Auto extracted" in job["if"] and job["timeout-minutes"] == 15
    assert ".git/*" in " ".join(s.get("run", "") for s in job["steps"])


# ---- Hugo templates: Go html/template refuses a tag that is opened in one {{ if }} branch and finished outside it
def test_no_html_tag_is_split_across_template_actions():
    import re
    bad = []
    for f in sorted((ROOT / "site" / "layouts").rglob("*.html")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"<[a-zA-Z][a-zA-Z0-9]*\s*\{\{", line) and not re.search(r"<[a-zA-Z][a-zA-Z0-9]*\s*\{\{[^}]*\}\}[^<>]*>", line):
                bad.append((f.name, n))          # "<h2{{ end }}" style: the tag is left open at the end of a branch
    assert not bad, bad
