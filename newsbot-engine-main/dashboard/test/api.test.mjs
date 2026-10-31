import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import { FakeKV, assets, fakeWorld, openSealed, draft, live, NEWS } from "./fakes.mjs";
import { get } from "../src/yamledit.js";
import { base32decode, hotp } from "../src/totp.js";

const PASS = "correct horse battery staple";
const mkEnv = () => ({ KV: new FakeKV(), ASSETS: assets, SESSION_SECRET: "s".repeat(48), DASHBOARD_PASSWORD: PASS, GH_TOKEN: "ghp_secret_token_value", BRAIN_REPO: "me/newsbot-brain", ENGINE_REPO: "me/newsbot-engine" });
const H = { "X-Requested-With": "newsbot", "Content-Type": "application/json" };
const call = (env, path, { method = "GET", body, cookie, headers = {}, ip = "1.2.3.4" } = {}) =>
  worker.fetch(new Request("https://dash.example" + path, { method, headers: { ...(method === "GET" ? {} : H), ...(cookie ? { Cookie: cookie } : {}), "CF-Connecting-IP": ip, ...headers }, body: body === undefined ? undefined : JSON.stringify(body) }), env);
const login = async (env, extra = {}) => {
  const r = await call(env, "/api/login", { method: "POST", body: { password: PASS, ...extra } });
  assert.equal(r.status, 200, await r.clone().text());
  return r.headers.get("set-cookie").split(";")[0];
};
const j = (r) => r.json();

test("static pages: login page for strangers, the app only with a session, security headers everywhere", async () => {
  const env = mkEnv();
  let r = await call(env, "/");
  assert.match(await r.text(), /NewsBot Control/); assert.equal(r.headers.get("content-security-policy").includes("script-src 'self'"), true);
  assert.equal(r.headers.get("x-frame-options"), "DENY");
  assert.equal((await call(env, "/app.js")).status, 401);
  assert.equal((await call(env, "/style.css")).status, 200);
  const cookie = await login(env);
  r = await call(env, "/", { cookie });
  assert.match(await r.text(), /id="view"/);
  r = await call(env, "/app.js", { cookie });
  assert.equal(r.status, 200); assert.match(r.headers.get("content-type"), /javascript/);
  assert.equal((await call(env, "/lib.js", { cookie })).status, 200);
  assert.equal((await call(env, "/nope")).status, 404);
});

test("login: wrong password, lock-out after 5 tries, CSRF guard, session cookie flags, log out everywhere", async () => {
  const env = mkEnv(); fakeWorld();
  assert.equal((await call(env, "/api/overview")).status, 401);
  for (let i = 0; i < 5; i++) assert.equal((await call(env, "/api/login", { method: "POST", body: { password: "wrong" } })).status, 401);
  assert.equal((await call(env, "/api/login", { method: "POST", body: { password: PASS } })).status, 429);          // even the right password is refused now
  assert.equal((await call(env, "/api/login", { method: "POST", body: { password: PASS }, ip: "9.9.9.9" })).status, 200);   // another address is unaffected
  const r = await call(env, "/api/login", { method: "POST", body: { password: PASS }, ip: "8.8.8.8" });
  assert.match(r.headers.get("set-cookie"), /HttpOnly; Secure; SameSite=Strict/);
  const cookie = r.headers.get("set-cookie").split(";")[0];
  assert.equal((await call(env, "/api/overview", { cookie })).status, 200);
  assert.equal((await call(env, "/api/logout-all", { method: "POST", cookie, body: {} })).status, 200);
  assert.equal((await call(env, "/api/overview", { cookie })).status, 401);
  const c2 = await login(env, {}).catch(() => null);                                                                  // same ip is locked, so use a fresh one
  void c2;
  // CSRF: a state-changing call without the custom header, or from another origin, is refused
  const c3 = (await call(env, "/api/login", { method: "POST", body: { password: PASS }, ip: "7.7.7.7" })).headers.get("set-cookie").split(";")[0];
  assert.equal((await worker.fetch(new Request("https://dash.example/api/dispatch", { method: "POST", headers: { Cookie: c3, "Content-Type": "application/json" }, body: "{}" }), env)).status, 403);
  assert.equal((await call(env, "/api/dispatch", { method: "POST", cookie: c3, body: { workflow: "hourly" }, headers: { Origin: "https://evil.example" } })).status, 403);
  assert.equal((await call(env, "/api/dispatch", { method: "POST", cookie: c3, body: { workflow: "hourly" }, headers: { "Sec-Fetch-Site": "cross-site" } })).status, 403);
  // a tampered cookie is not accepted
  assert.equal((await call(env, "/api/overview", { cookie: c3.slice(0, -2) + "xx" })).status, 401);
});

test("two-step: enrol, then the code is required and a used code cannot be replayed", async () => {
  const env = mkEnv(); fakeWorld();
  const cookie = await login(env, {}).catch(() => null) || "";
  const begin = await j(await call(env, "/api/totp/begin", { method: "POST", cookie, body: { password: PASS } }));
  assert.match(begin.uri, /^otpauth:\/\/totp\//);
  const step = () => Math.floor(Date.now() / 30000);
  const code = (n) => hotp(base32decode(begin.secret), n);
  assert.equal((await call(env, "/api/totp/confirm", { method: "POST", cookie, body: { code: "000000" } })).status, 400);
  assert.equal((await call(env, "/api/totp/confirm", { method: "POST", cookie, body: { code: await code(step()) } })).status, 200);
  assert.equal((await j(await call(env, "/api/session"))).totp, true);
  assert.equal((await call(env, "/api/overview", { cookie })).status, 401);                                          // enrolling logs the other sessions out
  assert.equal((await call(env, "/api/login", { method: "POST", body: { password: PASS }, ip: "5.5.5.5" })).status, 401);   // no code
  assert.equal((await call(env, "/api/login", { method: "POST", body: { password: PASS, code: await code(step()) }, ip: "5.5.5.5" })).status, 401);   // that step was already used at enrolment
  const next = await code(step() + 1);
  assert.equal((await call(env, "/api/login", { method: "POST", body: { password: PASS, code: next }, ip: "5.5.5.5" })).status, 200);
  assert.equal((await call(env, "/api/login", { method: "POST", body: { password: PASS, code: next }, ip: "6.6.6.6" })).status, 401);   // replay
});

async function session(world = fakeWorld()) {
  const env = mkEnv();
  const cookie = await login(env);
  return { env, cookie, w: world, c: (path, o = {}) => call(env, path, { cookie, ...o }) };
}

test("overview: counts, mode, site check, runs, usage and the secret names (never values)", async () => {
  const { c } = await session();
  const o = await j(await c("/api/overview"));
  assert.deepEqual(o.counts, { pending: { bn: 1, en: 2 }, published: { bn: 0, en: 1 } });
  assert.equal(o.mode, "review"); assert.equal(o.site.url, "https://news-site.pages.dev"); assert.equal(o.site.check.status, 200);
  assert.equal(o.lastHourly.conclusion, "success"); assert.equal(o.lastDeploy.conclusion, "failure");
  assert.equal(o.usage.counters["cf_phone1/@cf/openai/gpt-oss-120b"].tokens, 41000);
  assert.deepEqual(o.secrets, ["BRAIN_REPO_TOKEN"]);
});

test("settings: read, change one value with the comment kept, stale version refused, bad values refused, CI validation started", async () => {
  const { c, w } = await session();
  const d = await j(await c("/api/settings"));
  const mode = d.fields.find((f) => f.path.join(".") === "publish.mode");
  assert.equal(mode.value, "review"); assert.deepEqual(mode.options, ["review", "auto"]);
  assert.ok(d.chains.strong.length >= 1); assert.deepEqual(d.providers, ["workers_ai"]);
  const sha = d.files["config/newsbot.yaml"].sha;
  let r = await c("/api/settings", { method: "POST", body: { changes: [{ file: "config/newsbot.yaml", kind: "field", path: ["publish", "mode"], value: "auto" }, { file: "config/newsbot.yaml", kind: "field", path: ["limits", "max_new_per_day"], value: 9 }], versions: { "config/newsbot.yaml": sha } } });
  assert.equal(r.status, 200);
  assert.equal(get(w.files["config/newsbot.yaml"], ["publish", "mode"]), "auto");
  assert.equal(get(w.files["config/newsbot.yaml"], ["limits", "max_new_per_day"]), 9);
  assert.match(w.files["config/newsbot.yaml"], /mode: auto +# review \| auto/);                                       // the comment survived
  assert.equal(w.commits.length, 1); assert.match(w.commits[0].message, /^dashboard: publish\.mode=auto; limits\.max_new_per_day=9/);
  assert.deepEqual(w.dispatches.map((x) => x.workflow), ["validate-config"]);
  r = await c("/api/settings", { method: "POST", body: { changes: [{ file: "config/newsbot.yaml", kind: "field", path: ["publish", "mode"], value: "review" }], versions: { "config/newsbot.yaml": sha } } });
  assert.equal(r.status, 409);                                                                                       // sha is stale now
  for (const bad of [{ path: ["limits", "feed_workers"], value: 99 }, { path: ["publish", "mode"], value: "yolo" }, { path: ["nope", "x"], value: 1 }, { path: ["site", "base_url"], value: "http://x" }]) {
    const x = await c("/api/settings", { method: "POST", body: { changes: [{ file: "config/newsbot.yaml", kind: "field", ...bad }] } });
    assert.equal(x.status, 400, JSON.stringify(bad));
  }
  assert.equal((await c("/api/settings", { method: "POST", body: { changes: [{ file: "../etc/passwd", kind: "field", path: ["a"], value: 1 }] } })).status, 400);
  assert.equal(w.commits.length, 1);                                                                                 // nothing else was written
});

test("settings: model chain, tasks, embedding and budgets", async () => {
  const { c, w } = await session();
  const post = (ch) => c("/api/settings", { method: "POST", body: { changes: [{ file: "config/llm.yaml", ...ch }] } });
  assert.equal((await post({ kind: "chain", op: "insert", tier: "strong", index: 1, provider: "workers_ai", model: "@cf/test/model-a" })).status, 200);
  assert.equal(get(w.files["config/llm.yaml"], ["llm", "tiers", "strong", 1, "model"]), "@cf/test/model-a");
  assert.equal((await post({ kind: "chain", op: "set", tier: "strong", index: 1, provider: "workers_ai", model: "@cf/test/model-b" })).status, 200);
  assert.equal(get(w.files["config/llm.yaml"], ["llm", "tiers", "strong", 1, "model"]), "@cf/test/model-b");
  assert.equal((await post({ kind: "chain", op: "remove", tier: "strong", index: 1 })).status, 200);
  assert.equal(get(w.files["config/llm.yaml"], ["llm", "tiers", "strong", 1, "provider"]), undefined);   // the added entry is gone, the original stays
  assert.equal(get(w.files["config/llm.yaml"], ["llm", "tiers", "strong", 0, "provider"]), "workers_ai");
  assert.equal((await post({ kind: "task", task: "write_bn", tier: "small" })).status, 200);
  assert.equal(get(w.files["config/llm.yaml"], ["llm", "tasks", "write_bn"]), "small");
  assert.equal((await post({ kind: "embedding", provider: "workers_ai", model: "@cf/baai/bge-m3" })).status, 200);
  assert.equal((await post({ kind: "budget", op: "set", map: "tokens_per_day", key: "cf_phone1", value: 250000 })).status, 200);
  assert.equal(get(w.files["config/llm.yaml"], ["llm", "budget", "tokens_per_day", "cf_phone1"]), 250000);
  assert.equal((await post({ kind: "budget", op: "remove", map: "tokens_per_day", key: "cf_phone1" })).status, 200);
  for (const bad of [{ kind: "chain", op: "set", tier: "huge", index: 0, provider: "workers_ai", model: "m" }, { kind: "chain", op: "set", tier: "strong", index: 0, provider: "evil", model: "m" }, { kind: "chain", op: "set", tier: "strong", index: 0, provider: "groq", model: "m" },
    { kind: "chain", op: "set", tier: "strong", index: 0, provider: "workers_ai", model: "bad model!" }, { kind: "task", task: "x", tier: "small" }, { kind: "budget", op: "set", map: "tokens_per_day", key: "k", value: -1 },
    { kind: "chain", op: "remove", tier: "strong", index: 0 }, { kind: "wat" }]) assert.equal((await post(bad)).status, 400, JSON.stringify(bad));
});

test("raw files: allow-list, structure guard, history and restore", async () => {
  const { c, w } = await session();
  w.files["prompts/write_en.md"] = "old prompt"; w.versions["prompts/write_en.md"] = "vp";
  assert.equal((await c("/api/file?path=../secrets")).status, 400);
  assert.equal((await c("/api/file?path=state/usage.json")).status, 400);
  const f = await j(await c("/api/file?path=prompts/write_en.md"));
  assert.equal(f.text, "old prompt"); assert.equal(f.history.length, 1);
  let r = await c("/api/file", { method: "POST", body: { path: "prompts/write_en.md", text: "new prompt", sha: f.sha } });
  assert.equal(r.status, 200); assert.equal(w.files["prompts/write_en.md"], "new prompt");
  assert.equal((await c("/api/file", { method: "POST", body: { path: "prompts/write_en.md", text: "x", sha: "stale" } })).status, 409);
  const shrunk = w.files["config/newsbot.yaml"].replace(/^ {2}max_new_per_day:.*\n/m, "");
  r = await c("/api/file", { method: "POST", body: { path: "config/newsbot.yaml", text: shrunk } });
  assert.equal(r.status, 422); assert.match((await j(r)).error, /removes 1 setting/);
  assert.equal((await c("/api/file", { method: "POST", body: { path: "config/newsbot.yaml", text: shrunk, allowStructure: true } })).status, 200);
  assert.equal((await c("/api/file", { method: "POST", body: { path: "config/newsbot.yaml", text: "a:\n\tb: 1\n" } })).status, 400);
  w.old = { [`config/newsbot.yaml@${"a".repeat(40)}`]: NEWS };
  r = await c("/api/restore", { method: "POST", body: { path: "config/newsbot.yaml", ref: "a".repeat(40) } });
  assert.equal(r.status, 200); assert.equal(w.files["config/newsbot.yaml"], NEWS);
});

test("keys: encrypted for GitHub's key, tested first, rejected keys not stored unless forced, names checked, value never logged", async () => {
  const { c, w, env } = await session();
  const list = await j(await c("/api/keys"));
  assert.ok(list.keys.some((k) => k.name === "BRAIN_REPO_TOKEN") && !list.keys.some((k) => /GROQ|GEMINI|OPENROUTER/.test(k.name))); assert.deepEqual(list.vars.map((v) => v.name), ["PAGES_PROJECT"]);
  const value = "ghp_my-new-key-123";
  let r = await c("/api/keys/set", { method: "POST", body: { name: "BRAIN_REPO_TOKEN", value } });
  assert.equal(r.status, 200); assert.equal((await j(r)).status, "created");
  assert.equal(w.secrets.BRAIN_REPO_TOKEN.key_id, "kid1");
  assert.equal(openSealed(w, w.secrets.BRAIN_REPO_TOKEN.encrypted_value), value);                                      // real round trip
  w.providerStatus.github = 401;
  r = await c("/api/keys/set", { method: "POST", body: { name: "WORKFLOW_PAT", value: "ghp_bad" } });
  assert.equal(r.status, 422); assert.ok(!("WORKFLOW_PAT" in w.secrets));
  r = await c("/api/keys/set", { method: "POST", body: { name: "WORKFLOW_PAT", value: "ghp_bad", force: true } });
  assert.equal(r.status, 200); assert.equal(openSealed(w, w.secrets.WORKFLOW_PAT.encrypted_value), "ghp_bad");
  r = await c("/api/keys/set", { method: "POST", body: { name: "TELEGRAM_BOT_TOKEN", value: "12345678:" + "A".repeat(35) } });
  assert.equal((await j(r)).after, "setup-approve");
  r = await c("/api/keys/test", { method: "POST", body: { name: "TELEGRAM_CHAT_ID", value: "abc" } });
  assert.equal((await j(r)).ok, false);
  for (const bad of [{ name: "lower", value: "x" }, { name: "GITHUB_TOKEN", value: "x" }, { name: "OK_NAME", value: "" }, { name: "OK_NAME", value: "a\nb" }, { name: "A", value: "x" }]) {
    assert.equal((await c("/api/keys/set", { method: "POST", body: bad })).status, 400, JSON.stringify(bad));
  }
  assert.equal((await c("/api/keys/set", { method: "POST", body: { name: "MY_NEW_PROVIDER_KEY", value: "zzz" } })).status, 200);   // custom names are allowed
  const audit = JSON.stringify(await j(await c("/api/audit")));
  assert.ok(audit.includes("BRAIN_REPO_TOKEN")); assert.ok(!audit.includes(value) && !audit.includes("ghp_bad"));
  assert.equal((await c("/api/vars/set", { method: "POST", body: { name: "PAGES_PROJECT", value: "other" } })).status, 200); assert.equal(w.vars.PAGES_PROJECT, "other");
  assert.equal((await c("/api/vars/set", { method: "POST", body: { name: "NEW_VAR", value: "1" } })).status, 200); assert.equal(w.vars.NEW_VAR, "1");
  assert.equal((await c("/api/vars/set", { method: "POST", body: { name: "bad", value: "1" } })).status, 400);
  void env;
});

test("drafts: list, open, approve with edits, gate-failed needs the tick, reject, save", async () => {
  const { c, w } = await session();
  const l = await j(await c("/api/drafts?lang=en"));
  assert.equal(l.total, 2); assert.ok(l.items.some((i) => i.slug === "dead-actor" && i.head.includes("Actor dies")));
  assert.equal((await j(await c("/api/drafts?lang=en&q=gate"))).matched, 1);
  const d = await j(await c("/api/draft?lang=en&slug=dead-actor"));
  assert.equal(d.title, "Actor dies"); assert.match(d.pending, /sensitive:death/); assert.match(d.html, /<h3>Body<\/h3>/);
  assert.equal((await c("/api/draft?lang=en&slug=..%2Fx")).status, 400);
  const edits = { title: "Actor dies at 93", summary: "He died.", body: "Fixed text." };
  let r = await j(await c("/api/draft", { method: "POST", body: { lang: "en", slug: "dead-actor", action: "approve", edits, version: d.version } }));
  assert.equal(r.key, "approved");
  assert.ok(!("pending/en/dead-actor.md" in w.files)); assert.match(w.files["content/en/dead-actor.md"], /^title: "Actor dies at 93"$/m);
  assert.ok(!w.files["content/en/dead-actor.md"].includes("pending:")); assert.equal(w.dispatches.at(-1).workflow, "deploy");
  r = await j(await c("/api/draft", { method: "POST", body: { lang: "en", slug: "bad-gate", action: "approve", edits } }));
  assert.equal(r.key, "gate"); assert.ok("pending/en/bad-gate.md" in w.files);
  r = await j(await c("/api/draft", { method: "POST", body: { lang: "en", slug: "bad-gate", action: "save", edits: { ...edits, title: "Fixed name" } } }));
  assert.equal(r.key, "saved"); assert.match(w.files["pending/en/bad-gate.md"], /^title: "Fixed name"$/m); assert.ok(w.files["pending/en/bad-gate.md"].includes("pending:"));
  r = await j(await c("/api/draft", { method: "POST", body: { lang: "en", slug: "bad-gate", action: "approve", edits, ack: true } }));
  assert.equal(r.key, "approved");
  r = await j(await c("/api/draft", { method: "POST", body: { lang: "bn", slug: "one", action: "reject" } }));
  assert.equal(r.key, "rejected"); assert.ok(!("pending/bn/one.md" in w.files));
  assert.equal((await c("/api/draft", { method: "POST", body: { lang: "en", slug: "x", action: "approve", edits: { title: "", summary: "s", body: "b" } } })).status, 400);
  assert.equal((await j(await c("/api/draft", { method: "POST", body: { lang: "en", slug: "gone", action: "reject" } }))).key, "gone");
});

test("live articles: edit and hide/show redeploy the site; PRs can be merged or closed", async () => {
  const { c, w } = await session();
  const a = await j(await c("/api/article?lang=en&slug=live-one"));
  assert.equal(a.title, "Live one"); assert.equal(a.hidden, false);
  let r = await j(await c("/api/article", { method: "POST", body: { lang: "en", slug: "live-one", action: "save", edits: { title: "Live one v2", summary: "S", body: "New body" }, version: a.version } }));
  assert.equal(r.key, "published"); assert.match(w.files["content/en/live-one.md"], /^title: "Live one v2"$/m); assert.equal(w.dispatches.at(-1).workflow, "deploy");
  r = await j(await c("/api/article", { method: "POST", body: { lang: "en", slug: "live-one", action: "hide" } }));
  assert.match(w.files["content/en/live-one.md"], /\ndraft: true\n---/);
  assert.equal((await j(await c("/api/article?lang=en&slug=live-one"))).hidden, true);
  await c("/api/article", { method: "POST", body: { lang: "en", slug: "live-one", action: "show" } });
  assert.ok(!w.files["content/en/live-one.md"].includes("draft:"));
  assert.equal((await j(await c("/api/article", { method: "POST", body: { lang: "en", slug: "live-one", action: "show" } }))).key, "nochange");
  assert.equal((await c("/api/article", { method: "POST", body: { lang: "en", slug: "live-one", action: "save", edits: { title: "T", summary: "S", body: "B" }, version: "stale" } })).status, 409);
  assert.equal((await j(await c("/api/prs"))).prs[0].number, 7);
  await c("/api/pr", { method: "POST", body: { number: 7, action: "merge" } }); assert.equal(w.merged, 7);
  await c("/api/pr", { method: "POST", body: { number: 7, action: "close" } }); assert.equal(w.closed, 7);
  assert.equal((await c("/api/pr", { method: "POST", body: { number: "7", action: "merge" } })).status, 400);
});

test("workflows: only the allow-listed ones can be started; decisions and errors never leak secrets", async () => {
  const { c, w, env } = await session();
  assert.equal((await c("/api/dispatch", { method: "POST", body: { workflow: "hourly", mode: "dry-run" } })).status, 200);
  assert.deepEqual(w.dispatches.at(-1), { workflow: "hourly", body: { ref: "main", inputs: { mode: "dry-run" } } });
  assert.equal((await c("/api/dispatch", { method: "POST", body: { workflow: "unzip" } })).status, 400);
  assert.equal((await c("/api/dispatch", { method: "POST", body: { workflow: "hourly", mode: "rm -rf" } })).status, 400);
  assert.equal((await j(await c("/api/decisions"))).items[0].action, "skip");
  const broken = { ...env, GH_TOKEN: "" };
  const r = await call(broken, "/api/overview", { cookie: await login(broken) });
  const text = await r.text();
  assert.equal(r.status, 500); assert.ok(!text.includes("ghp_") && !text.includes(PASS));
});
