# LLM providers: OpenRouter, Gemini, Groq, Workers AI

Chains live in the brain repo (`config/llm.yaml`); a provider is used only if its secret exists.

| Secret (repo Settings > Secrets > Actions) | Provider | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | OpenRouter | no card. Free `:free` models: ~20 requests/min, ~50/day (1,000/day after a one-time $10 credit; then raise `requests_per_day.openrouter`). In openrouter.ai > Settings > Privacy allow free endpoints, otherwise free models answer 404. |
| `GEMINI_API_KEY` | Google Gemini | key from aistudio.google.com. `gemini-2.5-*` was shut down in October 2026: use `gemini-3.5-flash` / `gemini-3.1-flash-lite`. |
| `GROQ_API_KEY` | Groq | free tier: 8,000 tokens/minute, 200,000/day. |
| `CLOUDFLARE_*` | Workers AI | embeddings (multilingual clustering) + last fallback. 10,000 neurons/day. |

## Which one works? Run the live test
Actions > **setup** > Run workflow. The last step, "Live test", sends two small probes to every configured model:
```
llmtest provider=openrouter model=openai/gpt-oss-120b:free probe=tiny status=OK tokens=...
llmtest provider=gemini model=gemini-3.5-flash probe=realistic status=FAIL reason=status_404
llmtest hint provider=gemini ids=gemini-3.5-flash,...        (real model IDs to copy into llm.yaml)
llmtest summary working=3 failed=1 skipped=0
```
`status_401/403` = bad key or blocked region/account; `status_404` = unknown or retired model ID (see the hint line, or
the OpenRouter privacy setting); `status_429` = limit reached; `reason=…json_validate_failed` = the model cannot do JSON mode.
Only provider, model ID, probe name, status and token counts are logged, never keys, prompts or answers.
