// LLM account pool: list accounts with usage and health, check an account BEFORE saving it, add, remove, start a re-check.
// Each account = secret CFLLM_<LABEL> ({"a": account id, "t": token}; GitHub keeps it, nobody can read it back) + a mirror
// variable CFLLM_ID_<LABEL> (the account id; lets this panel list accounts and refuse a duplicate). No limit on how many.
import * as gh from "./github.js";
import { getFile } from "./github.js";
import { reader } from "./yamledit.js";
import { TIERS } from "./schema.js";
import { HttpError, must, isObj } from "./util.js";
import { CFLLM_PREFIX, CFLLM_ID_PREFIX, ACCOUNT_RE, LABEL_RE, normaliseLabel, checkCloudflareAccount } from "./keys.js";

const parse = (s, fallback) => { try { return JSON.parse(s); } catch { return fallback; } };
const providerOf = (label) => "cf_" + label.toLowerCase();
const mask = (id) => (typeof id === "string" && id.length >= 8 ? `${id.slice(0, 4)}…${id.slice(-4)}` : "");

/** Chat models and the embedding model that config/llm.yaml sends to the Workers AI pool. */
export function poolModels(llmText) {
  const out = { models: [], embedding: null, stop: 0.9, limit: 10000, overrides: {} };
  if (!llmText) return out;
  const rd = reader(llmText);
  for (const tier of TIERS) for (let i = 0; rd(["llm", "tiers", tier, i, "provider"]) !== undefined; i++)
    if (rd(["llm", "tiers", tier, i, "provider"]) === "workers_ai") { const m = rd(["llm", "tiers", tier, i, "model"]); if (m && !out.models.includes(m)) out.models.push(m); }
  if (rd(["llm", "embedding", "provider"]) === "workers_ai") out.embedding = rd(["llm", "embedding", "model"]) || null;
  const sf = rd(["llm", "budget", "stop_fraction"]), lim = rd(["llm", "budget", "neurons_per_day_per_account"]);
  if (typeof sf === "number") out.stop = sf;
  if (typeof lim === "number") out.limit = lim;
  return out;
}

async function parts(env) {
  const [secrets, vars, llm, health, snap, runs] = await Promise.all([
    gh.listSecrets(env), gh.listVars(env), getFile(env, "config/llm.yaml"), getFile(env, "state/llm_health.json").catch(() => null),
    gh.brainSnapshot(env), gh.runs(env, 30).catch(() => []),
  ]);
  return { secrets, vars, llm, health: health ? parse(health.text, null) : null, usage: parse(snap.usage, {}), runs };
}

export async function overview(env) {
  const { secrets, vars, llm, health, usage, runs } = await parts(env);
  const cfg = poolModels(llm && llm.text);
  const today = new Date().toISOString().slice(0, 10);
  const counters = usage.day === today ? usage.counters || {} : {};          // yesterday's numbers are not today's
  const errors = usage.day === today ? usage.errors || {} : {};
  const labels = new Map();
  for (const s of secrets) if (s.name.startsWith(CFLLM_PREFIX) && !s.name.startsWith(CFLLM_ID_PREFIX)) labels.set(s.name.slice(CFLLM_PREFIX.length), { updated: s.updated, hasSecret: true });
  for (const v of vars) if (v.name.startsWith(CFLLM_ID_PREFIX)) { const l = v.name.slice(CFLLM_ID_PREFIX.length); labels.set(l, { ...(labels.get(l) || {}), id: v.value }); }

  const accounts = [];
  for (const [label, info] of [...labels.entries()].sort()) {
    const prov = providerOf(label), models = [];
    let requests = 0, tokens = 0, neurons = 0, failures = 0;
    for (const [k, c] of Object.entries(counters)) {
      if (k.split("/", 1)[0] !== prov) continue;
      requests += c.requests || 0; tokens += c.tokens || 0; neurons += c.neurons || 0; failures += c.failures || 0;
      models.push({ model: k.slice(prov.length + 1), requests: c.requests || 0, tokens: c.tokens || 0, neurons: Math.round(c.neurons || 0), failures: c.failures || 0 });
    }
    const h = health?.accounts?.[label.toLowerCase()] || null;
    accounts.push({ label, provider: prov, complete: !!info.hasSecret && !!info.id, updated: info.updated || null, hint: mask(info.id),
      usage: { requests, tokens, neurons: Math.round(neurons), failures, pct: cfg.limit ? Math.min(999, Math.round((neurons / cfg.limit) * 100)) : null, models },
      error: errors[prov] || null, health: h });
  }
  const used = accounts.reduce((a, x) => a + x.usage.neurons, 0);
  const lastRun = runs.find((r) => r.name === "llm-check") || null;
  return {
    accounts, day: usage.day || null, today, limit: cfg.limit, stop: cfg.stop, models: cfg.models, embedding: cfg.embedding,
    totals: { accounts: accounts.length, neurons: used, capacity: cfg.limit ? Math.round(cfg.limit * cfg.stop * accounts.filter((a) => a.complete).length) : null,
      requests: accounts.reduce((a, x) => a + x.usage.requests, 0), failures: accounts.reduce((a, x) => a + x.usage.failures, 0) },
    health: health ? { checked_at: health.checked_at, summary: health.summary } : null,
    check: lastRun ? { status: lastRun.status, conclusion: lastRun.conclusion, created: lastRun.created, url: lastRun.url } : null,
  };
}

async function cfg(env) {
  const f = await getFile(env, "config/llm.yaml");
  return poolModels(f && f.text);
}

/** Step-by-step test; nothing is saved. */
export async function test(env, b) {
  must(isObj(b), 400, "bad request");
  const c = await cfg(env);
  return checkCloudflareAccount(String(b.account_id || "").trim().toLowerCase(), b.token, { models: c.models, embedding: c.embedding });
}

/** Re-checks everything server-side (the browser is never trusted), refuses duplicates, then saves secret + mirror variable. */
export async function add(env, b, audit) {
  must(isObj(b), 400, "bad request");
  const accountId = String(b.account_id || "").trim().toLowerCase();
  must(ACCOUNT_RE.test(accountId), 400, "the account id should be 32 characters of 0-9 a-f");
  must(typeof b.token === "string" && b.token.trim().length >= 20 && !/\s/.test(b.token.trim()) && b.token.length <= 2000, 400, "the API token looks wrong");
  const [secrets, vars] = await Promise.all([gh.listSecrets(env), gh.listVars(env)]);
  const taken = new Set(secrets.filter((s) => s.name.startsWith(CFLLM_PREFIX) && !s.name.startsWith(CFLLM_ID_PREFIX)).map((s) => s.name.slice(CFLLM_PREFIX.length)));
  const idOf = new Map(vars.filter((v) => v.name.startsWith(CFLLM_ID_PREFIX)).map((v) => [v.name.slice(CFLLM_ID_PREFIX.length), v.value]));
  let label = normaliseLabel(b.label);
  if (!label) { let n = 1; while (taken.has("ACC" + n)) n++; label = "ACC" + n; }
  must(LABEL_RE.test(label), 400, "the name: capital letters, digits and _ (up to 32)");
  if (taken.has(label)) must(b.replace === true, 409, `the name ${label} is already used. Pick another name, or choose “replace” to change its token`);
  for (const [l, id] of idOf) must(id !== accountId || l === label, 409, `this Cloudflare account is already added as ${l}`);
  const c = await cfg(env);
  const res = await checkCloudflareAccount(accountId, b.token, { models: c.models, embedding: c.embedding });
  if (res.ok === false) throw new HttpError(422, "Not saved: " + (res.steps.find((s) => s.ok === false)?.detail || "the check failed"), { test: res });
  if (res.ok === null && b.force !== true) throw new HttpError(422, "Could not fully confirm this account. Nothing was saved. Save anyway only if you are sure.", { test: res, needsForce: true });
  const status = await gh.putSecret(env, CFLLM_PREFIX + label, JSON.stringify({ a: accountId, t: b.token.trim() }));
  let mirror = "ok";
  try { await gh.upsertVar(env, CFLLM_ID_PREFIX + label, accountId); } catch { mirror = "failed"; }
  await audit(`llm account ${status}`, label);
  return { label, status, mirror, test: res };
}

export async function remove(env, b, audit) {
  must(isObj(b) && typeof b.label === "string" && LABEL_RE.test(b.label), 400, "bad account name");
  const s = await gh.deleteSecret(env, CFLLM_PREFIX + b.label);
  const v = await gh.deleteVar(env, CFLLM_ID_PREFIX + b.label);
  must(s === "deleted" || v === "deleted", 404, "no such account");
  await audit("llm account removed", b.label);
  return { ok: true };
}

export async function checkNow(env, audit) {
  await gh.dispatch(env, "llm-check");
  await audit("run workflow", "llm-check");
  return { ok: true };
}
