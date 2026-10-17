# First-run checklist

Do these once, in order. Nothing here needs a computer after step 8: approving articles works from a phone.

## 1. Repos
- Create **newsbot-engine** (public) and copy the contents of `engine/` into it.
- Create **newsbot-brain** (private) and copy `engine/brain-template/` (all of it, including the `.gitkeep` files) into its root. Edit `config/newsbot.yaml`: `site.name`, `site.base_url`, `publish.mode: review`.
- Keep the default branch `main` in both. Do not enable branch protection on the brain's `main` for the token's user (the engine pushes state there); PR review is done by merging, not by protection rules.

## 2. Accounts and tokens
- **Cloudflare**: note the Account ID. Create an API token with *Account / Cloudflare Pages / Edit* and *Account / Workers AI / Read* (the same token serves deploys and embeddings).
- **Groq**: create an API key. Optional: a Gemini API key.
- **GitHub**: create a fine-grained token limited to the **brain repo only** with *Contents: read and write* and *Pull requests: read and write*.
- **Telegram** (optional but recommended: every step result and every review request arrives in one chat). In Telegram open **@BotFather**, send `/newbot`, choose a name and a username, and copy the token it gives you. Open your new bot and press **Start** (or add it to a group). To find the chat ID, send the bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in your browser (use your own token; do not share that address) and copy the number after `"chat":{"id":`. A group's ID starts with `-`.

## 3. Engine repo secrets and variables (Settings, Secrets and variables, Actions)
Secrets: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `GROQ_API_KEY`, `BRAIN_REPO_TOKEN`, optional `GEMINI_API_KEY`, and for Telegram `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (set both or neither).
Variables: `BRAIN_REPO` (`owner/newsbot-brain`), `PAGES_PROJECT` (e.g. `newsbot`), `PUBLISH_MODE` (`review`).

## 4. Run `setup` (Actions tab, setup, Run workflow)
Every line must read `OK` (a `SKIP` for the optional Gemini key or for Telegram is fine). With the Telegram secrets set, it also sends a test message to your chat: `OK telegram test message reached the chat` means it arrived; a FAIL there usually means you have not pressed Start on the bot or the chat ID is wrong. It creates the Pages project when it is missing, checks the brain token and structure, Cloudflare, Workers AI, and the configured Groq/Workers AI model IDs. If a model ID FAILs, fix it in the brain's `config/llm.yaml` (IDs are a `VERIFY` item) and run `setup` again.

## 5. Fill in the site
Replace every `[OWNER: ...]` in the trust pages by adding overrides under the brain's `content/<lang>/pages/` (see `docs/SITE.md`). Set `site.name` and `site.base_url` to your Pages URL or custom domain.

## 6. Dry run
Run `hourly` manually with **mode = dry-run** (fixtures and a mock LLM: no keys spent, nothing is committed or deployed). If Telegram is connected, the run report arrives in the chat too, so you can see the format before anything real happens.

## 7. Real run in review mode
Run `hourly` manually with the mode field empty (it uses `PUBLISH_MODE`, default `review`). Most hours end with zero LLM calls: "no new stories" is a normal result. The first run also deploys the trust pages (the site needs content to build).
When a story passes every gate, the run opens a pull request in the brain repo titled `Review: new articles <date-time>`.
With Telegram connected, each run sends **one quiet message** (no sound) listing every step with its result: ✅ ok, ❌ fail (it tried and did not get what it needed), 🛑 error (unexpected crash; type and file:line only), ⏭ skip (nothing to do, cool-down, queue, robots.txt). The GitHub workflow steps are listed first, then the engine steps, then one block per story. If a workflow step failed, or any step failed or errored, the message makes a sound. Set `notify.telegram.report_idle_runs: false` in the brain config to hear only about hours where something happened.

## 8. Approve on a phone
- With Telegram connected, each article that needs you arrives as its own message **with a sound**: title, summary, sources, and buttons. **Open PR** (and **View article**) opens GitHub, where you merge or close. A sensitive or failed-gate draft arrives as *pending* with its reasons and an **Open draft** button. Merging and closing still happen in GitHub; buttons that approve directly from Telegram are not built.
- Open the GitHub mobile app, the brain repo, the pull request. Read the article under *Files changed* (summary, confirmed / unclear / disputed block, sources).
- **Merge** to publish. The next `hourly` run deploys it (within the hour; run `hourly` by hand to deploy sooner). **Close without merging** to reject (after 72 hours an unmerged PR counts as rejected).
- Drafts that failed a gate or are sensitive (deaths, accusations, health, elections, religion, minors, financial advice) are in the brain's `pending/<lang>/` with the reasons in the front matter. To publish one after reading it: move it to `content/<lang>/` and delete the `pending:` block from its front matter (the GitHub web editor does both). To discard it, delete it.

## 9. Go automatic only when you trust it
**Updates.** A story that gets new confirmed facts is updated in place (same URL, same publication date, new "Updated" time, a dated timeline entry, and a corrections entry when something earlier was wrong). In review mode the update arrives as a PR that changes the existing file: merging it publishes the update, closing it keeps the old text live. A sensitive or gate-failing update lands in `pending/<lang>/<same-slug>.md`; approving it means moving it over `content/<lang>/<same-slug>.md` and deleting its `pending:` block. While an update is waiting, no second update of that story is started (it expires after 72 hours). Stories with no news for 48 hours become `stable`, after 7 days `closed` (see `state/stories.jsonl`).

After a few weeks of merging nearly everything unchanged, set the `PUBLISH_MODE` variable to `auto`. Sensitive and failed-gate articles still go to `pending/`. Switch back to `review` at any time.

## 10. Later
- Bengali font subset and AdSense: `docs/SITE.md`. Turn ads on only after approval.
- If scheduled runs stop after two idle months, `keepalive` (monthly) prevents it; you can also run it by hand.
- Logs are public: they contain only counts, story IDs and status words. Never paste prompts or drafts into issues.
- Telegram is private to you but not end-to-end encrypted: titles and summaries of unpublished drafts pass through Telegram's servers. Set `notify.telegram.include_summary: false` to send titles only, or `notify.telegram.enabled: false` to turn it off. Never share the bot token or paste it into issues (it controls the bot).
