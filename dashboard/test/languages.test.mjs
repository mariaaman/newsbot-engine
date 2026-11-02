// Site languages in the dashboard: list from the catalog, add / remove one language, nothing else may break.
import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import { get } from "../src/yamledit.js";
import { CATALOG, CODES } from "../src/langs.js";
import { FakeKV, assets, fakeWorld } from "./fakes.mjs";

const PASS = "correct horse battery staple";
const H = { "X-Requested-With": "newsbot", "Content-Type": "application/json" };
async function session() {
  const env = { KV: new FakeKV(), ASSETS: assets, SESSION_SECRET: "s".repeat(48), DASHBOARD_PASSWORD: PASS, GH_TOKEN: "ghp_secret_token_value", BRAIN_REPO: "me/newsbot-brain", ENGINE_REPO: "me/newsbot-engine" };
  const w = fakeWorld();
  const lr = await worker.fetch(new Request("https://dash.example/api/login", { method: "POST", headers: { ...H, "CF-Connecting-IP": "1.2.3.4" }, body: JSON.stringify({ password: PASS }) }), env);
  const cookie = lr.headers.get("set-cookie").split(";")[0];
  const c = (path, { method = "GET", body } = {}) => worker.fetch(new Request("https://dash.example" + path, { method, headers: { ...(method === "GET" ? {} : H), Cookie: cookie, "CF-Connecting-IP": "1.2.3.4" }, body: body === undefined ? undefined : JSON.stringify(body) }), env);
  return { w, c };
}
const NEWS = "config/newsbot.yaml";
const lang = (c, op, code) => c("/api/settings", { method: "POST", body: { changes: [{ file: NEWS, kind: "languages", op, code }] } });
const langs = (w) => get(w.files[NEWS], ["site", "languages"]);

test("the catalog the dashboard offers is the engine's: codes, names, RTL and the label status", () => {
  assert.ok(CODES.length >= 20 && CODES.includes("bn") && CODES.includes("hi") && CODES.includes("ja"));
  assert.equal(CATALOG.find((l) => l.code === "ar").rtl, true);
  assert.equal(CATALOG.find((l) => l.code === "ja").spaceless, true);
  assert.equal(CATALOG.find((l) => l.code === "hi").ui, true);
  assert.equal(CATALOG.find((l) => l.code === "sw").ui, false);                  // labels not translated yet: shown in English
});

test("settings lists the enabled languages, the default and the catalog", async () => {
  const { c } = await session();
  const d = await (await c("/api/settings")).json();
  assert.deepEqual(d.languages.enabled, ["bn", "en"]); assert.equal(d.languages.default, "bn");
  assert.ok(d.languages.catalog.length >= 20);
  assert.deepEqual(d.fields.find((f) => f.path.join(".") === "site.default").options, CODES);
});

test("add: any catalog language is appended to site.languages, everything else in the file stays", async () => {
  const { c, w } = await session();
  const before = w.files[NEWS];
  for (const code of ["hi", "ar", "ja", "de"]) assert.equal((await lang(c, "add", code)).status, 200, code);
  assert.deepEqual(langs(w), ["bn", "en", "hi", "ar", "ja", "de"]);
  const restore = w.files[NEWS].replace("languages: [bn, en, hi, ar, ja, de]", "languages: [bn, en]");
  assert.equal(restore, before);                                                  // nothing but that one line changed
  assert.ok(w.dispatches.some((x) => x.workflow === "validate-config"));         // the engine's validation runs on the new config
  const d = await (await c("/api/settings")).json();
  assert.deepEqual(d.languages.enabled, ["bn", "en", "hi", "ar", "ja", "de"]);
});

test("add: unknown, repeated or malformed languages are refused and nothing is written", async () => {
  const { c, w } = await session();
  const before = w.files[NEWS];
  for (const code of ["xx", "BN", "", "../x", 5, null]) assert.equal((await lang(c, "add", code)).status, 400, String(code));
  assert.equal((await lang(c, "add", "bn")).status, 400);                        // already there
  assert.equal((await lang(c, "bogus", "hi")).status, 400);
  assert.equal(w.files[NEWS], before);
});

test("remove: a language can be removed, but not the default one and not the last one", async () => {
  const { c, w } = await session();
  await lang(c, "add", "hi");
  assert.equal((await lang(c, "remove", "hi")).status, 200); assert.deepEqual(langs(w), ["bn", "en"]);
  assert.equal((await lang(c, "remove", "hi")).status, 400);                     // not on the site
  const r = await lang(c, "remove", "bn");
  assert.equal(r.status, 400); assert.match((await r.json()).error, /default/);
  assert.deepEqual(langs(w), ["bn", "en"]);
});

test("the default language must be one of the site's languages", async () => {
  const { c, w } = await session();
  const set = (value) => c("/api/settings", { method: "POST", body: { changes: [{ file: NEWS, kind: "field", path: ["site", "default"], value }] } });
  assert.equal((await set("hi")).status, 400);                                    // hi is not enabled yet
  await lang(c, "add", "hi");
  assert.equal((await set("hi")).status, 200); assert.equal(get(w.files[NEWS], ["site", "default"]), "hi");
  assert.equal((await lang(c, "remove", "hi")).status, 400);                      // now it is the default: cannot go
  assert.equal((await set("bn")).status, 200); assert.equal((await lang(c, "remove", "hi")).status, 200);
});

test("a language change on llm.yaml or with a stale file version is refused", async () => {
  const { c } = await session();
  assert.equal((await c("/api/settings", { method: "POST", body: { changes: [{ file: "config/llm.yaml", kind: "languages", op: "add", code: "hi" }] } })).status, 400);
  const r = await c("/api/settings", { method: "POST", body: { changes: [{ file: NEWS, kind: "languages", op: "add", code: "hi" }], versions: { [NEWS]: "stale-sha" } } });
  assert.equal(r.status, 409);
});
