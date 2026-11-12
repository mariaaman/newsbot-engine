# First-run checklist

Do these once, in order. Nothing here needs a computer after step 7: nothing waits for your approval.

## 1. Repos
- Create **newsbot-engine** (public) and copy the contents of `engine/` into it.
- Create **newsbot-brain** (private) and copy `engine/brain-template/` (all of it, including the `.gitkeep` files) into its root. Edit `config/newsbot.yaml`: `site.name`, `site.base_url`. (`site.auto_sites` gives each language its own free Pages site.)
- Keep the default branch `main` in both. Do not enable branch protection on the brain's `main` for the token's user (the engine pushes state there).

## 2. Accounts and tokens
- **Cloudflare**: note the Account ID. Create an API token with *Account / Cloudflare Pages / Edit* and *Account / Workers AI / Read* (the same token serves deploys and embeddings).
- **LLM accounts**: no key to create now. After the dashboard is up, add your Cloudflare accounts on the *LLM* page (see docs/LLM_PROVIDERS.md). Until then the deploy account above is used as a single-account fallback.
- **GitHub**: create a fine-grained token limited to the **brain repo only** with *Contents: read and write*.
- **Telegram** (optional but recommended: every step result arrives in one chat). In Telegram open **@BotFather**, send `/newbot`, choose a name and a username, and copy the token it gives you. Open your new bot and press **Start** (or add it to a group). To find the chat ID, send the bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in your browser (use your own token; do not share that address) and copy the number after `"chat":{"id":`. A group's ID starts with `-`.

## 3. Engine repo secrets and variables (Settings, Secrets and variables, Actions)
Secrets: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `BRAIN_REPO_TOKEN`, and for Telegram `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (set both or neither).
Variables: `BRAIN_REPO` (`owner/newsbot-brain`), `PAGES_PROJECT` (e.g. `newsbot`).

## 4. Run `setup` (Actions tab, setup, Run workflow)
Every line must read `OK` (a `SKIP` for Telegram is fine). With the Telegram secrets set, it also sends a test message to your chat: `OK telegram test message reached the chat` means it arrived; a FAIL there usually means you have not pressed Start on the bot or the chat ID is wrong. It creates the Pages project when it is missing, checks the brain token and structure, Cloudflare, Workers AI, and the configured Groq/Workers AI model IDs. If a model ID FAILs, fix it in the brain's `config/llm.yaml` (IDs are a `VERIFY` item) and run `setup` again.

## 5. Fill in the site
Replace every `[OWNER: ...]` in the trust pages by adding overrides under the brain's `content/<lang>/pages/` (see `docs/SITE.md`). Set `site.name` and `site.base_url` to your Pages URL or custom domain.

## 6. Dry run
Run `hourly` manually with **mode = dry-run** (fixtures and a mock LLM: no keys spent, nothing is committed or deployed). If Telegram is connected, the run report arrives in the chat too, so you can see the format before anything real happens.

## 7. First real run
Run `hourly` manually with the mode field empty (it publishes). Most hours end with zero LLM calls: "no new stories" is a normal result. The first run also deploys the trust pages (the site needs content to build).
A story that passes the gates is committed to the brain's `content/` and the sites are rebuilt and deployed in the same run. A draft that still fails a gate after the automatic fixes is cancelled and archived in the brain's `rejected/<lang>/` with the reasons in its front matter.
With Telegram connected, each run sends **one quiet message** listing every step with its result: ✅ ok, ❌ fail (it tried and did not get what it needed), 🛑 error (unexpected crash; type and file:line only), ⏭ skip (nothing to do, cool-down, ...), plus one quiet line per published article.

## 8. Watch it from a phone
- The dashboard (docs/DASHBOARD.md) shows the sites, visitors, run history, the published articles (you can fix a typo or hide one from the site) and the **cancelled drafts** (read only, with the reason each failed).
- Sensitive topics (deaths, accusations, health, elections, religion, minors, financial advice) follow `publish.on_sensitive` in the brain's `config/newsbot.yaml`: `publish` (default) or `reject`.
- **Updates.** A story that gets new confirmed facts is updated in place (same URL, same publication date, new "Updated" time, a dated timeline entry, and a corrections entry when something earlier was wrong). An update that fails a gate is cancelled: the old article stays live.

## 9. Let it learn
Cancelled drafts stay in `rejected/<lang>/` and the self-learning files in `state/learning/` collect what the gates got wrong (docs/SELF_LEARNING.md). After a few days send them with the run logs for the next upgrade. There is no approval queue to keep clean.

## 10. Later
- Bengali font subset and AdSense: `docs/SITE.md`. Turn ads on only after approval.
- If scheduled runs stop after two idle months, `keepalive` (monthly) prevents it; you can also run it by hand.
- Logs are public: they contain only counts, story IDs and status words. Never paste prompts or drafts into issues.
- Telegram is private to you but not end-to-end encrypted: titles and summaries of unpublished drafts pass through Telegram's servers. Set `notify.telegram.include_summary: false` to send titles only, or `notify.telegram.enabled: false` to turn it off. Never share the bot token or paste it into issues (it controls the bot).
