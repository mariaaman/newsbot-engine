# Multi-language discovery, cross-language writing, deeper research

## What changed
| Before | Now |
|---|---|
| Feeds in Bengali and English only | Feeds in **any language** (20 shipped: en bn hi ur ar ja zh ko id es fr de ru pt tr fa vi th ne sw). Every `config/sources.<lang>.yaml` is loaded. |
| A language version needed 2 trusted domains **in that language** | **cross_language** routing: once >= `routing_min_orgs` (2) independent organisations in ANY language cover a story, it is written in **every site language** (bn + en), each written for its own readers from the language-neutral fact sheet, not translated. |
| Domains counted | **Organisations** counted: bbc.com = bbc.co.uk = bbci.co.uk (all BBC services are one source), DW = one, `.or.jp`/`.go.jp`/`.co.kr` domains no longer collapse into one. |
| 5 pages, 6,000 chars, 25 claims per source | 7 pages (a failed page is replaced by the next organisation), 8,000 chars, 40 claims, up to 60 facts. |
| Clustering links only close headlines | **research** step: single leftover items close to the story's centroid (`research.attach_threshold`) are attached before pages are fetched. No extra model call. |
| Articles 450-500 words, two sections | 600+ words (aim 700-1,100), sections: what happened, details, **who is reporting what**, context and impact, what is unknown. Outlets are named in the text and cited with [S#]. |

## Fixed on the way
- `৪ কোটি` (crore) was read as 4 instead of 40,000,000 (word-boundary bug with Bengali vowel signs) - this could wrongly fail or pass number checks.
- Digits of Arabic, Persian/Urdu and Devanagari are normalised; units 万 億 亿 만 억 लाख करोड़ لاکھ کروڑ млн etc. are understood.
- Japanese/Chinese headlines are matched with character bigrams (they have no spaces).
- Pages in Shift_JIS, GBK, windows-1251 ... are decoded with their own charset instead of being garbled as UTF-8.
- RDF feeds with `dc:date` (e.g. Asahi) were silently dropped.
- Feeds are fetched in parallel (8 workers); newest 30 per feed; items older than 36 h ignored; clustering uses unit vectors and `math.sumprod` (fast).
- One new article per hourly run (`limits.max_new_per_run`) so the 30-minute workflow limit is safe; the rest wait.

## Honest limits
- **Independence**: outlets that republish the same wire story (Reuters/AP/AFP/Xinhua) still look independent. State-controlled media are tier 3 in the shipped lists (discovery only, never fetched).
- **Token budget**: a rich article costs roughly 60-100k tokens (many sources, Bengali/Hindi/Japanese text is token-heavy). On free tiers expect **about 2-5 full articles per day**. Add `GEMINI_API_KEY` (optional fallback in llm.yaml) or lower `max_sources_per_story` / `source_chars` to trade depth for volume.
- Detail is limited by the fact sheet: the engine never lets the writer add facts. More sources and facts = richer article; thin sources = a shorter article (it is better short than invented).
- No live web search: "research" means more of the feed pool plus deeper reading, not Google search (Google News links are redirects that cannot be read reliably).
- Output stays **bn + en**. More output languages (hi, ur, ar ...) are a later step: the writer already falls back to `prompts/write_default.md`, but each new output language also needs Hugo i18n, trust pages, QA limits and a `write_<lang>` task in llm.yaml.
- Every feed URL is marked `# VERIFY` (written without network access). Dead feeds only cost a warning.
- Optional: set the `NEWSBOT_CONTACT_URL` environment variable (e.g. your About page) so the crawler identifies itself properly; some sites block anonymous agents.
