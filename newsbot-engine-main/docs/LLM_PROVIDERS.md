# LLM: Cloudflare Workers AI account pool

The only LLM provider is **Cloudflare Workers AI**. Any number of Cloudflare accounts can be added (nothing in the code
limits the count; GitHub itself allows about 100 secrets per repository). All accounts form one pool and are used
**equally**: the account that has used the smaller share of today's allowance goes first, accounts on the same level
take turns. When an account reaches 90% of its daily allowance, answers 429, or fails, the next account takes over.
When every account is used up the existing queue keeps the stories and the next run (after 00:00 UTC) continues.

## What to bring from each Cloudflare account
1. **Account ID**: dash.cloudflare.com, open the account, copy the 32-character *Account ID*.
2. **API token**: My Profile, API Tokens, Create Token, template **Workers AI**, Create Token. It is shown once. The token needs
   nothing else (no Pages, no DNS): a leaked LLM token can only spend that account's free allowance.
3. In the dashboard open **কী → LLM অ্যাকাউন্ট পুল**, enter name (optional), Account ID, token, press **পরীক্ষা করে যোগ করুন**.

## What is checked before anything is saved (server side, every time)
format of id and token; token accepted and Workers AI visible; every model used in `config/llm.yaml` is offered to
that account; one tiny real call per model and one embedding call answer. One failed step means nothing is saved.
"Could not tell" (Cloudflare unreachable, or the allowance of the day is already spent) is saved only after you confirm.
The same Cloudflare account added twice, or the same name twice, is refused.

## How accounts are stored and reach the engine
Dashboard: secret `CFLLM_<NAME>` = `{"a": "<account id>", "t": "<token>"}` (write-only at GitHub, nobody can read it back) and a
variable `CFLLM_ID_<NAME>` = the account id (used for the list, the duplicate check and the masked display).
Workflows: one step, `scripts/collect_llm_accounts.py`, receives the secrets context, keeps only `CFLLM_*`, writes an
owner-only file and exports only its **path** (`NEWSBOT_CF_ACCOUNTS_FILE`). Without any account the deploy account
(`CLOUDFLARE_*`) is used as a single-account fallback; once a dashboard account exists the deploy account is not used for LLM work.

## What the dashboard shows
Per account: neurons used today and % of the daily limit, requests, tokens, failures, the last error, and per model the same.
Totals for the pool. **এখনই যাচাই** starts workflow `llm-check`, which re-tests every account and stores
`state/llm_health.json` (labels and status words only). The probes are tiny and are not added to the usage numbers.

## Model per task (config/llm.yaml, editable in the dashboard settings)
`extract`, `qa_check`: tier *small* (gpt-oss-20b, then gpt-oss-120b). `verify`, `write_bn`, `write_en`: tier *strong* (gpt-oss-120b).
An answer that fails the JSON schema twice moves to the next **different** model in the chain. `model_params` can lower the
thinking effort of reasoning models (thinking tokens are billed as output); a model that rejects a field is asked again without it.
Every model ID, rate and limit in that file is marked `VERIFY`: Cloudflare changes them. `neuron_rates` is neurons per million
tokens (dollars per million tokens / 0.000011); unknown models are counted at the dearest `default` rate so the allowance is never under-counted.

## Commands
`python -m newsbot health` (model IDs on every account), `llmtest` (realistic calls, costs more), `llmcheck` (tiny calls, writes the
report the dashboard shows). Exit code 3 means a problem.
