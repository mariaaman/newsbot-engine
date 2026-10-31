// Settings: read the two config files, apply validated edits with the comment-preserving YAML editor, commit, then ask CI to validate.
import { FIELDS, GROUPS, findField, coerce, TASKS, PROVIDERS, TIERS, KEY_RE, MODEL_RE } from "./schema.js";
import { get, set, remove, listInsert, listRemove, flatten, reader } from "./yamledit.js";
import { getFile, putFile, dispatch, history } from "./github.js";
import { HttpError, must, isObj } from "./util.js";

export const CONFIG_FILES = ["config/newsbot.yaml", "config/llm.yaml"];
const RAW_OK = /^(config\/[A-Za-z0-9_.-]+\.yaml|prompts\/[A-Za-z0-9_.-]+\.md)$/;
export const checkRawPath = (p) => must(typeof p === "string" && RAW_OK.test(p) && !p.includes(".."), 400, "that file cannot be edited here");

const optionsFor = (field) => field;

export async function describe(env) {
  const out = { groups: GROUPS, files: {}, fields: [], chains: {}, budget: { tokens_per_day: {}, requests_per_day: {}, neurons_per_day: {} }, tasks: {}, embedding: {}, tiers: TIERS, providers: PROVIDERS, taskNames: TASKS };
  const texts = {};
  for (const path of CONFIG_FILES) {
    const f = await getFile(env, path);
    if (!f) { out.files[path] = null; continue; }
    out.files[path] = { sha: f.sha, size: f.size };
    texts[path] = f.text;
  }
  const read = Object.fromEntries(Object.entries(texts).map(([k, t]) => [k, reader(t)]));
  for (const f of FIELDS) {
    const v = read[f.file] ? read[f.file](f.path) : undefined;
    out.fields.push({ ...optionsFor(f), value: v === undefined ? null : v, present: v !== undefined });
  }
  const llm = texts["config/llm.yaml"];
  if (llm) {
    const rd = read["config/llm.yaml"], flat = flatten(llm);
    for (const tier of TIERS) {
      const list = [];
      for (let i = 0; rd(["llm", "tiers", tier, i, "provider"]) !== undefined; i++) list.push({ provider: rd(["llm", "tiers", tier, i, "provider"]), model: rd(["llm", "tiers", tier, i, "model"]) });
      out.chains[tier] = list;
    }
    for (const [k, v] of Object.entries(flat)) {
      const m = /^llm\.budget\.(tokens_per_day|requests_per_day|neurons_per_day)\.(.+)$/.exec(k);
      if (m) out.budget[m[1]][m[2]] = v;
    }
    for (const t of TASKS) out.tasks[t] = rd(["llm", "tasks", t]) ?? null;
    out.embedding = { provider: rd(["llm", "embedding", "provider"]) ?? null, model: rd(["llm", "embedding", "model"]) ?? null };
  }
  return out;
}

/** One change -> new text + a short summary. Throws HttpError(400) with a plain message on anything not allowed. */
export function applyChange(file, text, ch) {
  must(isObj(ch), 400, "bad change");
  try {
    if (ch.kind === "field") {
      const field = findField(file, ch.path);
      must(field, 400, "that setting cannot be edited here");
      const v = coerce(field, ch.value);
      return { text: set(text, field.path, v), summary: `${field.path.join(".")}=${Array.isArray(v) ? v.length + " items" : String(v).slice(0, 30)}` };
    }
    must(file === "config/llm.yaml", 400, "that edit belongs to llm.yaml");
    if (ch.kind === "chain") {
      must(TIERS.includes(ch.tier), 400, "unknown tier");
      const tierPath = ["llm", "tiers", ch.tier];
      if (ch.op === "remove") { must(Number.isInteger(ch.index), 400, "bad index"); return { text: listRemove(text, tierPath, ch.index), summary: `chain ${ch.tier}: remove #${ch.index}` }; }
      must(PROVIDERS.includes(ch.provider), 400, "unknown provider");
      must(typeof ch.model === "string" && MODEL_RE.test(ch.model), 400, "model id looks wrong");
      if (ch.op === "insert") { must(Number.isInteger(ch.index) && ch.index >= 0, 400, "bad index"); return { text: listInsert(text, tierPath, ch.index, { provider: ch.provider, model: ch.model }), summary: `chain ${ch.tier}: add ${ch.provider}` }; }
      must(ch.op === "set" && Number.isInteger(ch.index) && ch.index >= 0, 400, "bad chain edit");
      must(get(text, [...tierPath, ch.index, "provider"]) !== undefined, 400, "no such chain entry");
      let t = set(text, [...tierPath, ch.index, "provider"], ch.provider);
      t = set(t, [...tierPath, ch.index, "model"], ch.model);
      return { text: t, summary: `chain ${ch.tier}#${ch.index}=${ch.model.slice(0, 30)}` };
    }
    if (ch.kind === "budget") {
      must(["tokens_per_day", "requests_per_day", "neurons_per_day"].includes(ch.map), 400, "unknown budget");
      must(typeof ch.key === "string" && KEY_RE.test(ch.key), 400, "budget key looks wrong");
      const path = ["llm", "budget", ch.map, ch.key];
      if (ch.op === "remove") return { text: remove(text, path), summary: `budget ${ch.map}: remove ${ch.key}` };
      must(Number.isInteger(ch.value) && ch.value >= 0, 400, "budget must be a whole number, 0 or more");
      return { text: set(text, path, ch.value), summary: `budget ${ch.map}.${ch.key}=${ch.value}` };
    }
    if (ch.kind === "task") {
      must(TASKS.includes(ch.task) && TIERS.includes(ch.tier), 400, "unknown task or tier");
      return { text: set(text, ["llm", "tasks", ch.task], ch.tier), summary: `task ${ch.task}=${ch.tier}` };
    }
    if (ch.kind === "embedding") {
      must(PROVIDERS.includes(ch.provider) && typeof ch.model === "string" && MODEL_RE.test(ch.model), 400, "embedding provider or model looks wrong");
      return { text: set(set(text, ["llm", "embedding", "provider"], ch.provider), ["llm", "embedding", "model"], ch.model), summary: `embedding=${ch.model.slice(0, 30)}` };
    }
  } catch (e) {
    if (e instanceof HttpError) throw e;
    throw new HttpError(400, e.message);
  }
  throw new HttpError(400, "unknown kind of change");
}

/** body: { changes: [{file, ...}], versions: { file: sha } }. One commit per file. Returns what happened per file. */
export async function applySettings(env, body) {
  must(isObj(body) && Array.isArray(body.changes) && body.changes.length > 0 && body.changes.length <= 60, 400, "nothing to change");
  const byFile = {};
  for (const ch of body.changes) {
    must(isObj(ch) && CONFIG_FILES.includes(ch.file), 400, "unknown file");
    (byFile[ch.file] ||= []).push(ch);
  }
  const results = [];
  for (const [file, changes] of Object.entries(byFile)) {
    const cur = await getFile(env, file);
    must(cur, 404, `${file} not found in the brain repo`);
    if (body.versions && body.versions[file] && body.versions[file] !== cur.sha) throw new HttpError(409, `${file} changed meanwhile. Reload and try again.`);
    let text = cur.text;
    const before = flatten(text), parts = [];
    for (const ch of changes) {
      const r = applyChange(file, text, ch);
      text = r.text; parts.push(r.summary);
    }
    const after = flatten(text);
    const removed = changes.some((c) => c.op === "remove");
    if (!removed) for (const k of Object.keys(before)) must(k in after, 500, "internal check failed: the edit would lose a setting. Nothing was saved.");
    if (text === cur.text) { results.push({ file, changed: false }); continue; }
    const sha = await putFile(env, file, text, cur.sha, `dashboard: ${parts.join("; ").slice(0, 150)}`);
    results.push({ file, changed: true, sha, summary: parts });
  }
  if (results.some((r) => r.changed)) await dispatch(env, "validate-config").catch(() => {});
  return results;
}

// ---- raw files (other config, prompts) ----
export async function readRaw(env, path) {
  checkRawPath(path);
  const f = await getFile(env, path);
  must(f, 404, "file not found");
  return { path, text: f.text, sha: f.sha, history: await history(env, path).catch(() => []) };
}

export async function writeRaw(env, body) {
  checkRawPath(body.path);
  must(typeof body.text === "string" && body.text.length > 0 && body.text.length <= 200_000, 400, "the text is empty or too long");
  const cur = await getFile(env, body.path);
  must(cur, 404, "file not found");
  if (body.sha && body.sha !== cur.sha) throw new HttpError(409, "The file changed meanwhile. Reload and try again.");
  const isYaml = body.path.endsWith(".yaml");
  const text = body.text.replace(/\r\n?/g, "\n");
  if (isYaml) {
    must(!/\t/.test(text), 400, "YAML cannot contain tab characters");
    try {
      const a = Object.keys(flatten(cur.text)), b = new Set(Object.keys(flatten(text)));
      if (CONFIG_FILES.includes(body.path) && !body.allowStructure) {
        const lost = a.filter((k) => !b.has(k));
        must(lost.length === 0, 422, `this edit removes ${lost.length} setting(s) (${lost.slice(0, 3).join(", ")}...). Confirm to allow it.`);
      }
    } catch (e) { if (e instanceof HttpError) throw e; throw new HttpError(400, "could not read the YAML"); }
  }
  if (text === cur.text) return { changed: false };
  const sha = await putFile(env, body.path, text, cur.sha, `dashboard: edit ${body.path}`);
  if (isYaml) await dispatch(env, "validate-config").catch(() => {});
  return { changed: true, sha };
}

export async function restore(env, body) {
  checkRawPath(body.path);
  must(typeof body.ref === "string" && /^[0-9a-f]{7,40}$/.test(body.ref), 400, "bad version");
  const old = await getFile(env, body.path, body.ref);
  must(old, 404, "that version has no such file");
  const cur = await getFile(env, body.path);
  must(cur, 404, "file not found");
  if (old.text === cur.text) return { changed: false };
  const sha = await putFile(env, body.path, old.text, cur.sha, `dashboard: restore ${body.path} to ${body.ref.slice(0, 7)}`);
  if (body.path.endsWith(".yaml")) await dispatch(env, "validate-config").catch(() => {});
  return { changed: true, sha };
}
