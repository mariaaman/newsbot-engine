// Runs the real app.js against the real Worker (all fakes underneath) in a tiny fake DOM: every screen must render, and the main buttons must work.
import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import { FakeKV, assets, fakeWorld, addCf } from "./fakes.mjs";

class El {
  constructor(tag) { this.nodeType = 1; this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.className = ""; this.value = ""; this.checked = false; this.disabled = false; this.hidden = false; this.type = ""; this.open = false; this.selected = false; this.textContent = ""; this.innerHTML = ""; this.id = ""; this.title = ""; this.placeholder = ""; this.maxLength = 0; this.classList = { toggle: (c, on) => { const s = new Set(this.className.split(" ").filter(Boolean)); on ? s.add(c) : s.delete(c); this.className = [...s].join(" "); } }; }
  get childElementCount() { return this.children.filter((c) => c instanceof El).length; }
  append(...k) { this.children.push(...k); }
  replaceChildren(...k) { this.children = k; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); }
  remove() {}
  text() { return (this.textContent || "") + this.children.map((c) => (c instanceof El ? c.text() : String(c.data ?? ""))).join(" "); }
  find(pred, out = []) { if (pred(this)) out.push(this); for (const c of this.children) if (c instanceof El) c.find(pred, out); return out; }
}
const byId = {};
const body = new El("body");
globalThis.document = { createElement: (t) => new El(t), createTextNode: (data) => ({ nodeType: 3, data }), getElementById: (id) => (byId[id] ||= new El("div")), querySelectorAll: () => [], body };
const winListeners = {};
globalThis.window = { addEventListener: (ev, fn) => { winListeners[ev] = fn; }, scrollTo() {} };
globalThis.location = { hash: "#/", reload() {}, set href(v) {} };
globalThis.confirm = () => true;

const env = { KV: new FakeKV(), ASSETS: assets, SESSION_SECRET: "s".repeat(48), DASHBOARD_PASSWORD: "correct horse battery staple", GH_TOKEN: "ghp_x", BRAIN_REPO: "me/newsbot-brain", ENGINE_REPO: "me/newsbot-engine" };
let cookie = "";
const world = fakeWorld();
const gitHubFetch = globalThis.fetch;
globalThis.fetch = async (url, init = {}) => {
  if (typeof url === "string" && url.startsWith("/")) {
    return worker.fetch(new Request("https://dash.example" + url, { ...init, headers: { ...(init.headers || {}), Cookie: cookie, "CF-Connecting-IP": "1.1.1.1" } }), env);
  }
  return gitHubFetch(url, init);
};
const login = await worker.fetch(new Request("https://dash.example/api/login", { method: "POST", headers: { "X-Requested-With": "newsbot", "Content-Type": "application/json" }, body: JSON.stringify({ password: env.DASHBOARD_PASSWORD }) }), env);
cookie = login.headers.get("set-cookie").split(";")[0];

await import("../public/app.js");
const view = byId.view;
const tick = (ms = 80) => new Promise((r) => setTimeout(r, ms));
const go = async (hash) => { location.hash = hash; winListeners.hashchange(); await tick(); };
const click = async (pred, text) => { const b = view.find((e) => e.tag === "button" && pred(e) && e.text().includes(text))[0]; assert.ok(b, "button: " + text); for (const fn of b.listeners.click || []) await fn({ target: b }); await tick(); };

test("every screen renders without an error card", async () => {
  for (const hash of ["#/", "#/drafts", "#/articles", "#/settings", "#/keys", "#/llm", "#/runs", "#/more", "#/draft/en/dead-actor", "#/draft/en/bad-gate", "#/article/en/live-one"]) {
    await go(hash);
    const text = view.text();
    assert.ok(!view.find((e) => e.className.includes("card bad") && e.text().includes("ত্রুটি")).length, `${hash}: ${text.slice(0, 200)}`);
    assert.ok(text.length > 30, `${hash} is empty`);
  }
});

test("home shows the mode, counts and the run button works", async () => {
  await go("#/");
  const t = view.text();
  assert.match(t, /review/); assert.match(t, /প্রকাশিত 1/); assert.match(t, /খসড়া 3/); assert.match(t, /অনুপস্থিত সিক্রেট/);
  await click(() => true, "এখনই চালান");
  assert.equal(world.dispatches.at(-1).workflow, "hourly");
});

test("draft screen: preview is shown, the gate checkbox only appears for a failed draft, approve publishes", async () => {
  await go("#/draft/en/bad-gate");
  assert.ok(view.find((e) => e.tag === "label" && e.text().includes("আমি নিজে")).length === 1);
  await go("#/draft/en/dead-actor");
  assert.ok(view.find((e) => e.tag === "label" && e.text().includes("আমি নিজে")).length === 0);
  assert.ok(view.find((e) => e.className === "preview")[0].innerHTML.includes("<h3>Body</h3>"));
  await click(() => true, "অনুমোদন ও প্রকাশ");
  assert.ok("content/en/dead-actor.md" in world.files); assert.ok(!("pending/en/dead-actor.md" in world.files));
});

test("settings screen: editing a number marks it changed and the save bar writes it", async () => {
  await go("#/settings");
  const field = view.find((e) => e.className.includes("field") && e.text().includes("দিনে সর্বোচ্চ নতুন আর্টিকেল"))[0];
  const inp = field.find((e) => e.tag === "input" && e.type === "number")[0];
  assert.ok(inp, "max_new_per_day input");
  inp.value = "11";
  for (const fn of inp.listeners.input || []) fn();
  const bar = view.find((e) => e.className.split(" ").includes("bar"))[0];
  assert.equal(bar.hidden, false);
  const save = bar.find((e) => e.tag === "button" && e.text().includes("সংরক্ষণ"))[0];
  for (const fn of save.listeners.click) await fn({ target: save });
  await tick();
  assert.match(world.files["config/newsbot.yaml"], /max_new_per_day: 11/);
});

test("keys screen: a key can be tested and saved from the page", async () => {
  await go("#/keys");
  const cards = view.find((e) => e.className === "card" && e.text().includes("BRAIN_REPO_TOKEN"));
  const card = cards[cards.length - 1];
  const val = card.find((e) => e.tag === "input" && e.type === "password")[0];
  val.value = "ghp_from-the-ui";
  const save = card.find((e) => e.tag === "button" && e.text() === "সংরক্ষণ")[0];
  for (const fn of save.listeners.click) await fn({ target: save });
  await tick();
  assert.ok(world.secrets.BRAIN_REPO_TOKEN);
  assert.equal(val.value, "");                                                      // the typed value is cleared after saving
});

test("llm screen: add an account with the form (checked first), see it with its usage, remove it", async () => {
  const a = addCf(world, 1);
  await go("#/llm");
  assert.match(view.text(), /LLM অ্যাকাউন্ট পুল/); assert.match(view.text(), /এখনো কোনো অ্যাকাউন্ট যোগ করা হয়নি/);
  const inputs = view.find((e) => e.tag === "input");
  const [label, id, token] = inputs;
  label.value = "phone1"; id.value = a.id; token.value = a.token;
  const test = view.find((e) => e.tag === "button" && e.text().includes("শুধু পরীক্ষা"))[0];
  for (const fn of test.listeners.click) await fn({ target: test });
  await tick(150);
  assert.match(view.text(), /সব পরীক্ষা পাস/); assert.ok(!world.secrets.CFLLM_PHONE1);          // a test alone saves nothing
  const add = view.find((e) => e.tag === "button" && e.text().includes("পরীক্ষা করে যোগ করুন"))[0];
  for (const fn of add.listeners.click) await fn({ target: add });
  await tick(300);
  assert.ok(world.secrets.CFLLM_PHONE1);
  assert.equal(token.value, "");                                                                  // the typed token is cleared
  assert.match(view.text(), /PHONE1/);
  assert.match(view.text(), /আজকের অবস্থা/);
  const rm = view.find((e) => e.tag === "button" && e.text() === "মুছুন")[0];
  for (const fn of rm.listeners.click) await fn({ target: rm });
  await tick(300);
  assert.ok(!world.secrets.CFLLM_PHONE1);
});
