# Output languages (the languages the site publishes in)

The sources may be in any language (config/sources.<lang>.yaml). The **site** publishes in the languages listed in `site.languages`
(newsbot.yaml). Today: Bengali and English. Any language of the catalog (`newsbot/langs.py`: bn, en, hi, ur, ar, ne, id, es, fr, de, pt,
ru, tr, ja, zh, ko, fa, vi, th, sw) can be added.

## Add or remove a language
Dashboard, **সেটিংস → সাইটের ভাষা**: pick a language, press **ভাষা যোগ**. That edits only the `site.languages` line of newsbot.yaml.
Everything else comes from the catalog until you override it in newsbot.yaml:

| what | default for a new language | override in newsbot.yaml |
|---|---|---|
| minimum article length | catalog value (words; **characters** for ja, zh, th) | `qa.min_words.<lang>` |
| title length | catalog value | `qa.title_len.<lang>` |
| writer instruction | "explain what this means for readers who speak <language>" | `write.local_angle.<lang>` |
| AI notice under each article | catalog text | `site.ai_disclosure.<lang>` |
| writer prompt | `prompts/write_default.md` (own file `prompts/write_<lang>.md` wins) | brain `prompts/` |
| LLM tier of the writer | the tier of `write_default`, else of `write_en` | `llm.tasks.write_<lang>` in llm.yaml |
| Hugo language entry, language switcher, root 404 | generated at every build | (generated: scripts/gen_site_config.py) |
| UI labels (menu, "Sources", categories ...) | translated for hi ur ar es fr de pt id ru tr ja zh ko; **English** for ne fa vi th sw | `site/i18n/<lang>.yaml` (delete its first `# GENERATED` line, then edit) |
| right-to-left | automatic for ur, ar, fa (`dir="rtl"`) | |
| trust pages (about, privacy, corrections ...) | the **English** pages stand in | brain `content/<lang>/pages/<name>.md` |

The default language (`site.default`) cannot be removed; remove or change it only after another default is chosen. Removing a language
stops new articles and hides it from the menu; articles already in `content/<lang>/` stay in the brain repo.

## What it costs
`routing_mode: cross_language` writes every story in **every** site language. Each added language adds one writer call and one QA check per story,
so LLM neurons grow roughly with the number of languages. Watch the dashboard LLM page after adding one; lower `limits.max_new_per_run` or add
accounts if the allowance runs out. With `routing_mode: same_language` a language only gets stories from sources written in it.

## Quality limits you should know
* Every translated label, notice and prompt in the catalog is a **first draft without a native-speaker review**. Have a speaker of each language
  you publish in read the labels, the AI notice and a few articles before you rely on it, above all the trust pages (AdSense reviews them).
* The number check works for every script. The capitalised-name check runs for English (and Bengali) only: in every other language the writer
  translates place and organisation names (Estados Unidos, États-Unis), which the check rejected again and again in a real run, and German
  capitalises every noun. The LLM fact check still covers claims in every language.
* `bn_fluency_flags` (the Bengali evaluation) exists only for Bengali.
* Hugo itself was not available where this was built: the first build with a new language is the real test (`hourly` or `deploy`; the log shows
  `languages=...` from gen_site_config).

## Keeping the dashboard in step
`dashboard/src/langs.js` is generated: after editing `newsbot/langs.py` run `python scripts/gen_dashboard_langs.py` (a test fails otherwise).
