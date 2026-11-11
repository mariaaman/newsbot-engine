# Self-learning gate repair (autofix → diagnose → learned rules → review log)

Written so that someone (or Claude) who opens this repo 3-4 days later knows exactly what exists and what to do next.

## The ladder: what happens when a draft fails a gate
1. **Gates** (`newsbot/qa.py`) judge the draft.
2. **Diagnose** (`newsbot/diagnose.py`, only for `unsupported_name`): ONE short call to a strong model (task `diagnose`, tier of `verify`)
   with the flagged names, the sentence each is in and the fact sheet. The model answers in a fixed format: `variant` (another form of a name that
   IS in the fact sheet, quoting it) / `invented` / `other`. **Code** then checks a `variant` claim: the quoted name must appear in the fact sheet.
   Only then a rule is saved, and the same draft is judged again. The model never edits the article and never writes code.
3. **Autofix** (`newsbot/autofix.py`, no LLM): corrects a wrong figure when the fact sheet gives one unambiguous answer, otherwise removes the sentence.
4. **Repair rewrite** (`write.max_repair_attempts`, 0 now): the old model rewrite.
5. Still failing → the draft goes to `pending/` for a human AND one line is appended to `state/learning/review.jsonl`.

## Files (all in the PRIVATE brain repo, `state/learning/`, committed by `brain_commit.sh` with the rest of `state/`)
| file | what | who reads it |
|---|---|---|
| `rules.json` | learned rules `{term, needs[], kind, lang, story, learned}`: "`term` is not an invented name IF all `needs` words are in the fact sheet" | the engine, every run (`learned.name_ok`) |
| `review.jsonl` | every draft that still failed after all fixes: story id, lang, failure codes, offending names/numbers, short context sentences, the model's verdicts, whether autofix changed it, unsupported claims, the fact sheet's numbers | **the developer**, to decide the next automatic fix |
| `stats.json` | per UTC day counters: `gate_fail:<code>`, `draft_clean`, `draft_failed`, `autofix_kept`, `diagnose_call`, `diagnose_rules_saved`, `rule_learned`, `rule_used`, `verdict_*`, `diagnose_failed`, `diagnose_skipped_budget` | you: the trend shows whether model calls and failures fall |

The public engine log never contains names or sentences, only counts. Nothing is written in dry-run.

## What to do after 3-4 days
`python -m newsbot learning-report --brain brain` prints the per-day counters, the learned rules and the most common remaining failure codes.
To get help improving autofix, give Claude: `state/learning/review.jsonl`, `stats.json`, `rules.json` and the hourly logs. Claude should group
`review.jsonl` by `failures` and by the shape of `names` / `numbers` / `context`, and for each recurring shape do ONE of:
- a number format the parser misreads → fix `newsbot/extract.py::numbers_in` (add a test with the real string);
- an ordinary word flagged as a name → extend `ROLE_WORDS` / `DATE_WORDS` in `qa.py` or the sentence-start rule (add a test);
- a recurring variant that diagnose cannot validate → a new rule kind in `learned.py` (still data + a safety condition);
- an unsupported *claim* pattern → `autofix.py` (`bad_claim`) or the claims check prompt.
Rule of thumb: a fix is good when it removes a failure class WITHOUT an LLM call. Add a test for every new class.

## Safety rules (do not weaken)
- A learned rule is conditional on the fact sheet; it can never admit a name the fact sheet does not justify.
- The model classifies; code validates; nothing the model says is executed or written into articles.
- `learning` files are data: wrong or unwanted rules can be deleted by hand (edit `rules.json`).
- Switch the whole diagnose step off: `qa.diagnose: false` in `config/newsbot.yaml`.


## Fixed from real runs (v19)
Found by reading state/learning/review.jsonl and stats.json after four days:
- "todo" (Spanish for "all"), "placeholder", "tbd", "language model" were banned words: removed (real AI boilerplate remains banned).
- "France 24" made 24 an unsupported figure: digits inside source names are allowed.
- "$21 billion" vs a fact sheet that gives 21: a million/billion/trillion figure is accepted when its written part is a fact figure.
- 5 p.m. in the English lead written as 17:00 in a translation: the 24-hour form of the lead's 12-hour times is allowed.
- Names: sentence starters ("With", "Neither", "Large"), nationality and party adjectives (Turkish, Democratic), Europe/European, EU/UK/UN, "U.S." are not names.
- Translations are no longer sent to the model fact-check (it compared French/Hindi text with the English fact sheet and rejected correct claims); the fidelity gate already guards them. A body sentence left in English now counts as untranslated and is retried once.
- Headlines over the limit are cut at a natural break by the engine; the translation prompt states the length. Citation markers [S#] that name no listed source are dropped by autofix.
