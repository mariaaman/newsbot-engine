import test from "node:test";
import assert from "node:assert/strict";
import worker, { draftRef, publishable, safeEqual, sign, applyEdits, readField, preview } from "./index.js";

const DRAFT = (extra = "", fail = "failures: []") => `---\ntitle: Tom Skerritt, actor, dies at 93\nsummary: He died on Wednesday.\nslug: tom-skerritt-dies-93\nlang: en\nai_assisted: true\npending:\n  ${fail}\n  review:\n  - sensitive:death\n  unsupported: []${extra}\n---\n## What happened\nTom died [S1].\n`;
const OK_PENDING = DRAFT();
const GATE_PENDING = DRAFT("", "failures:\n  - unsupported_name");
const ENV = { GH_TOKEN: "ghp_x", TELEGRAM_BOT_TOKEN: "123:abc", TELEGRAM_CHAT_ID: "42", WEBHOOK_SECRET: "s3cret",
  BRAIN_REPO: "me/newsbot-brain", ENGINE_REPO: "me/newsbot-engine" };
const REF = "7c775f641c", PATH = "pending/en/tom-skerritt-dies-93.md";

/** Tiny in-memory fake of the GitHub + Telegram endpoints the Worker uses. */
function fakeNet({ files, refFails = 0, dispatchStatus = 204 }) {
  const log = { tg: [], commits: [], dispatch: 0, calls: [] };
  const blobs = new Map();
  let head = "c0", fails = refFails, ver = 1;
  const versions = Object.fromEntries(Object.keys(files).map((k) => [k, "v" + ver++]));
  globalThis.fetch = async (url, init = {}) => {
    const u = new URL(url), body = init.body ? JSON.parse(init.body) : null, p = u.pathname;
    log.calls.push(`${init.method || "GET"} ${p}`);
    const j = (status, o) => new Response(status === 204 ? null : o === undefined ? "" : JSON.stringify(o), { status });
    if (u.host === "api.telegram.org") { log.tg.push({ method: p.split("/").pop(), body }); return j(200, { ok: true }); }
    let m;
    if ((m = p.match(/^\/repos\/me\/newsbot-brain\/contents\/pending\/(\w+)$/))) {
      const names = Object.keys(files).filter((f) => f.startsWith(`pending/${m[1]}/`));
      return names.length ? j(200, names.map((n) => ({ type: "file", name: n.split("/").pop(), path: n }))) : j(404, {});
    }
    if ((m = p.match(/^\/repos\/me\/newsbot-brain\/contents\/(.+)$/))) {
      return files[m[1]] ? j(200, { content: Buffer.from(files[m[1]]).toString("base64"), sha: versions[m[1]] }) : j(404, {});
    }
    if (p.endsWith("/git/ref/heads/main")) return j(200, { object: { sha: head } });
    if (p.match(/\/git\/commits\/[^/]+$/) && !init.method) return j(200, { tree: { sha: "t0" } });
    if (p.endsWith("/git/blobs")) { const sha = "b" + blobs.size; blobs.set(sha, body.content); return j(201, { sha }); }
    if (p.endsWith("/git/trees")) { log.commits.push({ tree: body.tree, blobs: Object.fromEntries(blobs) }); return j(201, { sha: "t1" }); }
    if (p.endsWith("/git/commits")) return j(201, { sha: "c" + (log.commits.length + 1) });
    if (p.endsWith("/git/refs/heads/main")) {
      if (fails-- > 0) return j(422, {});
      head = body.sha;
      const last = log.commits[log.commits.length - 1];                                  // apply the landed commit to the fake repo
      for (const e of last.tree) {
        if (e.sha === null) delete files[e.path]; else { files[e.path] = last.blobs[e.sha]; versions[e.path] = "v" + ver++; }
      }
      return j(200, {});
    }
    if (p.endsWith("/actions/workflows/deploy.yml/dispatches")) { log.dispatch++; return j(dispatchStatus); }
    return j(500, { unexpected: p });
  };
  log.files = files;
  return log;
}

const tgReq = (cb, { secret = "s3cret", from = 42, chat = 42, path = "/telegram" } = {}) =>
  new Request("https://w.dev" + path, { method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": secret },
    body: JSON.stringify({ callback_query: { id: "q1", from: { id: from }, message: { message_id: 7, chat: { id: chat } }, data: cb } }) });

const sigOf = (lang = "en", ref = REF, env = ENV) => sign(env, lang, ref);
const get = async (env = ENV, sig) => worker.fetch(new Request(`https://w.dev/d/en/${REF}?s=${sig ?? (await sigOf())}`), env);
const post = async (fields, env = ENV, sig) => worker.fetch(new Request(`https://w.dev/d/en/${REF}?s=${sig ?? (await sigOf())}`, { method: "POST", body: new URLSearchParams(fields) }), env);
const FORM = { title: "Tom Skerritt, actor, dies at 93", summary: "He died on Wednesday.", body: "## What happened\nTom died [S1].", v: "v1" };

// ---------- helpers ----------

test("hash and signature match the Python side", async () => {
  assert.equal(await draftRef("tom-skerritt-dies-93"), REF);
  assert.equal(await draftRef("প্রধানমন্ত্রী-রাজশাহী"), "a9802abe76");
  assert.equal(await sigOf(), "134bb1f077f361ca9cbfae8761be5bd7ed2d6f83a1843758872d8537a88e6c7a");
});

test("publishable strips only the pending block; gate failures need an explicit allow", () => {
  assert.equal(publishable(OK_PENDING).md, OK_PENDING.replace(/pending:[\s\S]*?unsupported: \[\]\n/, ""));
  assert.equal(publishable(GATE_PENDING).refuse, "gate");
  assert.ok(publishable(GATE_PENDING, true).md && !publishable(GATE_PENDING, true).md.includes("pending:"));
  assert.ok(publishable("no front matter").refuse);
  assert.ok(publishable("---\ntitle: x\n---\nbody").refuse);
});

test("applyEdits rewrites only changed title/summary/body and keeps every other line", () => {
  const out = applyEdits(OK_PENDING, { title: "New: title", summary: "He died on Wednesday.", body: "New body\r\nline" });
  assert.match(out, /^title: "New: title"$/m);
  assert.match(out, /^summary: He died on Wednesday\.$/m);                           // unchanged -> untouched
  assert.ok(out.includes("pending:\n  failures: []\n  review:\n  - sensitive:death"));
  assert.ok(out.endsWith("---\nNew body\nline\n"));
  assert.equal(readField(`title: 'It''s ok'\n`, "title"), "It's ok");
  assert.equal(readField(`title: "A \\"q\\" é"\n`, "title"), 'A "q" é');
  assert.equal(readField(`title: >-\n  folded\n`, "title"), null);
});

test("preview escapes HTML and only links http(s)", () => {
  const h = preview("## Hi <script>alert(1)</script>\n[x](javascript:alert(1)) [ok](https://a.com/p?x=1&y=2)\n\n- one\n- **two**");
  assert.ok(!h.includes("<script>") && !h.includes('href="javascript'));
  assert.match(h, /<h3>Hi &lt;script&gt;/);
  assert.match(h, /<a href="https:\/\/a\.com\/p\?x=1&amp;y=2"/);
  assert.match(h, /<ul><li>one<\/li><li><strong>two<\/strong><\/li><\/ul>/);
});

test("safeEqual", () => { assert.ok(safeEqual("a", "a")); assert.ok(!safeEqual("a", "ab")); assert.ok(!safeEqual("a", "b")); });

// ---------- Telegram buttons ----------

test("wrong secret -> 401, nothing else happens", async () => {
  const log = fakeNet({ files: {} });
  const r = await worker.fetch(tgReq(`a|en|${REF}`, { secret: "nope" }), ENV);
  assert.equal(r.status, 401); assert.equal(log.calls.length, 0);
});

test("a stranger (other user or other chat) is ignored without any call", async () => {
  const log = fakeNet({ files: { [PATH]: OK_PENDING } });
  for (const o of [{ from: 99 }, { chat: 99 }]) assert.equal((await worker.fetch(tgReq(`a|en|${REF}`, o), ENV)).status, 200);
  assert.equal(log.calls.length, 0);
});

test("junk callback data, other paths and non-POST do nothing harmful", async () => {
  const log = fakeNet({ files: {} });
  assert.equal((await worker.fetch(new Request("https://w.dev/telegram"), ENV)).status, 200);
  assert.equal((await worker.fetch(tgReq(`a|en|${REF}`, { path: "/x" }), ENV)).status, 200);
  for (const bad of ["a|en|../../x", "a|EN|" + REF, "x|en|" + REF, `a|en|${REF}|1`, ""]) await worker.fetch(tgReq(bad), ENV);
  assert.ok(!log.calls.some((c) => c.includes("/repos/")));
});

test("button approve: one commit moves the draft to content/ without the pending block, then deploy starts", async () => {
  const log = fakeNet({ files: { [PATH]: OK_PENDING, "pending/en/other.md": OK_PENDING } });
  assert.equal((await worker.fetch(tgReq(`a|en|${REF}`), ENV)).status, 200);
  assert.equal(log.commits.length, 1);
  assert.ok(log.files["content/en/tom-skerritt-dies-93.md"] && !log.files["content/en/tom-skerritt-dies-93.md"].includes("pending:"));
  assert.ok(!(PATH in log.files) && "pending/en/other.md" in log.files);
  assert.equal(log.dispatch, 1);
  assert.match(log.tg.find((t) => t.method === "answerCallbackQuery").body.text, /অনুমোদিত/);
  assert.ok(log.tg.some((t) => t.method === "editMessageReplyMarkup"));
});

test("button approve of a gate-failed draft is refused", async () => {
  const log = fakeNet({ files: { [PATH]: GATE_PENDING } });
  await worker.fetch(tgReq(`a|en|${REF}`), ENV);
  assert.equal(log.commits.length, 0); assert.equal(log.dispatch, 0); assert.match(log.tg[0].body.text, /মান-যাচাই/);
});

test("button reject deletes only the draft", async () => {
  const log = fakeNet({ files: { [PATH]: GATE_PENDING } });
  await worker.fetch(tgReq(`r|en|${REF}`), ENV);
  assert.deepEqual(Object.keys(log.files), []); assert.equal(log.dispatch, 0);
});

test("second tap is harmless; branch moved meanwhile retries; failing dispatch is reported", async () => {
  let log = fakeNet({ files: { "pending/en/zzz.md": OK_PENDING } });
  await worker.fetch(tgReq(`a|en|${REF}`), ENV);
  assert.equal(log.commits.length, 0); assert.match(log.tg[0].body.text, /নিষ্পন্ন/);
  log = fakeNet({ files: { [PATH]: OK_PENDING }, refFails: 1 });
  await worker.fetch(tgReq(`a|en|${REF}`), ENV);
  assert.equal(log.commits.length, 2); assert.equal(log.dispatch, 1);
  log = fakeNet({ files: { [PATH]: OK_PENDING }, dispatchStatus: 403 });
  await worker.fetch(tgReq(`a|en|${REF}`), ENV);
  assert.match(log.tg[0].body.text, /ডিপ্লয় চালু করা যায়নি/);
});

// ---------- the draft page ----------

test("page: bad or missing signature is 403 and touches nothing", async () => {
  const log = fakeNet({ files: { [PATH]: OK_PENDING } });
  assert.equal((await get(ENV, "0".repeat(64))).status, 403);
  assert.equal((await post(FORM, ENV, "bad")).status, 403);
  assert.equal(log.calls.length, 0);
});

test("page GET shows the draft, escapes content, sends hardening headers", async () => {
  fakeNet({ files: { [PATH]: DRAFT().replace("Tom died [S1].", "Tom <b>died</b> [S1].") } });
  const r = await get();
  const h = await r.text();
  assert.equal(r.status, 200);
  assert.match(h, /Tom Skerritt, actor, dies at 93/);
  assert.ok(!h.includes("<b>died</b>") && h.includes("&lt;b&gt;died&lt;/b&gt;"));
  assert.match(r.headers.get("content-security-policy"), /default-src 'none'/);
  assert.equal(r.headers.get("cache-control"), "no-store");
  assert.equal(r.headers.get("referrer-policy"), "no-referrer");
  assert.ok(!h.includes('name="ack"'));                                                 // no gate checkbox on a clean draft
});

test("page save: edits title/summary/body in pending/, keeps the pending block, publishes nothing", async () => {
  const log = fakeNet({ files: { [PATH]: OK_PENDING } });
  const r = await post({ ...FORM, title: "Fixed title", body: "Better text", action: "save" });
  assert.equal(r.status, 200);
  const f = log.files[PATH];
  assert.match(f, /^title: "Fixed title"$/m); assert.ok(f.includes("pending:") && f.endsWith("Better text\n"));
  assert.ok(!Object.keys(log.files).some((k) => k.startsWith("content/"))); assert.equal(log.dispatch, 0);
  assert.match(await r.text(), /সংরক্ষিত হয়েছে/);
});

test("page approve: saves the form's text and publishes it in one commit, deploys, tells Telegram", async () => {
  const log = fakeNet({ files: { [PATH]: OK_PENDING } });
  const r = await post({ ...FORM, body: "Corrected text", action: "approve" });
  assert.equal(r.status, 200); assert.equal(log.commits.length, 1);
  const pub = log.files["content/en/tom-skerritt-dies-93.md"];
  assert.ok(pub.endsWith("Corrected text\n") && !pub.includes("pending:")); assert.ok(!(PATH in log.files));
  assert.equal(log.dispatch, 1);
  assert.match(log.tg.find((t) => t.method === "sendMessage").body.text, /অনুমোদিত: Tom Skerritt/);
});

test("page approve of a gate-failed draft needs the checkbox", async () => {
  let log = fakeNet({ files: { [PATH]: GATE_PENDING } });
  let h = await (await post({ ...FORM, action: "approve" })).text();
  assert.match(h, /অনুমোদন করা যায় না/); assert.equal(log.dispatch, 0); assert.ok(PATH in log.files);
  log = fakeNet({ files: { [PATH]: GATE_PENDING } });
  await post({ ...FORM, action: "approve", ack: "1" });
  assert.equal(log.dispatch, 1); assert.ok(!(PATH in log.files));
});

test("page: stale version, empty fields and wrong PIN save nothing", async () => {
  let log = fakeNet({ files: { [PATH]: OK_PENDING } });
  assert.match(await (await post({ ...FORM, v: "old", action: "save" })).text(), /বদলে গেছে/);
  assert.match(await (await post({ ...FORM, title: " ", action: "approve" })).text(), /ফাঁকা রাখা যাবে না/);
  assert.equal(log.commits.length, 0);
  const env = { ...ENV, EDIT_PIN: "4321" };
  assert.match(await (await post({ ...FORM, action: "approve", pin: "0000" }, env)).text(), /PIN ভুল/);
  assert.equal(log.commits.length, 0);
  await post({ ...FORM, action: "approve", pin: "4321" }, env);
  assert.equal(log.commits.length, 1);
});

test("page reject deletes the draft; a link for a handled draft says so", async () => {
  const log = fakeNet({ files: { [PATH]: OK_PENDING } });
  await post({ action: "reject" });
  assert.deepEqual(Object.keys(log.files), []);
  assert.match(await (await get()).text(), /নিষ্পন্ন/);
});
