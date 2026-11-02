// LLM account pool: list accounts with usage and health, check an account BEFORE saving it, add, remove, start a re-check.
// The list of accounts lives in the Worker's KV (key llm:accounts: {LABEL: {a: account id, t: token, added}}). Every change also
// pushes the whole list as ONE GitHub secret, CFLLM_ACCOUNTS, which the workflows read like any other secret (no secrets-context dump).
// No limit on how many accounts (one GitHub secret holds about 300). The token is never sent back to the browser.
import * as gh from "./github.js";
import { getFile } from "./github.js";
import { reader } from "./yamledit.js";
import { TIERS } from "./schema.js";
import { HttpError, must, isObj } from "./util.js";
import { CFLLM_SECRET, KV_ACCOUNTS, ACCOUNT_RE, LABEL_RE, normaliseLabel, checkCloudflareAccount } from "./keys.js";

const parse = (s, fallback) => { try { return JSON.parse(s); } catch { return fallback; } };
const providerOf = (label) => "cf_" + label.toLowerCase();
const mask = (id) => (typeof id === "string" && id.length >= 8 ? `${id.slice(0, 4)}…${id.slice(-4)}` : "");
const MAX_SECRET = 45000;                                  // GitHub allows 48 KB per secret

async function readList(env) {
  const raw = await env.KV.get(KV_ACCOUNTS);
  const v = raw ? parse(raw, {}) : {};
  return isObj(v) ? v : {};
}
const forEngine = (list) => JSON.stringify(Object.fromEntries(Object.entries(list).map(([l, v]) => [l, { a: v.a, t: v.t }])));

/** Saves the list: KV first, then the GitHub secret; when the secret cannot be written the KV is put back as it was. */
async function save(env, before, after) {
  const labels = Object.keys(after);
  if (labels.length) must(forEngine(after).length <= MAX_SECRET, 400, "too many accounts for one GitHub secret (about 300)");
  await (labels.length ? env.KV.put(KV_ACCOUNTS, JSON.stringify(after)) : env.KV.delete(KV_ACCOUNTS));
  try {
    return labels.length ? await gh.putSecret(env, CFLLM_SECRET, forEngine(after)) : await gh.deleteSecret(env, CFLLM_SECRET);
  } catch (e) {
    await (Object.keys(before).length ? env.KV.put(KV_ACCOUNTS, JSON.stringify(before)) : env.KV.delete(KV_ACCOUNTS));
    throw new HttpError(502, "GitHub did not accept the change, nothing was saved (" + (e && e.message ? String(e.message).slice(0, 120) : "error") + ")");
  }
}

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
  const [secrets, list, llm, health, snap, runs] = await Promise.all([
    gh.listSecrets(env), readList(env), getFile(env, "config/llm.yaml"), getFile(env, "state/llm_health.json").catch(() => null),
    gh.brainSnapshot(env), gh.runs(env, 30).catch(() => []),
  ]);
  return { secret: secrets.find((x) => x.name === CFLLM_SECRET) || null, list, llm, health: health ? parse(health.text, null) : null, usage: parse(snap.usage, {}), runs };
}

export async function overview(env) {
  const { secret, list, llm, health, usage, runs } = await parts(env);
  const cfg = poolModels(llm && llm.text);
  const today = new Date().toISOString().slice(0, 10);
  const counters = usage.day === today ? usage.counters || {} : {};          // yesterday's numbers are not today's
  const errors = usage.day === today ? usage.errors || {} : {};
  const accounts = [];
  for (const [label, info] of Object.entries(list).sort(([a], [b]) => (a < b ? -1 : 1))) {
    const prov = providerOf(label), models = [];
    let requests = 0, tokens = 0, neurons = 0, failures = 0;
    for (const [k, c] of Object.entries(counters)) {
      if (k.split("/", 1)[0] !== prov) continue;
      requests += c.requests || 0; tokens += c.tokens || 0; neurons += c.neurons || 0; failures += c.failures || 0;
      models.push({ model: k.slice(prov.length + 1), requests: c.requests || 0, tokens: c.tokens || 0, neurons: Math.round(c.neurons || 0), failures: c.failures || 0 });
    }
    const h = health?.accounts?.[label.toLowerCase()] || null;
    accounts.push({ label, provider: prov, complete: !!secret, updated: info.added || null, hint: mask(info.a),
      usage: { requests, tokens, neurons: Math.round(neurons), failures, pct: cfg.limit ? Math.min(999, Math.round((neurons / cfg.limit) * 100)) : null, models },
      error: errors[prov] || null, health: h });
  }
  const used = accounts.reduce((a, x) => a + x.usage.neurons, 0);
  const lastRun = runs.find((r) => r.name === "llm-check") || null;
  return {
    accounts, synced: !Object.keys(list).length || !!secret, secret_updated: secret ? secret.updated : null, day: usage.day || null, today, limit: cfg.limit, stop: cfg.stop, models: cfg.models, embedding: cfg.embedding,
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

/** Re-checks everything server-side (the browser is never trusted), refuses duplicates, then saves. */
export async function add(env, b, audit) {
  must(isObj(b), 400, "bad request");
  const accountId = String(b.account_id || "").trim().toLowerCase();
  must(ACCOUNT_RE.test(accountId), 400, "the account id should be 32 characters of 0-9 a-f");
  must(typeof b.token === "string" && b.token.trim().length >= 20 && !/\s/.test(b.token.trim()) && b.token.length <= 2000, 400, "the API token looks wrong");
  const before = await readList(env);
  let label = normaliseLabel(b.label);
  if (!label) { let n = 1; while (before["ACC" + n]) n++; label = "ACC" + n; }
  must(LABEL_RE.test(label), 400, "the name: capital letters, digits and _ (up to 32)");
  if (before[label]) must(b.replace === true, 409, `the name ${label} is already used. Pick another name, or choose “replace” to change its token`);
  for (const [l, v] of Object.entries(before)) must(v.a !== accountId || l === label, 409, `this Cloudflare account is already added as ${l}`);
  const c = await cfg(env);
  const res = await checkCloudflareAccount(accountId, b.token, { models: c.models, embedding: c.embedding });
  if (res.ok === false) throw new HttpError(422, "Not saved: " + (res.steps.find((s) => s.ok === false)?.detail || "the check failed"), { test: res });
  if (res.ok === null && b.force !== true) throw new HttpError(422, "Could not fully confirm this account. Nothing was saved. Save anyway only if you are sure.", { test: res, needsForce: true });
  const after = { ...before, [label]: { a: accountId, t: b.token.trim(), added: new Date().toISOString() } };
  const status = await save(env, before, after);
  await audit(`llm account ${before[label] ? "updated" : "created"}`, label);
  return { label, status: before[label] ? "updated" : "created", test: res };
}

export async function remove(env, b, audit) {
  must(isObj(b) && typeof b.label === "string" && LABEL_RE.test(b.label), 400, "bad account name");
  const before = await readList(env);
  must(before[b.label], 404, "no such account");
  const { [b.label]: _gone, ...after } = before;
  await save(env, before, after);
  await audit("llm account removed", b.label);
  return { ok: true };
}

/** Pushes the stored list to GitHub again (after a failed save, or when the secret was deleted by hand). */
export async function sync(env, audit) {
  const list = await readList(env);
  must(Object.keys(list).length, 404, "no accounts to push");
  const status = await save(env, list, list);
  await audit("llm accounts synced", String(Object.keys(list).length));
  return { ok: true, status };
}

export async function checkNow(env, audit) {
  await gh.dispatch(env, "llm-check");
  await audit("run workflow", "llm-check");
  return { ok: true };
}
