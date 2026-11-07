"""Leftover claims that name people absent from the grouped claims (another event) must not come back as facts."""
from __future__ import annotations


def _build(claim_texts, grouped_ids):
    from newsbot.models import Claim, Source
    from newsbot.verify import build_fact_sheet
    srcs = [Source("S1", "A", 2, "https://a.com/1", "a.com", "en", "2026-10-20T00:00:00+00:00"),
            Source("S2", "B", 2, "https://b.com/1", "b.com", "es", "2026-10-20T00:00:00+00:00")]
    cl = [Claim(f"S{1 if i < 2 else 2}.C{i}", f"S{1 if i < 2 else 2}", "what", t) for i, t in enumerate(claim_texts)]

    class LLM:
        def complete(self, task, msgs, schema=None, max_tokens=0):
            return schema({"facts": [{"statement": "Tom Skerritt, the actor, died at 93.",
                                      "claim_ids": grouped_ids, "kind": "what"}]})
    return build_fact_sheet(LLM(), None, "st-x", srcs, cl)


def test_roundup_lines_about_other_people_are_dropped_but_same_story_leftovers_stay():
    texts = ["Tom Skerritt, the actor, died at the age of 93 on Wednesday",
             "Tom Skerritt, the actor, died at the age of 93 on Wednesday",
             "RJ Mitte, actor, murio a los 34 anos, hijo de Walter White en Breaking Bad",
             "Arnaud Denis, actor, murio a los 43 anos tras recurrir a la eutanasia",
             "La familia de Tom Skerritt informo su fallecimiento en un comunicado",
             "The funeral will be held privately for close friends and relatives"]
    fs = _build(texts, ["S1.C0", "S1.C1"])
    joined = " | ".join(f.statement for f in fs.facts)
    assert "Mitte" not in joined and "Denis" not in joined
    assert "Skerritt informo" in joined and "funeral will be held" in joined      # same story / no names: kept


def test_scripts_without_capitals_are_never_filtered_by_the_name_guard():
    from newsbot.verify import _names, _off_topic
    assert _names("রাজশাহী শহরে প্রধানমন্ত্রী একটি নতুন সেতু উদ্বোধন করেছেন") == set()
    assert not _off_topic("প্রধানমন্ত্রী একটি নতুন সেতু উদ্বোধন করেছেন", set(), "something else")
    assert _names("He met Minister Rahim in Dhaka.") == {"minister", "rahim", "dhaka"}
