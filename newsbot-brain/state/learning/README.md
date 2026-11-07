# state/learning — what the engine learned and what it could not fix

Written by the engine (see `docs/SELF_LEARNING.md` in newsbot-engine). Do not edit while a run is in progress.

- `rules.json`   rules learned from failures: a name is accepted only if the listed words are in the story's fact sheet. Delete a line to unlearn it.
- `review.jsonl` one line per draft that still failed a quality gate after every automatic fix (this is private: it contains names and sentences).
- `stats.json`   daily counters (failures by gate, autofix results, model calls, rules learned/used).

## After 3-4 days: give these to Claude
1. `state/learning/review.jsonl`   2. `state/learning/stats.json`   3. `state/learning/rules.json`   4. the latest hourly logs.
Say: "improve autofix from the review log". Claude groups the failures, fixes the recurring ones in the engine code (with tests) and tells you
which rules to keep or delete. Summary in the terminal: `python -m newsbot learning-report --brain brain`.
