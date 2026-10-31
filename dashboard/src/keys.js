// Known keys, a live "does it work?" test for each (run BEFORE saving), and the rules for custom secret names.
import { HttpError } from "./util.js";

const t = async (url, init = {}) => {
  try { const r = await fetch(url, { ...init, signal: AbortSignal.timeout(8000) }); return { status: r.status, json: await r.json().catch(() => null) }; }
  catch { return { status: 0, json: null }; }
};
const bearer = (v) => ({ headers: { Authorization: `Bearer ${v}` } });
const verdict = (res, okCodes = [200]) => res.status === 0 ? { ok: null, detail: "could not reach the service" } : okCodes.includes(res.status) ? { ok: true, detail: "accepted" } : res.status === 401 || res.status === 403 ? { ok: false, detail: "rejected (wrong or no permission)" } : { ok: null, detail: `service answered ${res.status}` };

export const KEYS = [
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

// ---------- Cloudflare LLM accounts (any number; one secret CFLLM_<LABEL> + one mirror variable CFLLM_ID_<LABEL> each) ----------
export const CFLLM_PREFIX = "CFLLM_", CFLLM_ID_PREFIX = "CFLLM_ID_";
export const LABEL_RE = /^[A-Z0-9_]{1,32}$/;
export const ACCOUNT_RE = /^[0-9a-f]{32}$/;
const CF = "https://api.cloudflare.com/client/v4";

export function normaliseLabel(raw) {
  const l = String(raw ?? "").trim().toUpperCase().replace(/[^A-Z0-9_]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 32);
  return l;
}

const cfErr = (r) => { const m = r.json?.errors?.[0]?.message; return typeof m === "string" ? m.replace(/\s+/g, " ").slice(0, 120) : ""; };
const short = (r) => `${r.status}${cfErr(r) ? ": " + cfErr(r) : ""}`;

/** Every check that must pass BEFORE an account is saved (and whose result the panel shows step by step).
 *  models: chat model IDs from llm.yaml; embedding: its model ID or null. Returns { ok: true|false|null, steps: [{name, ok, detail}] }.
 *  ok=false is a real failure (never saved); ok=null means "could not tell" (unreachable, allowance already used today). */
export async function checkCloudflareAccount(accountId, token, { models = [], embedding = null } = {}) {
  const steps = [];
  const add = (name, ok, detail) => { steps.push({ name, ok, detail }); return ok; };
  if (!ACCOUNT_RE.test(accountId || "")) { add("account id", false, "should be 32 characters of 0-9 a-f"); return finish(steps); }
  if (typeof token !== "string" || token.trim().length < 20 || /\s/.test(token.trim())) { add("API token", false, "looks too short or contains spaces"); return finish(steps); }
  token = token.trim();
  add("format", true, "account id and token look right");
  const auth = { headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" } };
  const call = async (path, init = {}) => {
    try { const r = await fetch(CF + path, { ...auth, ...init, signal: AbortSignal.timeout(25000) }); return { status: r.status, json: await r.json().catch(() => null) }; }
    catch { return { status: 0, json: null }; }
  };

  let ids = [];
  for (let page = 1; page <= 5; page++) {
    const r = await call(`/accounts/${accountId}/ai/models/search?per_page=100&page=${page}`);
    if (page === 1) {
      if (r.status === 0) { add("token and Workers AI", null, "could not reach Cloudflare"); return finish(steps); }
      if (r.status === 401 || r.status === 403) { add("token and Workers AI", false, "token rejected, or it has no Workers AI permission (create it with the “Workers AI” template)"); return finish(steps); }
      if (r.status === 404) { add("token and Workers AI", false, "no such account id"); return finish(steps); }
      if (r.status !== 200 || !r.json?.success) { add("token and Workers AI", false, `Cloudflare answered ${short(r)}`); return finish(steps); }
    } else if (r.status !== 200) break;
    const got = (r.json?.result || []).map((m) => m?.name).filter((n) => typeof n === "string");
    ids = ids.concat(got);
    if (got.length < 100) break;
  }
  add("token and Workers AI", true, `accepted, ${ids.length} models visible`);

  for (const m of models.slice(0, 8)) {
    if (!ids.includes(m)) { add(`model ${m}`, false, "not offered to this account"); continue; }
    const r = await call(`/accounts/${accountId}/ai/v1/chat/completions`, { method: "POST", body: JSON.stringify({ model: m, messages: [{ role: "user", content: "Reply with the single word OK." }], max_tokens: 256 }) });
    const text = r.json?.choices?.[0]?.message?.content;
    if (r.status === 200 && typeof text === "string" && text.trim()) add(`model ${m}`, true, "answered");
    else if (r.status === 200) add(`model ${m}`, false, "answered with nothing (thinking used every token)");
    else if (r.status === 429) add(`model ${m}`, null, "today's free allowance looks used up (or rate limit); works again after 00:00 UTC");
    else if (r.status === 0) add(`model ${m}`, null, "no answer in time");
    else add(`model ${m}`, false, `Cloudflare answered ${short(r)}`);
  }
  if (embedding) {
    if (!ids.includes(embedding)) add(`embedding ${embedding}`, false, "not offered to this account");
    else {
      const r = await call(`/accounts/${accountId}/ai/run/${embedding}`, { method: "POST", body: JSON.stringify({ text: ["test"] }) });
      const ok = r.status === 200 && Array.isArray(r.json?.result?.data) && r.json.result.data.length === 1;
      add(`embedding ${embedding}`, ok ? true : r.status === 429 || r.status === 0 ? null : false, ok ? "answered" : `Cloudflare answered ${short(r)}`);
    }
  }
  return finish(steps);
}

function finish(steps) {
  const ok = steps.some((s) => s.ok === false) ? false : steps.some((s) => s.ok === null) ? null : true;
  return { ok, steps };
}
