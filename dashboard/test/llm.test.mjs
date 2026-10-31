// The LLM account pool in the dashboard: check BEFORE saving, no limit on the number of accounts, duplicates refused,
// usage and health shown, remove, re-check. Cloudflare and GitHub are in-memory fakes (fakes.mjs).
import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import { FakeKV, assets, fakeWorld, openSealed, addCf, acct, CF_MODELS } from "./fakes.mjs";

const PASS = "correct horse battery staple";
const mkEnv = () => ({ KV: new FakeKV(), ASSETS: assets, SESSION_SECRET: "s".repeat(48), DASHBOARD_PASSWORD: PASS, GH_TOKEN: "ghp_secret_token_value", BRAIN_REPO: "me/newsbot-brain", ENGINE_REPO: "me/newsbot-engine" });
const H = { "X-Requested-With": "newsbot", "Content-Type": "application/json" };
async function session(opts) {
  const env = mkEnv(), w = fakeWorld(opts);
  const lr = await worker.fetch(new Request("https://dash.example/api/login", { method: "POST", headers: { ...H, "CF-Connecting-IP": "1.2.3.4" }, body: JSON.stringify({ password: PASS }) }), env);
  const cookie = lr.headers.get("set-cookie").split(";")[0];
  const c = (path, { method = "GET", body } = {}) => worker.fetch(new Request("https://dash.example" + path, { method, headers: { ...(method === "GET" ? {} : H), Cookie: cookie, "CF-Connecting-IP": "1.2.3.4" }, body: body === undefined ? undefined : JSON.stringify(body) }), env);
  return { w, c };
}
const post = (c, path, body) => c(path, { method: "POST", body });
const today = new Date().toISOString().slice(0, 10);

test("add: every check runs first, then the secret (id + token) and the mirror variable are saved", async () => {
  const { c, w } = await session();
  const a = addCf(w, 1);
  const r = await post(c, "/api/llm/add", { label: "phone 1", account_id: a.id, token: a.token });
  assert.equal(r.status, 200, await r.clone().text());
  const out = await r.json();
  assert.equal(out.label, "PHONE_1"); assert.equal(out.status, "created"); assert.equal(out.test.ok, true);
  const names = out.test.steps.map((s) => s.name);
  assert.ok(names.includes("token and Workers AI")); assert.ok(names.some((n) => n.startsWith("model @cf/openai/gpt-oss-20b"))); assert.ok(names.some((n) => n.startsWith("embedding")));
  const stored = JSON.parse(openSealed(w, w.secrets.CFLLM_PHONE_1.encrypted_value));
  assert.deepEqual(stored, { a: a.id, t: a.token });                         // real round trip: what the engine will read
  assert.equal(w.vars.CFLLM_ID_PHONE_1, a.id);
  assert.ok(!JSON.stringify(await (await c("/api/audit")).json()).includes(a.token));          // the token is never logged
});

test("add: a wrong token, an unknown account, a missing model or an empty answer are refused and nothing is saved", async () => {
  const { c, w } = await session();
  const a = addCf(w, 1);
  let r = await post(c, "/api/llm/add", { label: "A", account_id: a.id, token: "tok_" + "y".repeat(30) });
  assert.equal(r.status, 422); assert.match((await r.json()).error, /token rejected|permission/);
  r = await post(c, "/api/llm/add", { label: "A", account_id: acct(99), token: a.token });
  assert.equal(r.status, 422); assert.match((await r.json()).error, /no such account/);
  const b = addCf(w, 2, { models: ["@cf/openai/gpt-oss-120b", "@cf/baai/bge-m3"] });          // 20b missing
  r = await post(c, "/api/llm/add", { label: "B", account_id: b.id, token: b.token });
  assert.equal(r.status, 422); assert.match((await r.json()).error, /not offered/);
  const e = addCf(w, 3, { empty: true });
  r = await post(c, "/api/llm/add", { label: "E", account_id: e.id, token: e.token });
  assert.equal(r.status, 422);
  const f = addCf(w, 4, { chat: 500 });
  assert.equal((await post(c, "/api/llm/add", { label: "F", account_id: f.id, token: f.token })).status, 422);
  for (const bad of [{ account_id: "xyz", token: a.token }, { account_id: a.id, token: "short" }, { account_id: a.id, token: "has space inside the token value" }]) assert.equal((await post(c, "/api/llm/add", { label: "G", ...bad })).status, 400);
  assert.deepEqual(Object.keys(w.secrets), []); assert.deepEqual(Object.keys(w.vars), ["PAGES_PROJECT"]);
});

test("add: an account whose allowance is spent today is only saved when you confirm", async () => {
  const { c, w } = await session();
  const a = addCf(w, 1, { chat: 429 });
  let r = await post(c, "/api/llm/add", { label: "A", account_id: a.id, token: a.token });
  const body = await r.json();
  assert.equal(r.status, 422); assert.equal(body.needsForce, true); assert.equal(body.test.ok, null); assert.ok(!w.secrets.CFLLM_A);
  r = await post(c, "/api/llm/add", { label: "A", account_id: a.id, token: a.token, force: true });
  assert.equal(r.status, 200); assert.ok(w.secrets.CFLLM_A);
});

test("add: no limit on how many accounts; the same Cloudflare account or the same name twice is refused", async () => {
  const { c, w } = await session();
  for (let n = 1; n <= 12; n++) { const a = addCf(w, n); assert.equal((await post(c, "/api/llm/add", { label: `P${n}`, account_id: a.id, token: a.token })).status, 200, `account ${n}`); }
  assert.equal(Object.keys(w.secrets).filter((k) => k.startsWith("CFLLM_")).length, 12);
  const o = await (await c("/api/llm")).json();
  assert.equal(o.accounts.length, 12); assert.equal(o.totals.accounts, 12); assert.ok(o.accounts.every((x) => x.complete));
  const same = addCf(w, 1);
  let r = await post(c, "/api/llm/add", { label: "OTHERNAME", account_id: same.id, token: same.token });
  assert.equal(r.status, 409); assert.match((await r.json()).error, /already added as P1/);
  const fresh = addCf(w, 50);
  r = await post(c, "/api/llm/add", { label: "P3", account_id: fresh.id, token: fresh.token });
  assert.equal(r.status, 409);
  r = await post(c, "/api/llm/add", { label: "P3", account_id: fresh.id, token: fresh.token, replace: true });
  assert.equal(r.status, 200); assert.equal((await r.json()).status, "updated");
  r = await post(c, "/api/llm/add", { account_id: addCf(w, 60).id, token: w.cf[acct(60)].token });          // no name: gets ACC1
  assert.equal((await r.json()).label, "ACC1");
});

test("test: shows every step and saves nothing", async () => {
  const { c, w } = await session();
  const a = addCf(w, 1);
  const r = await (await post(c, "/api/llm/test", { account_id: a.id, token: a.token })).json();
  assert.equal(r.ok, true); assert.ok(r.steps.length >= 5);
  assert.deepEqual(Object.keys(w.secrets), []);
  const bad = await (await post(c, "/api/llm/test", { account_id: a.id, token: "tok_" + "z".repeat(30) })).json();
  assert.equal(bad.ok, false);
});

test("overview: per-account usage from today's numbers, the last check, totals and masked ids", async () => {
  const usage = { day: today, counters: { "cf_p1/@cf/openai/gpt-oss-120b": { requests: 4, tokens: 20000, neurons: 3000, failures: 1 }, "cf_p1/@cf/openai/gpt-oss-20b": { requests: 2, tokens: 5000, neurons: 500 }, "cf_p2/@cf/openai/gpt-oss-120b": { requests: 1, tokens: 900, neurons: 100 } }, errors: { cf_p1: { reason: "status_429", at: today + "T08:00:00Z" } } };
  const health = { checked_at: today + "T09:00:00Z", summary: { accounts: 2, ok: 1, failed: 1 }, accounts: { p1: { ok: true, auth: "ok", models: { "@cf/openai/gpt-oss-120b": { status: "OK" } }, embedding: { status: "OK" } }, p2: { ok: false, auth: "token_rejected", models: {}, embedding: null } } };
  const { c, w } = await session({ files: { "state/usage.json": JSON.stringify(usage), "state/llm_health.json": JSON.stringify(health) } });
  for (const [n, l] of [[1, "P1"], [2, "P2"]]) { const a = addCf(w, n); await post(c, "/api/llm/add", { label: l, account_id: a.id, token: a.token }); }
  const o = await (await c("/api/llm")).json();
  const p1 = o.accounts.find((x) => x.label === "P1"), p2 = o.accounts.find((x) => x.label === "P2");
  assert.equal(p1.usage.neurons, 3500); assert.equal(p1.usage.requests, 6); assert.equal(p1.usage.failures, 1); assert.equal(p1.usage.pct, 35);
  assert.equal(p1.usage.models.length, 2); assert.equal(p1.error.reason, "status_429");
  assert.equal(p2.usage.neurons, 100); assert.equal(p2.health.ok, false);
  assert.match(p1.hint, /^aaaa….{4}$/); assert.ok(!JSON.stringify(o).includes(acct(1)));            // never the full id
  assert.equal(o.totals.neurons, 3600); assert.equal(o.totals.capacity, 18000); assert.equal(o.limit, 10000);
  assert.equal(o.health.summary.ok, 1); assert.deepEqual(o.models.sort(), ["@cf/openai/gpt-oss-120b", "@cf/openai/gpt-oss-20b"]); assert.equal(o.embedding, "@cf/baai/bge-m3");
});

test("overview: yesterday's usage is not shown as today's", async () => {
  const { c, w } = await session({ files: { "state/usage.json": JSON.stringify({ day: "2020-01-01", counters: { "cf_p1/m": { requests: 9, tokens: 9, neurons: 9999 } } }) } });
  const a = addCf(w, 1); await post(c, "/api/llm/add", { label: "P1", account_id: a.id, token: a.token });
  const o = await (await c("/api/llm")).json();
  assert.equal(o.accounts[0].usage.neurons, 0); assert.equal(o.totals.neurons, 0);
});

test("remove: the secret and its mirror variable go; an unknown name or a bad name is refused; re-check starts the workflow", async () => {
  const { c, w } = await session();
  const a = addCf(w, 1); await post(c, "/api/llm/add", { label: "P1", account_id: a.id, token: a.token });
  assert.equal((await post(c, "/api/llm/remove", { label: "lower" })).status, 400);
  assert.equal((await post(c, "/api/llm/remove", { label: "NOPE" })).status, 404);
  assert.equal((await post(c, "/api/llm/remove", { label: "P1" })).status, 200);
  assert.ok(!w.secrets.CFLLM_P1); assert.ok(!w.vars.CFLLM_ID_P1);
  assert.equal((await post(c, "/api/llm/check", {})).status, 200);
  assert.deepEqual(w.dispatches.map((x) => x.workflow), ["llm-check"]);
});

test("old provider keys are gone from the key list and the workflow list knows llm-check", async () => {
  const { c } = await session();
  const list = await (await c("/api/keys")).json();
  assert.ok(!list.keys.some((k) => /GROQ|GEMINI|OPENROUTER/.test(k.name)));
  assert.equal((await post(c, "/api/dispatch", { workflow: "llm-check" })).status, 200);
});
void CF_MODELS;
