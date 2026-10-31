"""Deterministic offline stand-in for the LLM so `--mode dry-run` exercises extract/verify with no keys.
It behaves like an honest model: claims are sentences copied from the source; verify groups claims
that carry the same figures. Not used in real runs."""
from __future__ import annotations

import json
import re

from newsbot.llm.mock import MockProvider


def _extract(messages) -> dict:
    block = re.search(r"<source_data[^>]*>\n(.*)\n</source_data>", messages[-1]["content"], re.S)
    text = block.group(1) if block else ""
    sents = [s.strip() for s in re.split(r"(?<=[.।!?])\s+", text) if len(s.strip()) > 20]
    sents = [s for s in sents if not re.search(r"ignore (all )?previous|http", s, re.I)]
    from newsbot.extract import numbers_in
    return {"claims": [{"kind": "number" if numbers_in(s) else "what", "text": s[:280]} for s in sents[:6]]}


def _verify(messages) -> dict:
    from newsbot.extract import numbers_in
    block = re.search(r"<claims_data>\n(.*)\n</claims_data>", messages[-1]["content"], re.S)
    claims = json.loads(block.group(1))["claims"] if block else []
    groups: dict = {}
    for c in claims:
        key = frozenset(numbers_in(c["text"])) or frozenset([c["id"]])
        groups.setdefault(key, []).append(c)
    return {"facts": [{"statement": g[0]["text"], "claim_ids": [c["id"] for c in g], "kind": g[0]["kind"],
                       "disputed": False, "note": ""} for g in groups.values()]}


_FILLER = {
    "en": ["This account is built only from the verified fact sheet and will change as more is confirmed.",
           "Readers should treat single source reports with care until other trusted outlets agree.",
           "Local authorities and independent outlets usually publish further details in the following hours.",
           "Where outlets differ, the differences are shown instead of being smoothed over.",
           "Context matters here because early figures often shift as communication and access improve.",
           "The sources listed below are the ones this report relied on, with their tier and date.",
           "Updates keep the same address, with a dated entry added to the timeline for every change.",
           "People affected should follow official guidance from the relevant ministry or local administration.",
           "A correction is recorded openly whenever an earlier statement turns out to be wrong.",
           "This page is written from several independent reports rather than from a single outlet.",
           "Impact on daily life usually becomes clearer once services and transport are restored.",
           "Further verified facts will be added when at least two trusted sources agree on them."],
    "bn": ["এই প্রতিবেদন শুধু যাচাই করা তথ্যের ভিত্তিতে লেখা এবং নতুন নিশ্চিত তথ্য এলে বদলাবে।",
           "একটি মাত্র সূত্রের খবর অন্য নির্ভরযোগ্য সংবাদমাধ্যম সমর্থন না করা পর্যন্ত সতর্কভাবে পড়া উচিত।",
           "স্থানীয় প্রশাসন ও স্বাধীন সংবাদমাধ্যম সাধারণত পরবর্তী কয়েক ঘণ্টায় আরও তথ্য প্রকাশ করে থাকে।",
           "যেখানে সূত্রগুলোর মধ্যে অমিল আছে সেখানে সেই অমিল লুকানো হয়নি, স্পষ্ট করে দেখানো হয়েছে।",
           "প্রাথমিক সংখ্যা প্রায়ই বদলায়, কারণ যোগাযোগ ও যাতায়াত স্বাভাবিক হলে নতুন তথ্য পাওয়া যায়।",
           "নিচে তালিকাভুক্ত সূত্রগুলোই এই প্রতিবেদনের ভিত্তি, সঙ্গে তাদের স্তর ও তারিখ দেওয়া আছে।",
           "প্রতিবেদনের ঠিকানা একই থাকে এবং প্রতিটি পরিবর্তনের জন্য সময়রেখায় তারিখসহ নতুন এন্ট্রি যোগ হয়।",
           "ক্ষতিগ্রস্ত মানুষের উচিত সংশ্লিষ্ট মন্ত্রণালয় বা স্থানীয় প্রশাসনের নির্দেশনা অনুসরণ করা।",
           "আগের কোনো তথ্য ভুল প্রমাণিত হলে তা সংশোধনী হিসেবে প্রকাশ্যে লেখা হয়।",
           "এই লেখা একাধিক স্বাধীন প্রতিবেদন থেকে তৈরি, কোনো একক সংবাদমাধ্যমের ওপর নির্ভর করে নয়।",
           "দৈনন্দিন জীবনে প্রভাব সাধারণত পরিষেবা ও যোগাযোগ ফিরে এলে আরও স্পষ্ট হয়।",
           "অন্তত দুটি নির্ভরযোগ্য সূত্র একমত হলে তবেই নতুন যাচাই করা তথ্য যোগ করা হবে।"],
}
_LEADS = {"en": ["reports say", "it is also stated that", "further", "and then", "while"],
          "bn": ["প্রতিবেদন অনুযায়ী", "আরও বলা হয়েছে", "এছাড়া", "তারপর", "একই সঙ্গে"]}
_HEAD = {"en": ("What happened", "Context and impact"), "bn": ("কী ঘটেছে", "প্রেক্ষাপট ও প্রভাব")}
_TARGET = {"en": 540, "bn": 480}


def _rephrase(stmt: str, lang: str, size: int = 5) -> str:
    """Mock 'paraphrase': short fragments joined by connector words, so no 8-word run of a source survives.
    Gate-clean plumbing for dry runs, deliberately NOT good prose (real models write the real articles)."""
    ws = stmt.replace('"', "").rstrip(".").split()      # the mock never emits quotation marks
    leads = _LEADS[lang]
    out = " ".join(f"{leads[(i // size) % len(leads)]} {' '.join(ws[i:i + size])}" for i in range(0, len(ws), size))
    return out[:1].upper() + out[1:] + "."


def _write(lang: str):
    def respond(messages) -> dict:
        # the fact sheet is in the request message; a repair note may follow it as a later message
        carrier = next((x["content"] for x in reversed(messages) if "<fact_sheet>" in x["content"]), messages[-1]["content"])
        m = re.search(r"<fact_sheet>\n(.*?)\n</fact_sheet>", carrier, re.S)
        fs = json.loads(m.group(1)) if m else {"facts": [], "sources": []}
        facts, sources = fs["facts"], fs["sources"]
        marks = lambda f: "".join(f"[{i}]" for i in f["source_ids"])      # noqa: E731
        first, second = _HEAD[lang]
        paras = [f"{_rephrase(f['statement'], lang)} {marks(f)}" for f in facts]
        wc = lambda parts: sum(len(p.split()) for p in parts)             # noqa: E731
        ctx, pool, i = [], _FILLER[lang], 0
        while wc(paras) + wc(ctx) < _TARGET[lang] + 20:
            ctx.append(" ".join(pool[(i + k) % len(pool)] for k in range(3)))
            i += 3
        head = facts[0]["statement"].rstrip(".").split()[:7] if facts else ["story"]
        title = ("Developing story: " if lang == "en" else "বিকাশমান খবর: ") + " ".join(head)
        day = (sources[0]["published_at"][:10] if sources else "")
        by = {s: [f for f in facts if f["status"] == s] for s in ("confirmed", "reported", "disputed")}
        item = lambda f: f"{_rephrase(f['statement'], lang)} {marks(f)}"  # noqa: E731
        return {"title": title, "summary": _rephrase(facts[0]["statement"], lang) if facts else "No facts.",
                "body_markdown": f"## {first}\n\n" + "\n\n".join(paras) + f"\n\n## {second}\n\n" + "\n\n".join(ctx),
                "timeline": [{"time": day, "text": f"{sources[0]['name']} {'first reported' if lang == 'en' else 'প্রথম জানায়'} [{sources[0]['id']}]"}] if sources else [],
                "confirmed": [item(f) for f in by["confirmed"]], "unclear": [item(f) for f in by["reported"]],
                "disputed": [item(f) for f in by["disputed"]],
                "sources": [{"id": s["id"]} for s in sources], "category": "world",
                "slug": "developing-" + re.sub(r"[^a-z0-9]+", "-", fs.get("story_id", "story").lower()).strip("-")}
    return respond


def dry_run_provider() -> MockProvider:
    return MockProvider({"extract": _extract, "verify": _verify, "write_en": _write("en"), "write_bn": _write("bn"),
                         "qa_check": {"unsupported": []}, "*": {}})
