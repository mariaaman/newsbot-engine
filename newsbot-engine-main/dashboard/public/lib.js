// Helpers for app.js: a tiny element builder, the API caller, formatting, and the draft front-matter reader.
// No innerHTML except where the caller passes server-escaped preview HTML; every other text goes in as text nodes.

export const LANGS = [["bn", "বাংলা"], ["en", "English"]];          // keep in step with site.languages in config/newsbot.yaml
export const DEFAULT_LANG = "bn";

// Secrets the bot needs to run end to end (the dashboard shows which are missing; it can never read a value).
export const REQUIRED_SECRETS = ["BRAIN_REPO_TOKEN", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "APPROVE_GH_TOKEN"];

export const RAW_FILES = [
  "config/newsbot.yaml", "config/llm.yaml",
  ...["bn", "en", "ar", "de", "es", "fa", "fr", "hi", "id", "ja", "ko", "ne", "pt", "ru", "sw", "th", "tr", "ur", "vi", "zh"].map((l) => `config/sources.${l}.yaml`),
  "prompts/extract.md", "prompts/verify.md", "prompts/write_default.md", "prompts/write_bn.md", "prompts/write_en.md", "prompts/qa_check.md", "prompts/update.md",
];

export const WORKFLOW_LABELS = [
  ["hourly", "hourly: এখনই একটা রান"], ["deploy", "deploy: সাইট আবার প্রকাশ"], ["validate-config", "validate-config: কনফিগ যাচাই"], ["llm-check", "llm-check: সব LLM অ্যাকাউন্ট যাচাই"],
  ["readiness", "readiness: প্রস্তুতি-পরীক্ষা"], ["setup-approve", "setup-approve: অনুমোদন Worker বসান"],
];

export const MSG = {
  approved: "✅ অনুমোদিত। ১–৩ মিনিটের মধ্যে সাইটে প্রকাশ হবে।",
  approvedNoDeploy: "✅ অনুমোদিত, কিন্তু ডিপ্লয় চালু করা যায়নি। পরের hourly রানে বা deploy চালালে প্রকাশ হবে।",
  rejected: "🗑 বাতিল করা হয়েছে, খসড়া মুছে গেছে।",
  saved: "💾 সংরক্ষিত হয়েছে। এখনো প্রকাশ হয়নি।",
  gone: "ℹ️ এটি আগেই নিষ্পন্ন হয়েছে (খসড়া আর pending-এ নেই)।",
  gate: "⛔ মান-যাচাইয়ে ব্যর্থ খসড়া। নিজে দেখে নিচের টিক দিয়ে আবার চেষ্টা করুন।",
  stale: "⚠️ খসড়াটি ইতিমধ্যে বদলে গেছে। পাতা রিফ্রেশ করে আবার চেষ্টা করুন, কিছু সংরক্ষিত হয়নি।",
  published: "✅ সংরক্ষিত। ১–৩ মিনিটের মধ্যে সাইটে দেখা যাবে।",
  publishedNoDeploy: "✅ সংরক্ষিত, কিন্তু ডিপ্লয় চালু করা যায়নি। deploy চালান।",
  nochange: "ℹ️ কিছু বদলায়নি।",
};

export class ApiError extends Error {
  constructor(status, message, data) { super(message); this.status = status; this.data = data || null; }
}

const PROP = new Set(["value", "checked", "disabled", "hidden", "type", "open", "selected", "textContent", "innerHTML", "id", "title", "placeholder", "maxLength", "href", "target", "rel", "rows", "min", "max", "step", "autocomplete", "inputMode", "name", "htmlFor"]);

/** h("div", {class: "card", onclick: fn}, "text", child, [more children]) */
export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (PROP.has(k)) el[k] = typeof v === "number" ? String(v) : v;
    else el.setAttribute(k, String(v));
  }
  const add = (x) => {
    if (x === undefined || x === null || x === false) return;
    if (Array.isArray(x)) { x.forEach(add); return; }
    el.append(typeof x === "object" ? x : document.createTextNode(String(x)));
  };
  kids.forEach(add);
  return el;
}

export const $ = (id) => document.getElementById(id);

/** Flattens nested arrays and drops null/false, so replaceChildren/append never receive an array or a null. */
export const flat = (...xs) => xs.flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false);

let toastTimer = null;
export function toast(text, kind = "") {
  const t = $("toast");
  t.textContent = text; t.className = "toast " + kind; t.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, kind === "bad" ? 7000 : 4000);
}

/** GET when `body` is undefined, otherwise POST JSON. Throws ApiError with the server's message. */
export async function api(path, body) {
  const init = { credentials: "same-origin", headers: { "X-Requested-With": "newsbot" } };
  if (body !== undefined) { init.method = "POST"; init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  const r = await fetch(path, init);
  let j = null;
  try { j = await r.json(); } catch { /* not JSON */ }
  if (r.status === 401) { location.href = "/"; throw new ApiError(401, "আবার লগইন করুন", j); }
  if (!r.ok) throw new ApiError(r.status, (j && j.error) || `HTTP ${r.status}`, j);
  return j;
}

/** Runs fn while the button is disabled; shows the error as a toast instead of throwing. */
export async function guard(btn, fn) {
  if (btn) btn.disabled = true;
  try { return await fn(); }
  catch (e) { toast("ত্রুটি: " + (e && e.message ? e.message : String(e)), "bad"); }
  finally { if (btn) btn.disabled = false; }
}

export const errorCard = (e) => h("div", { class: "card bad" }, "ত্রুটি: " + (e && e.message ? e.message : String(e)));

// ---------- formatting ----------
export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  try { return d.toLocaleString("bn-BD", { timeZone: "Asia/Dhaka", dateStyle: "medium", timeStyle: "short" }); } catch { return d.toISOString().slice(0, 16).replace("T", " "); }
}

export function ago(iso) {
  const t = new Date(iso).getTime();
  if (!t) return "";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 90) return "এইমাত্র";
  if (s < 3600) return `${Math.round(s / 60)} মিনিট আগে`;
  if (s < 86400) return `${Math.round(s / 3600)} ঘণ্টা আগে`;
  return `${Math.round(s / 86400)} দিন আগে`;
}

export const num = (n) => Number(n || 0).toLocaleString("en-US");

export function runChip(r) {
  if (!r) return h("span", { class: "chip" }, "কোনো রান নেই");
  if (r.status !== "completed") return h("span", { class: "chip warn" }, "চলছে");
  if (r.conclusion === "success") return h("span", { class: "chip ok" }, "সফল");
  if (r.conclusion === "cancelled" || r.conclusion === "skipped") return h("span", { class: "chip" }, r.conclusion);
  return h("span", { class: "chip bad" }, "ব্যর্থ");
}

// ---------- articles ----------
export function titleOf(head) {
  const m = String(head || "").match(/^title:\s*(.*)$/m);
  if (!m) return "(শিরোনাম নেই)";
  let t = m[1].trim();
  if ((t.startsWith('"') && t.endsWith('"') && t.length > 1) || (t.startsWith("'") && t.endsWith("'") && t.length > 1)) t = t.slice(1, -1).replace(/\\"/g, '"');
  return t || "(শিরোনাম নেই)";
}

/** The text under `pending:` in a front matter / head. */
export function pendingOf(head) {
  const m = (String(head || "") + "\n").match(/^pending:[^\n]*\n(?:[ \t]+[^\n]*\n)*/m);
  return m ? m[0].replace(/^pending:[^\n]*\n/, "") : "";
}

/** { failures: [...], review: [...] } from the pending block (list items may sit at the same indent as their key). */
export function parsePending(text) {
  const out = { failures: [], review: [] };
  let cur = null, m;
  const clean = (s) => s.trim().replace(/^["']|["']$/g, "");
  for (const raw of String(text || "").split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if ((m = line.match(/^\s*(failures|review):\s*(.*)$/))) {
      cur = m[1];
      const rest = m[2].trim();
      if (rest && rest !== "[]") out[cur].push(...rest.replace(/^\[|\]$/g, "").split(",").map(clean).filter(Boolean));
      continue;
    }
    if (cur && (m = line.match(/^\s*-\s+(.*)$/))) { out[cur].push(clean(m[1])); continue; }
    if (/^\s*[^\s-][^:]*:/.test(line)) cur = null;
  }
  return out;
}

const SENS = { death: "মৃত্যু", crime: "অপরাধ", health: "স্বাস্থ্য", election: "নির্বাচন", religion: "ধর্ম", minors: "শিশু-কিশোর", financial_advice: "আর্থিক পরামর্শ" };
export function reasonLabel(code) {
  const m = /^sensitive:(.+)$/.exec(code);
  return m ? "সংবেদনশীল: " + (SENS[m[1]] || m[1]) : code;
}
