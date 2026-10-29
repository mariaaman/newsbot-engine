// NewsBot dashboard: one Cloudflare Worker (static files + JSON API). The brain repo on GitHub is the database; KV only holds log-in state.
import { SEC, json, HttpError, must, isObj } from "./util.js";
import { readSession, login, clearCookie, authStatus, logoutAll, totpBegin, totpConfirm, totpDisable, audit, clientIp } from "./auth.js";
import * as gh from "./github.js";
import * as settings from "./settings.js";
import * as articles from "./articles.js";
import { KEYS, testKey, checkSecretName, checkSecretValue, VAR_NAME } from "./keys.js";
import { reader } from "./yamledit.js";

const PUBLIC_ASSETS = new Set(["/login.html", "/login.js", "/style.css", "/favicon.svg", "/manifest.webmanifest"]);
const PRIVATE_ASSETS = new Set(["/app.js", "/lib.js"]);

async function asset(env, path, status = 200) {
  const r = await env.ASSETS.fetch(new Request("https://assets.local" + path));
  const h = new Headers(r.headers);
  for (const [k, v] of Object.entries(SEC)) h.set(k, v);
  if (path.endsWith(".js")) h.set("Content-Type", "text/javascript; charset=utf-8");
  if (path.endsWith(".css")) h.set("Content-Type", "text/css; charset=utf-8");
  if (path.endsWith(".html")) h.set("Content-Type", "text/html; charset=utf-8");
  if (path.endsWith(".svg")) h.set("Content-Type", "image/svg+xml");
  return new Response(r.body, { status: r.ok ? status : r.status, headers: h });
}

async function body(request) {
  const text = await request.text();
  must(text.length <= 400_000, 413, "request too large");
  if (!text) return {};
  try { const j = JSON.parse(text); must(isObj(j), 400, "bad request"); return j; } catch (e) { if (e instanceof HttpError) throw e; throw new HttpError(400, "bad JSON"); }
}

function sameOrigin(request) {
  const url = new URL(request.url), origin = request.headers.get("Origin");
  if (origin && new URL(origin).host !== url.host) return false;
  const site = request.headers.get("Sec-Fetch-Site");
  if (site && !["same-origin", "none"].includes(site)) return false;
  return request.headers.get("X-Requested-With") === "newsbot";
}

const parseJson = (s, fallback) => { try { return JSON.parse(s); } catch { return fallback; } };

async function siteCheck(url) {
  if (!/^https:\/\/[A-Za-z0-9.-]+(:\d+)?(\/.*)?$/.test(url || "")) return null;
  const t0 = Date.now();
  try { const r = await fetch(url, { method: "GET", redirect: "follow", signal: AbortSignal.timeout(6000), headers: { "User-Agent": "newsbot-dashboard" } }); await r.arrayBuffer(); return { status: r.status, ms: Date.now() - t0 }; }
  catch { return { status: 0, ms: Date.now() - t0 }; }
}

async function overview(env) {
  const snap = await gh.brainSnapshot(env);
  const yget = reader(snap.newsbot);
  const base = yget(["site", "base_url"]);
  const [rs, secrets, site] = await Promise.all([gh.runs(env, 20).catch(() => null), gh.listSecrets(env).catch(() => null), siteCheck(base)]);
  const usage = parseJson(snap.usage, {});
  const lastOf = (name) => (rs || []).find((r) => r.name === name) || null;
  return {
    site: { name: yget(["site", "name"]), url: base, check: site, deployed: parseJson(snap.deployed, {}).deployed_at || null },
    mode: yget(["publish", "mode"]) || "review",
    ads: yget(["ads", "enabled"]) === true,
    counts: snap.counts,
    usage: { day: usage.day || null, counters: usage.counters || {} },
    cooldowns: Object.keys(parseJson(snap.cooldowns, {})).length,
    runs: rs, lastHourly: lastOf("hourly"), lastDeploy: lastOf("deploy"),
    secrets: secrets ? secrets.map((s) => s.name) : null,
  };
}

async function decisions(env) {
  const { text, size } = await gh.rawTail(env, "state/decisions.jsonl", 30000);
  const lines = text.split("\n").filter(Boolean);
  if (size > 30000) lines.shift();                      // the first line of a tail is usually cut in half
  return { size, items: lines.slice(-60).map((l) => parseJson(l, null)).filter(Boolean).reverse() };
}

const ROUTES = {
  "GET /api/overview": (env) => overview(env),
  "GET /api/runs": async (env) => ({ runs: await gh.runs(env, 30) }),
  "GET /api/decisions": (env) => decisions(env),
  "GET /api/audit": async (env) => ({ items: parseJson(await env.KV.get("audit"), []) }),
  "POST /api/dispatch": async (env, req, b) => {
    must(typeof b.workflow === "string", 400, "workflow?");
    let inputs;
    if (b.workflow === "hourly" && b.mode) { must(["auto", "review", "dry-run"].includes(b.mode), 400, "bad mode"); inputs = { mode: b.mode }; }
    await gh.dispatch(env, b.workflow, inputs);
    await audit(env, req, "run workflow", `${b.workflow}${inputs ? " " + inputs.mode : ""}`);
    return { ok: true };
  },
  "GET /api/drafts": (env, req, b, u) => articles.list(env, "pending", u.searchParams.get("lang"), Number(u.searchParams.get("offset")) || 0, u.searchParams.get("q") || ""),
  "GET /api/draft": (env, req, b, u) => articles.open(env, "pending", u.searchParams.get("lang"), u.searchParams.get("slug")),
  "POST /api/draft": async (env, req, b) => {
    const r = await articles.draftAction(env, b);
    await audit(env, req, `draft ${b.action}`, `${b.lang}/${b.slug} -> ${r.key}`);
    return r;
  },
  "GET /api/articles": (env, req, b, u) => articles.list(env, "content", u.searchParams.get("lang"), Number(u.searchParams.get("offset")) || 0, u.searchParams.get("q") || ""),
  "GET /api/article": (env, req, b, u) => articles.open(env, "content", u.searchParams.get("lang"), u.searchParams.get("slug")),
  "POST /api/article": async (env, req, b) => {
    const r = await articles.articleAction(env, b);
    await audit(env, req, `article ${b.action}`, `${b.lang}/${b.slug} -> ${r.key}`);
    return r;
  },
  "GET /api/prs": async (env) => ({ prs: await gh.openPrs(env) }),
  "POST /api/pr": async (env, req, b) => {
    await gh.prAction(env, b.number, b.action);
    if (b.action === "merge") await gh.deploy(env).catch(() => {});
    await audit(env, req, `pr ${b.action}`, `#${b.number}`);
    return { ok: true };
  },
  "GET /api/settings": (env) => settings.describe(env),
  "POST /api/settings": async (env, req, b) => {
    const r = await settings.applySettings(env, b);
    await audit(env, req, "settings", r.flatMap((x) => x.summary || []).join("; "));
    return { results: r };
  },
  "GET /api/file": (env, req, b, u) => settings.readRaw(env, u.searchParams.get("path")),
  "POST /api/file": async (env, req, b) => {
    const r = await settings.writeRaw(env, b);
    await audit(env, req, "file edit", b.path);
    return r;
  },
  "POST /api/restore": async (env, req, b) => {
    const r = await settings.restore(env, b);
    await audit(env, req, "file restore", `${b.path} @ ${String(b.ref).slice(0, 7)}`);
    return r;
  },
  "GET /api/keys": async (env) => ({
    keys: KEYS.map((k) => ({ name: k.name, label: k.label, testable: true, after: k.after || null })),
    secrets: await gh.listSecrets(env), vars: await gh.listVars(env),
  }),
  "POST /api/keys/test": async (env, req, b) => {
    checkSecretName(b.name); checkSecretValue(b.value);
    return testKey(env, b.name, b.value);
  },
  "POST /api/keys/set": async (env, req, b) => {
    checkSecretName(b.name); checkSecretValue(b.value);
    const test = await testKey(env, b.name, b.value);
    if (test.ok === false && b.force !== true) throw new HttpError(422, `Not saved: ${test.detail}`, { test });
    const status = await gh.putSecret(env, b.name, b.value.trim());
    await audit(env, req, "key set", `${b.name} (${status})`);          // the name only, never the value
    return { status, test, after: KEYS.find((k) => k.name === b.name)?.after || null };
  },
  "POST /api/vars/set": async (env, req, b) => {
    must(typeof b.name === "string" && VAR_NAME.test(b.name), 400, "variable names: capital letters, digits and _");
    must(typeof b.value === "string" && b.value.length <= 1000 && !/[\r\n]/.test(b.value), 400, "the value must be one line (up to 1000 characters)");
    const status = await gh.upsertVar(env, b.name, b.value);
    await audit(env, req, "variable set", `${b.name} (${status})`);
    return { status };
  },
  "POST /api/totp/begin": (env, req, b) => totpBegin(env, b),
  "POST /api/totp/confirm": async (env, req, b) => { await totpConfirm(env, b); await audit(env, req, "two-step on"); return { ok: true }; },
  "POST /api/totp/disable": async (env, req, b) => { await totpDisable(env, b); await audit(env, req, "two-step off"); return { ok: true }; },
  "POST /api/logout-all": async (env, req) => { await logoutAll(env); await audit(env, req, "logout everywhere"); return { ok: true }; },
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url), path = url.pathname;
    try {
      const session = await readSession(request, env);
      // ---- pages and static files ----
      if (request.method === "GET" && (path === "/" || path === "/index.html")) return asset(env, session ? "/index.html" : "/login.html");
      if (request.method === "GET" && PUBLIC_ASSETS.has(path)) return asset(env, path);
      if (request.method === "GET" && PRIVATE_ASSETS.has(path)) { if (!session) return json({ error: "login" }, 401); return asset(env, path); }
      if (!path.startsWith("/api/")) return json({ error: "not found" }, 404);
      // ---- public API ----
      if (request.method === "GET" && path === "/api/session") return json({ authenticated: !!session, ...(await authStatus(env)) });
      if (request.method !== "GET" && !sameOrigin(request)) throw new HttpError(403, "cross-site request refused");
      if (request.method === "POST" && path === "/api/login") {
        const cookie = await login(request, env, await body(request));
        await audit(env, request, "login");
        return json({ ok: true }, 200, { "Set-Cookie": cookie });
      }
      if (request.method === "POST" && path === "/api/logout") return json({ ok: true }, 200, { "Set-Cookie": clearCookie() });
      // ---- everything below needs a session ----
      if (!session) throw new HttpError(401, "login");
      const handler = ROUTES[`${request.method} ${path}`];
      if (!handler) throw new HttpError(404, "not found");
      gh.checkEnv(env);
      const b = request.method === "POST" ? await body(request) : {};
      const out = await handler(env, request, b, url);
      return json(out, 200, path === "/api/logout-all" ? { "Set-Cookie": clearCookie() } : {});
    } catch (e) {
      if (e instanceof HttpError) return json({ error: e.message, ...e.extra }, e.status);
      console.error("internal", e && e.message);
      return json({ error: "internal error" }, 500);
    }
  },
};
void clientIp;
