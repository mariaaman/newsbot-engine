// Known keys, a live "does it work?" test for each (run BEFORE saving), and the rules for custom secret names.
import { HttpError } from "./util.js";

const t = async (url, init = {}) => {
  try { const r = await fetch(url, { ...init, signal: AbortSignal.timeout(8000) }); return { status: r.status, json: await r.json().catch(() => null) }; }
  catch { return { status: 0, json: null }; }
};
const bearer = (v) => ({ headers: { Authorization: `Bearer ${v}` } });
const verdict = (res, okCodes = [200]) => res.status === 0 ? { ok: null, detail: "could not reach the service" } : okCodes.includes(res.status) ? { ok: true, detail: "accepted" } : res.status === 401 || res.status === 403 ? { ok: false, detail: "rejected (wrong or no permission)" } : { ok: null, detail: `service answered ${res.status}` };

export const KEYS = [
  { name: "GEMINI_API_KEY", label: "Gemini API key", test: async (v) => verdict(await t("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1", { headers: { "x-goog-api-key": v } }), [200]) },
  { name: "GROQ_API_KEY", label: "Groq API key", test: async (v) => verdict(await t("https://api.groq.com/openai/v1/models", bearer(v))) },
  { name: "OPENROUTER_API_KEY", label: "OpenRouter API key", test: async (v) => verdict(await t("https://openrouter.ai/api/v1/key", bearer(v))) },
  { name: "TELEGRAM_BOT_TOKEN", label: "Telegram bot token", after: "setup-approve",
    test: async (v) => { if (!/^\d{5,}:[A-Za-z0-9_-]{30,}$/.test(v)) return { ok: false, detail: "does not look like a bot token" }; const r = await t(`https://api.telegram.org/bot${v}/getMe`); return r.status === 200 && r.json?.ok ? { ok: true, detail: "bot found" } : verdict(r); } },
  { name: "TELEGRAM_CHAT_ID", label: "Telegram chat id", after: "setup-approve", test: async (v) => /^-?\d{5,}$/.test(v) ? { ok: true, detail: "format ok" } : { ok: false, detail: "should be a number" } },
  { name: "CLOUDFLARE_API_TOKEN", label: "Cloudflare API token", test: async (v) => { const r = await t("https://api.cloudflare.com/client/v4/user/tokens/verify", bearer(v)); return r.status === 200 && r.json?.success ? { ok: true, detail: "token is active" } : r.status === 0 ? verdict(r) : { ok: null, detail: "could not verify here (an account-scoped token cannot be checked this way); saved only if you confirm" }; } },
  { name: "CLOUDFLARE_ACCOUNT_ID", label: "Cloudflare account id", test: async (v) => /^[0-9a-f]{32}$/.test(v) ? { ok: true, detail: "format ok" } : { ok: false, detail: "should be 32 hex characters" } },
  { name: "BRAIN_REPO_TOKEN", label: "Brain repo token", test: (v, env) => repoToken(v, env.BRAIN_REPO) },
  { name: "APPROVE_GH_TOKEN", label: "Approve Worker GitHub token", test: async (v, env) => { const a = await repoToken(v, env.BRAIN_REPO); return a.ok ? repoToken(v, env.ENGINE_REPO) : a; } },
  { name: "WORKFLOW_PAT", label: "Workflow PAT (unzip)", test: (v, env) => repoToken(v, env.ENGINE_REPO) },
];

async function repoToken(v, repo) {
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo || "")) return { ok: null, detail: "repo name not configured" };
  const r = await t(`https://api.github.com/repos/${repo}`, { headers: { Authorization: `Bearer ${v}`, Accept: "application/vnd.github+json", "User-Agent": "newsbot-dashboard" } });
  return r.status === 200 ? { ok: true, detail: "can read the repo" } : verdict(r);
}

export const SECRET_NAME = /^[A-Z][A-Z0-9_]{2,63}$/;
export const VAR_NAME = /^[A-Z][A-Z0-9_]{1,63}$/;

export function checkSecretName(name) {
  if (typeof name !== "string" || !SECRET_NAME.test(name) || name.startsWith("GITHUB_")) throw new HttpError(400, "secret names: capital letters, digits and _, 3-64 characters, not starting with GITHUB_");
}
export function checkSecretValue(value) {
  if (typeof value !== "string" || !value.trim() || value.length > 4096 || /[\r\n]/.test(value)) throw new HttpError(400, "the value must be one non-empty line (up to 4096 characters)");
}

export async function testKey(env, name, value) {
  const k = KEYS.find((x) => x.name === name);
  if (!k) return { ok: null, detail: "no automatic test for this key" };
  return k.test(value.trim(), env);
}
