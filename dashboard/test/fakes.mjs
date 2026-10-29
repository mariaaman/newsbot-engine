// In-memory stand-ins for KV, static assets, GitHub (REST + GraphQL + git data), Telegram and the key-test endpoints.
import fs from "node:fs";
import nacl from "../src/vendor/nacl-fast.cjs";
import { blake2b } from "../src/blake2b.js";

export class FakeKV {
  constructor() { this.m = new Map(); }
  async get(k) { return this.m.has(k) ? this.m.get(k) : null; }
  async put(k, v) { this.m.set(k, String(v)); }
  async delete(k) { this.m.delete(k); }
}

const PUBLIC = new URL("../public/", import.meta.url);
export const assets = { async fetch(req) {
  const p = new URL(req.url).pathname.slice(1);
  try { return new Response(fs.readFileSync(new URL(p, PUBLIC)), { status: 200 }); } catch { return new Response("nope", { status: 404 }); }
} };

const DIR = new URL("../../brain-template/config/", import.meta.url);
export const NEWS = fs.readFileSync(new URL("newsbot.yaml", DIR), "utf8").replace(/base_url:.*/, "base_url: https://news-site.pages.dev");
export const LLM = fs.readFileSync(new URL("llm.yaml", DIR), "utf8");

export const draft = (title, { fail = false, extra = "" } = {}) => `---\ntitle: ${title}\nsummary: A summary.\nslug: s\nlang: en\ndate: 2026-10-25T10:00:00+00:00\nai_assisted: true\npending:\n  failures:${fail ? "\n  - unsupported_name" : " []"}\n  review:\n  - sensitive:death${extra}\n---\n## Body\nSome text [S1].\n`;
export const live = (title) => `---\ntitle: ${title}\nsummary: Live summary.\nlang: en\ndate: 2026-10-24T10:00:00+00:00\nlastmod: 2026-10-24T10:00:00+00:00\n---\nLive body.\n`;

export function fakeWorld(opts = {}) {
  const kp = nacl.box.keyPair();
  const w = {
    files: { "config/newsbot.yaml": NEWS, "config/llm.yaml": LLM, "state/usage.json": JSON.stringify({ day: "2026-10-25", counters: { "groq/openai/gpt-oss-120b": { requests: 7, tokens: 41000 } } }),
      "state/cooldowns.json": "{}", "state/deployed.json": JSON.stringify({ deployed_at: "2026-10-25T09:00:00+00:00" }), "state/decisions.jsonl": '{"action":"skip","story_id":"st-1","reason":"low score","ts":"2026-10-25T09:00:00+00:00"}\n',
      "pending/en/dead-actor.md": draft("Actor dies"), "pending/en/bad-gate.md": draft("Gate failed", { fail: true }), "pending/bn/one.md": draft("এক"), "content/en/live-one.md": live("Live one"), ...(opts.files || {}) },
    versions: {}, calls: [], commits: [], dispatches: [], secrets: {}, vars: { PAGES_PROJECT: "newsite" }, keyPair: kp, tg: [], providerStatus: { groq: 200, gemini: 200, openrouter: 200, telegram: 200, github: 200 }, ...opts.world,
  };
  let v = 1;
  for (const k of Object.keys(w.files)) w.versions[k] = "v" + v++;
  const touch = (k) => { w.versions[k] = "v" + v++; };
  let head = "c0", n = 0;
  const blobs = new Map();
  const J = (status, o) => new Response(status === 204 ? null : JSON.stringify(o ?? {}), { status, headers: { "Content-Type": "application/json" } });

  globalThis.fetch = async (url, init = {}) => {
    const u = new URL(url), p = u.pathname, body = init.body && typeof init.body === "string" ? JSON.parse(init.body) : null, m = init.method || "GET";
    w.calls.push(`${m} ${u.host}${p}`);
    if (u.host === "news-site.pages.dev") return new Response("<html></html>", { status: 200 });
    if (u.host === "raw.example") return new Response(w.files[decodeURIComponent(p.slice(1))] ?? "", { status: 200 });
    if (u.host === "api.groq.com") return J(w.providerStatus.groq, {});
    if (u.host === "generativelanguage.googleapis.com") return J(w.providerStatus.gemini, {});
    if (u.host === "openrouter.ai") return J(w.providerStatus.openrouter, {});
    if (u.host === "api.telegram.org") return J(w.providerStatus.telegram, { ok: w.providerStatus.telegram === 200 });
    if (u.host === "api.cloudflare.com") return J(403, { success: false });
    if (u.host !== "api.github.com") throw new Error("unexpected host " + u.host);
    if ((p === "/repos/me/newsbot-brain" || (p.startsWith("/repos/me/newsbot-engine") && !p.includes("/actions/"))) && m === "GET") return J(w.providerStatus.github, {});
    let r;
    if (p === "/graphql") {
      const data = {};
      for (const x of body.query.matchAll(/(\w+): object\(expression: ("(?:[^"\\]|\\.)*")\) \{ \.\.\. on (Blob|Tree)/g)) {
        const path = JSON.parse(x[2]).replace(/^main:/, "");
        if (x[3] === "Blob") data[x[1]] = path in w.files ? { text: w.files[path] } : null;
        else { const names = Object.keys(w.files).filter((f) => f.startsWith(path + "/") && !f.slice(path.length + 1).includes("/")).map((f) => ({ name: f.split("/").pop() })); data[x[1]] = names.length ? { entries: names } : null; }
      }
      return J(200, { data: { repository: data } });
    }
    if ((r = p.match(/^\/repos\/me\/newsbot-brain\/contents\/(.+)$/))) {
      const path = decodeURIComponent(r[1]);
      if (m === "GET") {
        const dir = Object.keys(w.files).filter((f) => f.startsWith(path + "/"));
        if (!(path in w.files) && dir.length) return J(200, [...new Set(dir.filter((f) => !f.slice(path.length + 1).includes("/")))].map((f) => ({ type: "file", name: f.split("/").pop(), path: f, size: w.files[f].length })));
        if (!(path in w.files)) return J(404);
        const ref = u.searchParams.get("ref");
        const text = w.old && w.old[`${path}@${ref}`] !== undefined ? w.old[`${path}@${ref}`] : w.files[path];
        return J(200, { type: "file", path, sha: w.versions[path], size: text.length, content: Buffer.from(text).toString("base64"), download_url: "https://raw.example/" + encodeURIComponent(path) });
      }
      if (m === "PUT") {
        if (path in w.files && body.sha !== w.versions[path]) return J(409);
        w.files[path] = Buffer.from(body.content, "base64").toString("utf8"); touch(path); w.commits.push({ message: body.message, paths: [path] });
        return J(200, { content: { sha: w.versions[path] } });
      }
    }
    if (p.endsWith("/git/ref/heads/main")) return J(200, { object: { sha: head } });
    if (p.match(/\/git\/commits\/[^/]+$/) && m === "GET") return J(200, { tree: { sha: "t0" } });
    if (p.endsWith("/git/blobs")) { const sha = "b" + blobs.size; blobs.set(sha, body.content); return J(201, { sha }); }
    if (p.endsWith("/git/trees")) { w.lastTree = body.tree.map((e) => ({ ...e, text: blobs.get(e.sha) })); return J(201, { sha: "t1" }); }
    if (p.endsWith("/git/commits") && m === "POST") { w.pendingMsg = body.message; return J(201, { sha: "c" + (++n) }); }
    if (p.endsWith("/git/refs/heads/main")) {
      head = body.sha;
      for (const e of w.lastTree) { if (e.sha === null) { delete w.files[e.path]; delete w.versions[e.path]; } else { w.files[e.path] = e.text; touch(e.path); } }
      w.commits.push({ message: w.pendingMsg, paths: w.lastTree.map((e) => e.path) });
      return J(200, {});
    }
    if (p.endsWith("/dispatches")) { w.dispatches.push({ workflow: p.split("/").slice(-2)[0].replace(/\.yml$/, ""), body }); return J(204); }
    if (p.endsWith("/actions/runs")) return J(200, { workflow_runs: [{ id: 1, name: "hourly", event: "schedule", status: "completed", conclusion: "success", created_at: "2026-10-25T09:00:00Z", html_url: "https://github.com/x/1", display_title: "hourly" }, { id: 2, name: "deploy", event: "workflow_dispatch", status: "completed", conclusion: "failure", created_at: "2026-10-25T08:00:00Z", html_url: "https://github.com/x/2" }] });
    if (p.endsWith("/actions/secrets/public-key")) return J(200, { key_id: "kid1", key: Buffer.from(w.keyPair.publicKey).toString("base64") });
    if (p.endsWith("/actions/secrets") && m === "GET") return J(200, { secrets: Object.keys(w.secrets).map((name) => ({ name, updated_at: "2026-10-20T00:00:00Z" })).concat([{ name: "GROQ_API_KEY", updated_at: "2026-10-20T00:00:00Z" }]) });
    if ((r = p.match(/\/actions\/secrets\/([A-Z0-9_]+)$/)) && m === "PUT") { const existed = r[1] in w.secrets; w.secrets[r[1]] = body; return J(existed ? 204 : 201); }
    if (p.endsWith("/actions/variables") && m === "GET") return J(200, { variables: Object.entries(w.vars).map(([name, value]) => ({ name, value, updated_at: "2026-10-20T00:00:00Z" })) });
    if ((r = p.match(/\/actions\/variables\/([A-Z0-9_]+)$/)) && m === "PATCH") { if (!(r[1] in w.vars)) return J(404); w.vars[r[1]] = body.value; return J(204); }
    if (p.endsWith("/actions/variables") && m === "POST") { w.vars[body.name] = body.value; return J(201); }
    if (p.endsWith("/pulls") && m === "GET") return J(200, [{ number: 7, title: "New article", html_url: "https://github.com/x/pull/7", created_at: "2026-10-25T07:00:00Z", user: { login: "bot" } }]);
    if ((r = p.match(/\/pulls\/(\d+)\/merge$/))) { w.merged = Number(r[1]); return J(200, { merged: true }); }
    if ((r = p.match(/\/pulls\/(\d+)$/)) && m === "PATCH") { w.closed = Number(r[1]); return J(200, {}); }
    if (p.endsWith("/commits") && m === "GET") return J(200, [{ sha: "a".repeat(40), commit: { message: "dashboard: x\n\nmore", author: { name: "n", date: "2026-10-25T00:00:00Z" } } }]);
    return J(500, { unexpected: `${m} ${p}` });
  };
  return w;
}

/** Opens a sealed secret the way GitHub would, to prove the dashboard encrypted it for the right key. */
export function openSealed(w, b64) {
  const sealed = Buffer.from(b64, "base64"), epk = new Uint8Array(sealed.subarray(0, 32)), pk = w.keyPair.publicKey;
  const nin = new Uint8Array(64); nin.set(epk); nin.set(pk, 32);
  const out = nacl.box.open(new Uint8Array(sealed.subarray(32)), blake2b(nin, 24), epk, w.keyPair.secretKey);
  return out ? new TextDecoder().decode(out) : null;
}
