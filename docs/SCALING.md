# Scaling: the 20,000-file limit

Cloudflare Pages (free plan) accepts at most **20,000 files per site** and **25 MiB per file** (VERIFY the current
limits). `scripts/check_file_count.py` runs after every Hugo build:

| Files in `site/public` | Result |
|---|---|
| up to 12,000 | `ok` |
| 12,001 to 16,000 | `warn`: the run is green but prints a warning that points here |
| above 16,000, or any file over 25 MiB | `fail`: the step fails and **the deploy is skipped** |

A failed check does not take the site down: the last deployed version stays live. But new articles stop
appearing and the `hourly` run turns red every hour (the deploy is retried because it was never recorded) until you
make room. Act at the warning, not at the failure. The thresholds are the script's defaults (`--warn`, `--fail`).

## How fast do you get there?

An article is exactly one file (`/<lang>/<slug>/index.html`). A story written in both languages is two files.
Everything else is small and bounded: the home page and its feed per language, up to about 12 category pages with
one feed each (and one extra page per 20 articles in a category), the sitemaps, `404.html`, `robots.txt`,
`ads.txt`, the stylesheet and the fonts. Plan with **files ≈ articles + 300**.

| Pace (language versions per day) | Warning at 12,000 | Failure at 16,000 |
|---|---|---|
| 6 (a quiet day) | about 5.3 years | about 7.2 years |
| 13 (typical: 8 stories, most in both languages) | about 2.5 years | about 3.3 years |
| 48 (hard cap: 24 stories in both languages, every day) | about 8 months | about 11 months |

The hard cap is a ceiling, not a plan (a typical day is 4 to 12 stories). Check the real pace with
`find brain/content -name '*.md' | wc -l` after a month and divide by the days (the trust pages add a handful).

## Levers, in the order to use them

1. **Do nothing yet.** Below 12,000 files there is nothing to fix.
2. **Publish less.** `limits.max_new_per_day` and the scoring `threshold` in the brain's `config/newsbot.yaml` are
   plain config. Raising the threshold also raises quality. Stories routed to one language cost one file instead of two.
3. **Archive old articles** (below). This is the real fix and it is permanent.

The site already generates the minimum: no tag pages, no per-day or per-month archives, no section lists, feeds only
for the home page and categories (`site/hugo.yaml`). There is nothing further to trim there.

## Archive procedure (when `check_file_count` warns)

Idea: a news article that is a year old and `closed` gets almost no visits. Take it out of the build; keep it in git.

1. In the **brain** repo, list the candidates: stories with `"status": "closed"` in `state/stories.jsonl` whose last
   update is older than your cut-off (start with 12 months). Each story lists its files under `versions.<lang>.url`
   (`/<lang>/<slug>/`, so the file is `content/<lang>/<slug>.md`).
2. Move those files out of the build, keeping the history:
   ```bash
   mkdir -p archive/en archive/bn
   git mv content/en/<slug>.md archive/en/      # one line per article; a short script is fine
   git commit -m "Archive closed stories older than 12 months"
   ```
   Nothing reads `archive/`, so Hugo does not build it. Never archive a story that is not `closed`: a `stable`
   story can still reopen and be updated in place, and an update needs its live file.
3. The next `hourly` run sees changed content, rebuilds, and deploys. The archived URLs now return the site's 404 page
   and drop out of the sitemap by themselves. Re-run the file count to confirm the drop.

What this does not break (checked in the code, not on a live site): `reconcile` only looks at articles that are
waiting for review, so a published, closed article whose file has moved is ignored; a closed story never reopens, so
the engine will not try to update or rewrite it.

What it costs: inbound links and search-engine entries for those articles stop working. Keep the ones that still get
traffic (Cloudflare Web Analytics, or Search Console, shows which) by leaving their files in `content/`.

## Keeping archived URLs alive (optional, not implemented)

If old URLs must keep working, the engine would need a second Pages project that serves `archive/` (for example on
`archive.<your-domain>`) plus redirects from the live site. That means a second deploy step in `hourly.yml` and a
redirect list; Pages limits `_redirects` to roughly 2,000 static rules (VERIFY), so only the most visited articles
could be redirected. Build this only if archiving by deletion turns out to cost real traffic.

## Other things that grow

- `state/decisions.jsonl` gains one line per story per hour (IDs and numbers only). Nothing reads it back, so you may
  truncate or `git mv` it into `state/archive/` once a year. `state/stories.jsonl` is read on every run: leave it.
- `state/factsheets/` keeps one small JSON per story; after a story is archived its sheet can go too.
- A GitHub repository is comfortable far beyond the sizes involved here (Markdown and small JSON).
