"""Autofix: an unsupported figure is corrected or its sentence removed by the engine itself (no rewrite call)."""
import copy

from newsbot.articles import produce_articles
from newsbot.autofix import autofix_article
from newsbot.llm import build_llm, load_llm_settings
from newsbot.llm.dryrun import dry_run_provider
from newsbot.llm.mock import MockProvider
from newsbot.qa import run_gates
from tests.helpers import TEMPLATE, make_cfg
from tests.test_m4 import STORIES, base

FS = STORIES["ev-metro"]


def _mock(extra):
    calls = []

    def writer(msgs):
        art = copy.deepcopy(dry_run_provider().responses["write_en"](msgs))
        calls.append(any("QA REJECTED" in m["content"] for m in msgs if m["role"] == "user"))
        art["body_markdown"] = art["body_markdown"].replace("\n\n## ", f"\n\n{extra}\n\n## ", 1)
        return art
    return MockProvider({**dry_run_provider().responses, "write_en": writer}), calls


def _produce(extra, **qa):
    mock, calls = _mock(extra)
    llm = build_llm(load_llm_settings(TEMPLATE), None, dry_run=True, mock=mock)
    cfg = make_cfg(qa={"max_repair_attempts": 0, **qa})
    return produce_articles(FS, ["en"], cfg, None, llm, recent_by_lang={"en": []})["en"], calls


def test_an_invented_name_is_cut_out_without_asking_the_model_again():
    r, calls = _produce("Zorblax Quimby opened the line [S1]. The metro rail extension opened to passengers on Thursday [S1].")
    assert calls == [False]                                              # one write, no rewrite
    assert "Zorblax" not in r.article["body_markdown"] and not r.qa.failures
    assert "extension opened to passengers" in r.article["body_markdown"]   # the true sentence stays


def test_a_wrong_figure_is_replaced_by_the_one_the_fact_sheet_gives():
    r, calls = _produce("The new section is 4.2 km long and has two stations [S1].")
    body = r.article["body_markdown"]
    assert calls == [False] and not r.qa.failures
    assert "4.2" not in body and "3.9 km long" in body                     # corrected, not deleted


def test_a_figure_with_no_clear_answer_is_removed_not_guessed():
    r, calls = _produce("Opposition leaders said 9,000 people marched on the ministry [S1].")
    assert calls == [False] and "9,000" not in r.article["body_markdown"] and not r.qa.failures


def test_autofix_off_leaves_the_failure_for_a_human():
    r, _ = _produce("Opposition leaders said 9,000 people marched on the ministry [S1].", autofix=False)
    assert r.status == "pending" and "unsupported_number" in r.qa.failures


def test_a_citation_is_kept_when_the_last_sentence_of_a_paragraph_is_removed():
    art = copy.deepcopy(base())
    art["body_markdown"] = art["body_markdown"].replace(
        "\n\n## ", "\n\nThe line opened on Thursday. Zorblax Quimby cut the ribbon. [S1][S2]\n\n## ", 1)
    qa = run_gates(art, FS, "en", make_cfg(), recent=[], run_llm_check=False)
    out, fixed, dropped = autofix_article(art, FS, "en", qa)
    assert dropped == 1 and "Zorblax" not in out["body_markdown"] and "opened on Thursday. [S1] [S2]" in out["body_markdown"]


def test_a_draft_that_would_lose_most_of_its_body_goes_to_a_human_instead():
    art = copy.deepcopy(base())
    art["body_markdown"] = "## What happened\n\nZorblax Quimby opened it [S1].\n\nZorblax Quimby closed it [S1].\n\nZorblax Quimby left [S1]."
    qa = run_gates(art, FS, "en", make_cfg(), recent=[], run_llm_check=False)
    assert autofix_article(art, FS, "en", qa) is None


def test_a_language_without_a_name_check_loses_nothing_over_names():
    art = copy.deepcopy(base())
    art["body_markdown"] = art["body_markdown"].replace("\n\n## ", "\n\nZorblax Quimby opened it. [S1]\n\n## ", 1)
    qa = run_gates(art, FS, "en", make_cfg(), recent=[], run_llm_check=False)
    qa.failures[:] = ["unsupported_number"]                     # pretend only a figure was rejected (the name gate is off for this language)
    assert autofix_article(art, FS, "en", qa) is None            # nothing to fix: the name sentence stays


def test_numbers_written_with_spaces_or_decimal_commas_match_the_fact_sheet():
    from newsbot.extract import numbers_in
    assert numbers_in("405 000 votes") == numbers_in("405,000") == {"405000"}
    assert numbers_in("785.000") >= {"785000"} and numbers_in("785 000") <= numbers_in("785.000")   # Spanish fact sheet, spaced article
    assert numbers_in("6,9 %") == numbers_in("6.9 percent") == {"6.9"}
    assert numbers_in("1.234,56") == numbers_in("1,234.56") == {"1234.56"}
    assert numbers_in("12 May 2026") == {"12", "2026"}


def test_us_is_not_an_invented_name_when_the_fact_sheet_says_united_states():
    from newsbot.qa import unsupported_names
    assert unsupported_names("Voters in the US went to the polls.", {"voters", "in", "the", "went", "to", "polls", "united", "states"}) == []
    assert unsupported_names("Voters in the US went to the polls.", {"voters", "in", "the", "went", "to", "polls"}) == ["US"]


def test_real_run_false_alarms_are_not_failures():
    from newsbot.qa import BANNED, extra_numbers, lead_numbers, unsupported_names
    # Spanish "todo" and AI news saying "language model" are ordinary text
    assert "todo" not in BANNED and "language model" not in BANNED
    # "$21 billion" when the facts carry 21 and 4 (the unit spelled another way)
    assert extra_numbers({"21000000000", "4000000000", "2500000"}, {"21", "4", "2.5"}) == set()
    assert extra_numbers({"21000"}, {"21"}) == {"21000"}                  # thousands are not accepted this way
    assert extra_numbers({"21000000000"}, {"22"}) == {"21000000000"}
    # 12-hour clock in the lead, 24-hour clock in a translation
    lead = {"title": "x", "summary": "", "body_markdown": "It began at 5 p.m. and ended at 8 p.m. [S1]", "timeline": [], "confirmed": [], "unclear": [], "disputed": []}
    assert {"17", "20"} <= lead_numbers(lead)
    assert extra_numbers({"17", "20"}, set(), lead) == set()
    # names: sentence starters, nationality / party adjectives, Europe vs European, US / EU / U.S.
    v = {"report"}
    for text in ("With Republicans out of both chambers, little changed.", "Neither Rahman nor aides spoke.", "Large cyber attacks rose.",
                 "The Turkish and Iranian sides met.", "A Democratic majority formed.", "The EU and the U.K. agreed."):
        assert unsupported_names(text, v | {"republicans", "rahman", "aides", "spoke", "cyber"}) == [], text
    assert unsupported_names("Leaders in Europe met.", {"european", "leaders", "met"}) == []
    assert unsupported_names("Voters met in West Palm Beach.", {"voters", "met"}) != []     # a real unsupported place is still caught


def test_autofix_shortens_a_long_headline_and_drops_unresolved_markers():
    from newsbot.autofix import shorten_title, autofix_article
    t = "La Chambre des représentants envisage la destitution du secrétaire à la Défense: un vote est attendu après la pause du Congrès"
    short = shorten_title(t, 20, 100)
    assert short and 20 <= len(short) <= 100 and t.startswith(short.rstrip(": ,"))
    assert shorten_title("x" * 30, 40, 100) is None                      # too short to fix by cutting
    art = copy.deepcopy(base())
    art["body_markdown"] = art["body_markdown"].replace("[S1]", "[S9]", 1)
    qa = run_gates(art, FS, "en", make_cfg(), run_llm_check=False)
    assert "unresolved_marker" in qa.failures
    res = autofix_article(art, FS, "en", qa)
    assert res and "[S9]" not in res[0]["body_markdown"]
    assert "unresolved_marker" not in run_gates(res[0], FS, "en", make_cfg(), run_llm_check=False).failures


def test_a_draft_that_cannot_be_fixed_is_archived_in_rejected_not_left_pending(tmp_path):
    from newsbot.articles import write_outputs
    from newsbot.qa import QAResult
    pub = type("P", (), {"on_failure": "reject", "on_sensitive": "reject", "on_review": "publish"})()
    q = QAResult(); q.failures = ["unsupported_name"]
    assert q.route(pub) == ("pending", "rejected")
    q = QAResult(); q.review = ["sensitive:crime"]
    assert q.route(pub) == ("pending", "rejected")
    q = QAResult(); q.review = ["low_confidence"]
    assert q.route(pub) == ("publish", "")
    assert QAResult().route(pub) == ("publish", "")
    assert q.route(type("P", (), {})()) == ("pending", "pending")        # settings absent: the old, safe behaviour
    # the file goes to rejected/<lang>/
    cfg = make_cfg(publish={"mode": "auto", "on_failure": "reject"})
    mock, _ = _mock("Zorblax Quimby opened the line [S1].")
    llm = build_llm(load_llm_settings(TEMPLATE), None, dry_run=True, mock=mock)
    cfg = make_cfg(qa={"max_repair_attempts": 0, "autofix": False}, publish={"mode": "auto", "on_failure": "reject"})
    r = produce_articles(FS, ["en"], cfg, None, llm, recent_by_lang={"en": []})["en"]
    assert r.status == "pending" and r.hold == "rejected"
    paths = write_outputs(tmp_path, FS, {"en": r})
    assert paths[0].startswith("rejected/en/") and (tmp_path / paths[0]).is_file() and not (tmp_path / "pending").exists()
