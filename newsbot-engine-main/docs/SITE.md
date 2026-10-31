# Site (Hugo) notes

Everything lives in `engine/site/`. No JS framework; one small inline script exists only for the optional consent banner.

## Build
```bash
python scripts/gen_site_config.py --brain <brain> --sync-content      # data/site.yaml, static/ads.txt, static/robots.txt, content
hugo --minify -s site --baseURL "$(python scripts/gen_site_config.py --brain <brain> --print-base-url)"
python scripts/check_file_count.py site/public                          # warn >12,000, fail >16,000 files
```
`--sync-content` replaces the sample articles in `site/content/{bn,en}/` with the brain's `content/<lang>/*.md`.
The shipped trust pages (`site/content/<lang>/pages/*.md`) stay unless the brain has `content/<lang>/pages/<name>.md`,
which then overrides them (keep owner details in the private brain repo).

## URLs
`/bn/<slug>/`, `/en/<slug>/`, categories `/<lang>/categories/<name>/` (paginated 20), policy pages `/<lang>/pages/<name>/`.
The root `/` redirects to `/bn/`; `static/404.html` is the root 404 (Cloudflare Pages would otherwise behave like an SPA).
hreflang links appear only on pages that have a translation sibling (same `translationKey`).

## Owner steps
- Replace every `[OWNER: ...]` in the trust pages (the generator warns while any remain).
- Set `site.name` and `site.base_url` in the brain config.
- Bengali font: add a Noto Sans Bengali subset as `site/static/fonts/NotoSansBengali-subset.woff2`, then set `site.bengali_font: true`. Until then a system font stack is used.
- Ads: set `ads.publisher_id` (`pub-...`) and `ads.enabled: true` only after AdSense approval; optional ad-unit ids in `ads.slots`; `ads.consent_required: true` for EU/UK traffic (ads load only after "accept"). No ads on policy pages or 404.
