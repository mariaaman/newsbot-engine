# NewsBot engine

An automated, bilingual (Bengali and English) news site that tries to be trustworthy rather than big. Every hour it
looks at Google Trends, Google News and trusted RSS feeds, picks the few stories worth covering, gathers facts from
several independent sources, cross-checks them, and writes a source-analysed article. The same story is updated in
place (same URL) when verified new facts appear. Hosting is Cloudflare Pages (free plan), the models are Groq's free
tier first, income is Google AdSense once approved.

This repository (**newsbot-engine**, public) holds the code, the Hugo site, the workflows and the tests. Everything
private (config, prompts, state, articles) lives in a second, private repository, **newsbot-brain**, started from
`brain-template/`. No secrets, prompts or drafts ever belong in this repo or in its logs.

```
hourly -> discover -> cluster -> score -> gate -> fetch -> extract -> verify -> write (bn/en) -> QA
       -> brain repo (state + content, or a pull request in review mode) -> Hugo -> Cloudflare Pages
```

Most hours end at the gate with zero model calls: publishing nothing is a normal, correct result (a typical day is
4 to 12 stories; the hard cap is 24 new stories a day).

## What it guarantees (and what it does not)

- **Facts only from the verified fact sheet.** A claim is *confirmed* only when two or more independent tier 1/2
  organisations agree; otherwise it is written as reported, unclear or disputed. Code decides the status, not the model.
- **Sensitive topics never auto-publish** (deaths, accusations against named people, health claims, elections,
  religious or communal issues, minors, financial advice): they wait in `pending/` for a human.
- **Every article shows** its sources with tier and date, a confirmed / unclear / disputed block, the last update
  time, a corrections log, and an AI-assisted line.
- **Fetched web text is untrusted.** Injection-like sentences are dropped at fetch, text goes to models only inside
  data blocks that cannot be closed from inside, claims must be grounded in their source, model output is stripped of
  HTML and of every link, `www.`, FTP address and e-mail not in the source list, and the QA gates reject unsupported
  figures, names and citations. One honest limit: a planted sentence that looks like ordinary news can still become
  *one source's* claim; it can never be confirmed alone.
- **Logs are public**, so they contain only counts, story IDs, domains, model IDs and status words. An unexpected error
  is logged as its type and file:line only.
- **Not guaranteed:** the quality of real-model articles, Bengali fluency, and the live behaviour of GitHub Actions,
  Cloudflare and Hugo were not testable while building; every such assumption is marked `VERIFY` in the code and the
  first `setup` and CI runs check the most important ones. Start in review mode and read what it writes.

## Setup (10 steps)

Do these once, in order. Full detail with every secret and setting: [`docs/FIRST_RUN.md`](docs/FIRST_RUN.md).

## 1. Repos
Create **newsbot-engine** (public) with the contents of `engine/`, and **newsbot-brain** (private) with everything in
`brain-template/` (including the `.gitkeep` files). In the brain's `config/newsbot.yaml` set `site.name`,
`site.base_url` and `publish.mode: review`. Keep `main` as the default branch in both and do not protect the brain's `main`.

## 2. Accounts and tokens
Cloudflare: note the Account ID and create an API token with *Cloudflare Pages: Edit* and *Workers AI: Read*. Groq: an
API key (optional: a Gemini key). GitHub: a fine-grained token for the **brain repo only** with *Contents* and
*Pull requests* read and write.

## 3. Engine repo secrets and variables (Settings, Secrets and variables, Actions)
Secrets: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `GROQ_API_KEY`, `BRAIN_REPO_TOKEN`, optional
`GEMINI_API_KEY`. Variables: `BRAIN_REPO` (`owner/newsbot-brain`), `PAGES_PROJECT`, `PUBLISH_MODE` (`review`).

## 4. Run `setup` (Actions tab, setup, Run workflow)
Every line must say `OK` (`SKIP` for the optional Gemini key is fine). It creates the Pages project if missing and
checks the tokens, the brain repo structure and the configured model IDs. Fix a failing model ID in the brain's
`config/llm.yaml` and run it again.

## 5. Fill in the site
Replace every `[OWNER: ...]` in the trust pages through overrides in the brain's `content/<lang>/pages/`
(see [`docs/SITE.md`](docs/SITE.md)). Set `site.name` and `site.base_url`.

## 6. Dry run
Run `hourly` by hand with mode **dry-run**: fixtures and a mock model, no keys spent, nothing committed or deployed.

## 7. Real run in review mode
Run `hourly` by hand with the mode field empty (it uses `PUBLISH_MODE`, default `review`). "No new stories" is a normal
result. A story that passes every gate opens a pull request in the brain repo, `Review: new articles <date-time>`.

## 8. Approve on a phone
In the GitHub app open the pull request and read the article under *Files changed*. **Merge** to publish (the next
`hourly` run deploys it); **close** to reject. Drafts that failed a gate or are sensitive wait in `pending/<lang>/`
with their reasons; to publish one, move it to `content/<lang>/` and delete its `pending:` block.

## 9. Go automatic only when you trust it
After a few weeks of merging nearly everything unchanged, set the `PUBLISH_MODE` variable to `auto`. Sensitive and
failed-gate articles still go to `pending/`. Updates to live articles follow the same rule (a pull request in review
mode). You can switch back to `review` at any time.

## 10. Later
Bengali font subset and AdSense: [`docs/SITE.md`](docs/SITE.md) (turn ads on only after approval). The monthly
`keepalive` workflow stops GitHub from pausing the schedule. When the site grows, read
[`docs/SCALING.md`](docs/SCALING.md) (the 20,000-file limit). Never paste prompts or drafts into public issues.

## Develop and test

```bash
pip install -e ".[dev]" && pytest                       # full suite (needs pydantic and pytest)
python -m newsbot run --mode dry-run                    # whole pipeline on fixtures with a mock model, offline
python tools/run_tests_offline.py                       # same logic tests without pydantic or pytest
python tools/eval_writers.py --help                     # compare configured models on 10 saved stories
python scripts/gen_site_config.py --brain <brain> --sync-content && hugo --minify -s site
python scripts/check_file_count.py site/public
```

Set `NEWSBOT_DEBUG=1` locally to see full tracebacks (public workflow logs deliberately never print them).

## Layout

| Path | What |
|---|---|
| `newsbot/` | the engine: discovery, clustering, scoring, gate, fetch, extract, verify, write, QA, publish, update flow, LLM layer |
| `brain-template/` | starting point for the private brain repo: config, prompts, state folders |
| `site/` | Hugo site (layouts, i18n, trust pages, JSON-LD, ads flag) |
| `.github/workflows/` | `hourly`, `setup`, `keepalive`, `ci` |
| `scripts/` | brain commit/PR flow, setup checks, site config generator, file-count check |
| `tests/`, `tools/` | tests with offline fixtures; the model evaluation tool |
| `docs/` | `FIRST_RUN.md`, `SITE.md`, `SCALING.md` |

Out of scope for v1: images and video, comments, accounts, search, newsletters, social posting, other languages.
