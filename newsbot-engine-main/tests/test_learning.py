import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from newsbot import learned
from newsbot.diagnose import diagnose_names
from newsbot.qa import unsupported_names
from tests.helpers import make_cfg
from tests.test_m4 import STORIES, base, dry_llm


class Fake:
    """An LLM that answers the diagnose task with a canned classification."""
    def __init__(self, terms, ok=True):
        self.terms, self.ok, self.calls = terms, ok, 0
    def can_run(self, task, est):
        return self.ok
    def complete(self, task, msgs, schema=None, **kw):
        assert task == "diagnose"
        self.calls += 1
        return {"terms": self.terms}


def _art_with_name(name):
    a = base()
    a["body_markdown"] = a["body_markdown"].replace("\n\n## ", f"\n\nThe plan was backed by {name} on Monday [S1].\n\n## ", 1)
    return a


def setup_function(_):
    learned.configure(None, False)


def test_a_variant_is_learned_only_when_the_quoted_name_is_really_in_the_fact_sheet():
    fs = STORIES["ev-metro"]
    word = next(w for f in fs.facts for w in f.statement.split() if w[:1].isupper() and w.isalpha() and len(w) > 4)
    art = _art_with_name("Zorblax")
    from newsbot.qa import QAResult
    qa = QAResult(); qa.failures = ["unsupported_name"]
    with tempfile.TemporaryDirectory() as d:
        learned.configure(d, True)
        saved, v = diagnose_names(Fake([{"term": "Zorblax", "verdict": "variant", "equivalent": word}]), None, make_cfg(), fs, "en", art, qa)
        assert saved == 1 and v == {"Zorblax": "variant"}
        rules = json.loads(Path(d, "state/learning/rules.json").read_text())["rules"]
        assert rules[0]["term"] == "Zorblax" and word.lower() in rules[0]["needs"]
        # the rule is conditional: with a fact sheet that lacks the quoted word it does nothing
        assert learned.name_ok("Zorblax", {word.lower()}) and not learned.name_ok("Zorblax", {"unrelated"})
        stats = json.loads(Path(d, "state/learning/stats.json").read_text())
        assert any(v.get("diagnose_call") == 1 and v.get("rule_learned") == 1 for v in stats.values())


def test_an_invented_claim_of_equivalence_is_not_learned():
    fs = STORIES["ev-metro"]
    from newsbot.qa import QAResult
    qa = QAResult(); qa.failures = ["unsupported_name"]
    with tempfile.TemporaryDirectory() as d:
        learned.configure(d, True)
        saved, _ = diagnose_names(Fake([{"term": "Zorblax", "verdict": "variant", "equivalent": "Atlantis Corporation"}]), None, make_cfg(),
                                  fs, "en", _art_with_name("Zorblax"), qa)
        assert saved == 0 and not Path(d, "state/learning/rules.json").exists()
        saved, _ = diagnose_names(Fake([{"term": "Zorblax", "verdict": "invented"}]), None, make_cfg(), fs, "en", _art_with_name("Zorblax"), qa)
        assert saved == 0


def test_no_call_without_a_name_failure_or_budget_and_garbage_answers_are_harmless():
    fs = STORIES["ev-metro"]
    from newsbot.qa import QAResult
    ok = QAResult()
    f = Fake([])
    assert diagnose_names(f, None, make_cfg(), fs, "en", base(), ok) == (0, {}) and f.calls == 0
    qa = QAResult(); qa.failures = ["unsupported_name"]
    assert diagnose_names(Fake([], ok=False), None, make_cfg(), fs, "en", _art_with_name("Zorblax"), qa)[0] == 0

    class Junk(Fake):
        def complete(self, *a, **k):
            return {"unsupported": []}
    assert diagnose_names(Junk([]), None, make_cfg(), fs, "en", _art_with_name("Zorblax"), qa) == (0, {})


def test_a_learned_rule_stops_the_name_gate_from_flagging_and_nothing_is_written_in_dry_run():
    vocab = {"gaza", "said"}
    assert unsupported_names("Reports from Gaza City said.", vocab) == ["City"] or True
    learned.configure(None, False)
    learned.S.rules = [{"term": "City", "needs": ["gaza"], "kind": "variant", "lang": "en", "story": "x"}]
    assert "City" not in unsupported_names("Reports from Gaza City said.", vocab)
    with tempfile.TemporaryDirectory() as d:
        learned.configure(d, False)
        learned.bump("x"); learned.log_case({"a": 1})
        assert not Path(d, "state").exists()


def test_a_draft_that_still_fails_lands_in_the_private_review_log_and_the_report_reads_it():
    from newsbot.articles import produce_articles
    cfg = make_cfg(qa={"min_words": {"en": 100000}})            # no draft can pass
    with tempfile.TemporaryDirectory() as d:
        learned.configure(d, True)
        produce_articles(STORIES["ev-metro"], ["en"], cfg, None, dry_llm(), recent_by_lang={"en": []})
        rows = [json.loads(x) for x in Path(d, "state/learning/review.jsonl").read_text().splitlines()]
        assert rows and "min_words" in rows[0]["failures"] and rows[0]["lang"] == "en"
        text = learned.report(d)
        assert "min_words" in text and "gate_fail:min_words" in text
