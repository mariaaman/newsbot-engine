// GitHub access for the dashboard. Reuses the approve Worker's tested helpers (worker/index.js): gh(), commit(), deploy(), act().
import { gh, b64utf8, commit, deploy, act, draftRef, applyEdits, split, readField, findDraft } from "../../worker/index.js";
import { sealToBase64 } from "./sealedbox.js";
import { HttpError } from "./util.js";

export { act, commit, deploy, draftRef, applyEdits, split, readField, findDraft };

const REPO = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
export const repos = (env) => ({ brain: env.BRAIN_REPO, branch: env.BRAIN_BRANCH || "main", engine: env.ENGINE_REPO, eb: env.ENGINE_BRANCH || "main" });
export function checkEnv(env) {
  const r = repos(env);
  if (!env.GH_TOKEN) throw new HttpError(500, "GH_TOKEN is missing");
  if (!REPO.test(r.brain || "") || !REPO.test(r.engine || "")) throw new HttpError(500, "BRAIN_REPO / ENGINE_REPO are not set right");
}

const q = encodeURIComponent;
const encPath = (p) => p.split("/").map(q).join("/");
const need = (res, ...ok) => { if (!ok.includes(res.status)) throw new HttpError(res.status === 404 ? 404 : res.status === 403 || res.status === 401 ? 502 : 502, `GitHub answered ${res.status}`, { github: res.status }); return res.json; };

function b64(text) {
  const bytes = new TextEncoder().encode(text);
  let s = "";
  for (let i = 0; i < bytes.length; i += 8192) s += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(s);
}

export async function getFile(env, path, ref) {
  const { brain, branch } = repos(env);
  const res = await gh(env, `/repos/${brain}/contents/${encPath(path)}?ref=${q(ref || branch)}`);
  if (res.status === 404) return null;
  const j = need(res, 200);
  if (Array.isArray(j)) throw new HttpError(400, "that is a folder");
  if ((j.size || 0) > 1_000_000) throw new HttpError(413, "file is larger than 1 MB");
  return { text: b64utf8(j.content || ""), sha: j.sha, size: j.size };
}

export async function putFile(env, path, text, sha, message) {
  const { brain, branch } = repos(env);
  const res = await gh(env, `/repos/${brain}/contents/${encPath(path)}`, { method: "PUT", body: JSON.stringify({ message, content: b64(text), branch, ...(sha ? { sha } : {}) }) });
  if (res.status === 409 || res.status === 422) throw new HttpError(409, "The file changed meanwhile. Reload and try again.");
  return need(res, 200, 201).content.sha;
}

export async function listDir(env, dir) {
  const { brain, branch } = repos(env);
  const res = await gh(env, `/repos/${brain}/contents/${encPath(dir)}?ref=${q(branch)}`);
  if (res.status === 404) return [];
  const j = need(res, 200);
  return (Array.isArray(j) ? j : []).filter((f) => f.type === "file").map((f) => ({ name: f.name, size: f.size }));
}

export async function graphql(env, query) {
  const res = await gh(env, "/graphql", { method: "POST", body: JSON.stringify({ query }) });
  const j = need(res, 200);
  if (j.errors && !j.data) throw new HttpError(502, "GitHub GraphQL error");
  return j.data;
}

/** First 3000 characters (the front matter) of many files in ONE request. names: file names inside `dir`. */
export async function heads(env, dir, names) {
  if (!names.length) return {};
  const { brain, branch } = repos(env);
  const [owner, name] = brain.split("/");
  const parts = names.map((n, i) => `f${i}: object(expression: ${JSON.stringify(`${branch}:${dir}/${n}`)}) { ... on Blob { text } }`);
  const data = await graphql(env, `{ repository(owner: ${JSON.stringify(owner)}, name: ${JSON.stringify(name)}) { ${parts.join(" ")} } }`);
  const out = {};
  names.forEach((n, i) => { const t = data.repository?.[`f${i}`]?.text; out[n] = typeof t === "string" ? t.slice(0, 3000) : ""; });
  return out;
}

export async function brainSnapshot(env) {
  const { brain, branch } = repos(env);
  const [owner, name] = brain.split("/");
  const blob = (alias, path) => `${alias}: object(expression: ${JSON.stringify(`${branch}:${path}`)}) { ... on Blob { text } }`;
  const tree = (alias, path) => `${alias}: object(expression: ${JSON.stringify(`${branch}:${path}`)}) { ... on Tree { entries { name } } }`;
  const data = await graphql(env, `{ repository(owner: ${JSON.stringify(owner)}, name: ${JSON.stringify(name)}) {
    ${blob("newsbot", "config/newsbot.yaml")} ${blob("usage", "state/usage.json")} ${blob("cooldowns", "state/cooldowns.json")} ${blob("deployed", "state/deployed.json")}
    ${tree("pendingBn", "pending/bn")} ${tree("pendingEn", "pending/en")} ${tree("contentBn", "content/bn")} ${tree("contentEn", "content/en")} } }`);
  const r = data.repository || {};
  const n = (k) => (r[k]?.entries || []).filter((e) => e.name.endsWith(".md")).length;
  return { newsbot: r.newsbot?.text || "", usage: r.usage?.text || "", cooldowns: r.cooldowns?.text || "", deployed: r.deployed?.text || "",
    counts: { pending: { bn: n("pendingBn"), en: n("pendingEn") }, published: { bn: n("contentBn"), en: n("contentEn") } } };
}

export async function runs(env, limit = 20) {
  const { engine } = repos(env);
  const j = need(await gh(env, `/repos/${engine}/actions/runs?per_page=${limit}`), 200);
  return (j.workflow_runs || []).map((r) => ({ id: r.id, name: r.name, event: r.event, status: r.status, conclusion: r.conclusion, created: r.created_at, started: r.run_started_at, updated: r.updated_at, url: r.html_url, title: (r.display_title || "").slice(0, 100) }));
}

export const WORKFLOWS = ["hourly", "deploy", "validate-config", "setup-approve", "setup", "readiness", "llm-check"];
export async function dispatch(env, workflow, inputs) {
  if (!WORKFLOWS.includes(workflow)) throw new HttpError(400, "unknown workflow");
  const { engine, eb } = repos(env);
  const res = await gh(env, `/repos/${engine}/actions/workflows/${workflow}.yml/dispatches`, { method: "POST", body: JSON.stringify({ ref: eb, ...(inputs ? { inputs } : {}) }) });
  if (res.status !== 204) throw new HttpError(502, `GitHub answered ${res.status}`, { github: res.status });
}

export async function listSecrets(env) {
  const { engine } = repos(env);
  const j = need(await gh(env, `/repos/${engine}/actions/secrets?per_page=100`), 200);
  return (j.secrets || []).map((s) => ({ name: s.name, updated: s.updated_at }));
}
export async function deleteSecret(env, name) {
  const { engine } = repos(env);
  const res = await gh(env, `/repos/${engine}/actions/secrets/${q(name)}`, { method: "DELETE" });
  need(res, 204, 404);
  return res.status === 204 ? "deleted" : "absent";
}
export async function deleteVar(env, name) {
  const { engine } = repos(env);
  const res = await gh(env, `/repos/${engine}/actions/variables/${q(name)}`, { method: "DELETE" });
  need(res, 204, 404);
  return res.status === 204 ? "deleted" : "absent";
}
export async function listVars(env) {
  const { engine } = repos(env);
  const j = need(await gh(env, `/repos/${engine}/actions/variables?per_page=100`), 200);
  return (j.variables || []).map((v) => ({ name: v.name, value: v.value, updated: v.updated_at }));
}
export async function upsertVar(env, name, value) {
  const { engine } = repos(env);
  const patch = await gh(env, `/repos/${engine}/actions/variables/${q(name)}`, { method: "PATCH", body: JSON.stringify({ name, value }) });
  if (patch.status === 204) return "updated";
  if (patch.status !== 404) need(patch, 204);
  need(await gh(env, `/repos/${engine}/actions/variables`, { method: "POST", body: JSON.stringify({ name, value }) }), 201);
  return "created";
}
export async function putSecret(env, name, value) {
  const { engine } = repos(env);
  const pk = need(await gh(env, `/repos/${engine}/actions/secrets/public-key`), 200);
  const res = await gh(env, `/repos/${engine}/actions/secrets/${q(name)}`, { method: "PUT", body: JSON.stringify({ encrypted_value: sealToBase64(value, pk.key), key_id: pk.key_id }) });
  need(res, 201, 204);
  return res.status === 201 ? "created" : "updated";
}

export async function openPrs(env) {
  const { brain } = repos(env);
  const j = need(await gh(env, `/repos/${brain}/pulls?state=open&per_page=30`), 200);
  return j.map((p) => ({ number: p.number, title: (p.title || "").slice(0, 140), url: p.html_url, created: p.created_at, user: p.user?.login }));
}
export async function prAction(env, number, action) {
  const { brain } = repos(env);
  if (!Number.isInteger(number) || number < 1) throw new HttpError(400, "bad PR number");
  if (action === "merge") {
    const r = await gh(env, `/repos/${brain}/pulls/${number}/merge`, { method: "PUT", body: JSON.stringify({ merge_method: "squash" }) });
    if (r.status === 405 || r.status === 409) throw new HttpError(409, "GitHub cannot merge this PR right now");
    need(r, 200);
    return;
  }
  if (action === "close") { need(await gh(env, `/repos/${brain}/pulls/${number}`, { method: "PATCH", body: JSON.stringify({ state: "closed" }) }), 200); return; }
  throw new HttpError(400, "unknown action");
}

export async function history(env, path) {
  const { brain, branch } = repos(env);
  const j = need(await gh(env, `/repos/${brain}/commits?path=${q(path)}&sha=${q(branch)}&per_page=10`), 200);
  return j.map((c) => ({ sha: c.sha, message: (c.commit?.message || "").split("\n")[0].slice(0, 120), date: c.commit?.author?.date, author: c.commit?.author?.name }));
}

export async function rawTail(env, path, bytes = 12000) {
  const { brain, branch } = repos(env);
  const meta = await gh(env, `/repos/${brain}/contents/${encPath(path)}?ref=${q(branch)}`);
  if (meta.status === 404) return { text: "", size: 0 };
  const j = need(meta, 200);
  if ((j.size || 0) > 3_000_000) throw new HttpError(413, "file is larger than 3 MB");
  const r = await fetch(j.download_url || "", { headers: { Authorization: `Bearer ${env.GH_TOKEN}` } }).catch(() => null);
  const text = r && r.ok ? await r.text() : j.content ? b64utf8(j.content) : "";
  return { text: text.slice(-bytes), size: j.size || 0 };
}
