"""Live test (`python -m newsbot llmtest`): does each configured account/model really answer?
Two small probes per model: a tiny JSON reply, and a realistic extraction on a short Bengali + Japanese text (the case
where JSON mode and token limits have failed). Logs only provider, model ID, probe name, status and token counts:
never keys, prompts or answers."""
from __future__ import annotations

import logging

from newsbot.llm.base import LLMError, expand_pool, parse_json, safe_reason

log = logging.getLogger("newsbot.llmtest")

TINY = [{"role": "user", "content": 'Reply with only this JSON object: {"ok": true, "n": 7}'}]
_TEXT = ("ঢাকায় মঙ্গলবার সকালে প্রধানমন্ত্রী একটি নতুন সেতু উদ্বোধন করেছেন। অনুষ্ঠানে প্রায় ৫০০ জন অতিথি উপস্থিত ছিলেন। "
         "সেতুটির নির্মাণে ১২০ কোটি টাকা খরচ হয়েছে বলে কর্তৃপক্ষ জানিয়েছে।\n"
         "東京都で火曜日の朝、震度5強の地震が発生し、約5万人に避難指示が出された。気象庁によると震源の深さは10キロ。"
         "首相は被害の確認を急ぐよう指示した。") * 2
REALISTIC = [
    {"role": "system", "content": 'Extract facts. Return ONLY JSON: {"claims": [{"kind": "what|number|who|where|when", "text": "..."}]}. '
                                  "Up to 8 claims, each in the language of the text it comes from."},
    {"role": "user", "content": "<source_data>\n" + _TEXT + "\n</source_data>"},
]


def _ok_tiny(d) -> bool:
    return isinstance(d, dict) and d.get("ok") is True


def _ok_claims(d) -> bool:
    return isinstance(d, dict) and isinstance(d.get("claims"), list) and len(d["claims"]) >= 1


def run_livetest(settings: dict, providers: dict) -> dict:
    """Returns {"ok": [(provider, model)], "failed": [...], "skipped": [...]}; logs one line per probe."""
    out = {"ok": [], "failed": [], "skipped": []}
    seen = set()
    for tier, entries in settings.get("tiers", {}).items():
        for e in [{"provider": n, "model": en["model"]} for en in entries
                  for n in (expand_pool(providers, en["provider"]) or [en["provider"]])]:
            key = (e["provider"], e["model"])
            if key in seen:
                continue
            seen.add(key)
            prov = providers.get(e["provider"])
            if prov is None:
                log.info("llmtest provider=%s model=%s status=SKIP reason=no_key", *key)
                out["skipped"].append(key)
                continue
            all_ok = True
            for probe, msgs, check, mx in (("tiny", TINY, _ok_tiny, 500), ("realistic", REALISTIC, _ok_claims, 2500)):
                try:
                    r = prov.chat(e["model"], msgs, json_mode=True, max_tokens=mx, task="llmtest")
                    good = check(parse_json(r.text))
                    log.info("llmtest provider=%s model=%s probe=%s status=%s tokens=%d", *key, probe,
                             "OK" if good else "BAD_ANSWER", r.total_tokens)
                    all_ok &= good
                except (LLMError, ValueError) as ex:
                    all_ok = False
                    log.info("llmtest provider=%s model=%s probe=%s status=FAIL reason=%s", *key, probe, safe_reason(ex))
                    if getattr(ex, "args", None) and "404" in str(ex):
                        _hint(prov, key[0])
                    break                                   # no point sending the bigger probe after a failure
            out["ok" if all_ok else "failed"].append(key)
    log.info("llmtest summary working=%d failed=%d skipped=%d", len(out["ok"]), len(out["failed"]), len(out["skipped"]))
    return out


def _hint(prov, pname: str) -> None:
    """After a 404 (retired or unknown model ID): list a few real IDs so the config can be fixed. Public IDs only."""
    try:
        ids = prov.list_models()
    except LLMError:
        return
    log.info("llmtest hint provider=%s ids=%s", pname, ",".join(ids[:8]))
