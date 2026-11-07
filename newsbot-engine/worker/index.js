// NewsBot approve Worker (Cloudflare Workers, free plan). No dependencies. Setup: docs/TELEGRAM_APPROVE.md
//
// 1. Telegram buttons: Approve / Reject under a pending draft  ->  POST /telegram  (webhook)
// 2. A private page per draft: read it, fix the title / summary / text, Save, Approve or Reject  ->  GET|POST /d/<lang>/<ref>?s=<sig>
// Approve = one commit in the brain repo (draft moves from pending/ to content/, `pending:` block removed), then the
// `deploy` workflow publishes the site. Reject = the draft is deleted. Nothing else is ever touched.
//
// Secrets:   GH_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_SECRET   (optional: EDIT_PIN)
// Variables: BRAIN_REPO (owner/newsbot-brain), ENGINE_REPO (owner/newsbot-engine), optional BRAIN_BRANCH, ENGINE_BRANCH

const TG = "https://api.telegram.org";
const GH = "https://api.github.com";
const CALLBACK = /^([ar])\|([a-z]{2,3})\|([0-9a-f]{10})$/;     // a|bn|<sha1(slug)[:10]>  or  r|en|<...>
const PAGE = /^\/d\/([a-z]{2,3})\/([0-9a-f]{10})$/;
const REPO = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const MSG = {
  approved: "✅ অনুমোদিত। ১–৩ মিনিটের মধ্যে সাইটে প্রকাশ হবে।",
  approvedNoDeploy: "✅ অনুমোদিত, কিন্তু ডিপ্লয় চালু করা যায়নি। পরের hourly রানে প্রকাশ হবে।",
  rejected: "🗑 বাতিল করা হয়েছে, খসড়া মুছে গেছে।",
  saved: "💾 সংরক্ষিত হয়েছে। এখনো প্রকাশ হয়নি।",
  gone: "ℹ️ এটি আগেই নিষ্পন্ন হয়েছে (খসড়া আর pending-এ নেই)।",
  gate: "⛔ মান-যাচাইয়ে ব্যর্থ খসড়া অনুমোদন করা যায় না। সম্পাদনা পাতায় গিয়ে নিজে যাচাই করে টিক দিন।",
  stale: "⚠️ খসড়াটি ইতিমধ্যে বদলে গেছে। পাতাটি রিফ্রেশ করে আবার চেষ্টা করুন, কিছু সংরক্ষিত হয়নি।",
  pin: "🔒 PIN ভুল। কিছু বদলায়নি।",
  invalid: "⚠️ শিরোনাম, সারাংশ ও লেখা ফাঁকা রাখা যাবে না (শিরোনাম ৩০০, সারাংশ ১০০০ অক্ষরের মধ্যে)। কিছু সংরক্ষিত হয়নি।",
  error: "❌ ত্রুটি হয়েছে, কিছু বদলায়নি। আবার চেষ্টা করুন বা GitHub-এ দেখুন।",
};

const enc = new TextEncoder();
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

export function safeEqual(a, b) {
  const x = enc.encode(String(a)), y = enc.encode(String(b));
  let d = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) d |= (x[i] || 0) ^ (y[i] || 0);
  return d === 0;
}

export async function draftRef(slug) {
  return hex(await crypto.subtle.digest("SHA-1", enc.encode(slug))).slice(0, 10);
}

/** Signature of a draft link: HMAC-SHA256 over "lang|ref", key derived from the bot token (the engine's notify step signs the same way). */
export async function sign(env, lang, ref) {
  const raw = await crypto.subtle.digest("SHA-256", enc.encode("newsbot-edit|" + env.TELEGRAM_BOT_TOKEN));
  const key = await crypto.subtle.importKey("raw", raw, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(`${lang}|${ref}`)));
}

// ---------------- front matter ----------------

export function split(md) {
  const m = md.match(/^---\n([\s\S]*?)\n---\n/);
  return m ? { fm: m[1], body: md.slice(m[0].length) } : null;
}

const fieldRe = (k) => new RegExp(`^${k}:[^\\n]*(?:\\n[ \\t]+[^\\n]*)*`, "m");

/** Value of a one-key YAML scalar (plain, 'single' or "double" quoted; folded continuation lines joined). null = unsupported style. */
export function readField(fm, k) {
  const m = fm.match(fieldRe(k));
  if (!m) return null;
  const v = m[0].slice(k.length + 1).split("\n").map((s) => s.trim()).join(" ").trim();
  if (!v || /^[>|&*!%@`]/.test(v)) return null;
  if (v.startsWith("'") && v.endsWith("'") && v.length > 1) return v.slice(1, -1).replace(/''/g, "'");
  if (v.startsWith('"') && v.endsWith('"') && v.length > 1) { try { return JSON.parse(v); } catch { return null; } }
  return v;
}

function setField(fm, k, value) {
  const m = fm.match(fieldRe(k));
  return fm.replace(m[0], () => `${k}: ${JSON.stringify(value)}`);          // a JSON string is a valid YAML double-quoted scalar
}

/** Applies the editor's fields to a draft. Only fields that really changed are rewritten; every other line stays byte for byte. */
export function applyEdits(md, e) {
  const p = split(md);
  if (!p) throw new Error("no front matter");
  let fm = p.fm;
  for (const k of ["title", "summary"]) {
    const old = readField(fm, k);
    if (old !== null && e[k] !== old) fm = setField(fm, k, e[k]);
  }
  return "---\n" + fm + "\n---\n" + e.body.replace(/\r\n?/g, "\n").trim() + "\n";
}

/** Removes the `pending:` block. {md} or {refuse}. A draft that failed a quality gate is refused unless the editor was told to allow it. */
export function publishable(md, allowFailures = false) {
  const p = split(md);
  if (!p) return { refuse: "no front matter" };
  const fm = p.fm + "\n";
  const block = fm.match(/^pending:[^\n]*\n(?:[ \t]+[^\n]*\n)*/m);
  if (!block) return { refuse: "no pending block" };
  if (!allowFailures && /^\s*(failures|unsupported):\s*\n\s*- /m.test(block[0])) return { refuse: "gate" };
  const clean = fm.replace(block[0], "").replace(/\n$/, "");
  if (/^pending:/m.test(clean)) return { refuse: "pending block remains" };
  return { md: "---\n" + clean + "\n---\n" + p.body };
}

export const hasGateFailures = (md) => {
  const p = split(md), b = p && (p.fm + "\n").match(/^pending:[^\n]*\n(?:[ \t]+[^\n]*\n)*/m);
  return !!b && /^\s*(failures|unsupported):\s*\n\s*- /m.test(b[0]);
};

// ---------------- GitHub ----------------

export async function gh(env, path, init = {}) {
  const r = await fetch(GH + path, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "newsbot-approve", ...(init.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  const text = await r.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
  return { status: r.status, json };
}

export function must(res, ...ok) {
  if (!ok.includes(res.status)) throw new Error(`github ${res.status}`);
  return res.json;
}

export function b64utf8(b64) {
  const bin = atob(b64.replace(/\n/g, ""));
  return new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
}

const repos = (env) => ({ brain: env.BRAIN_REPO, branch: env.BRAIN_BRANCH || "main", engine: env.ENGINE_REPO, eb: env.ENGINE_BRANCH || "main" });

export async function findDraft(env, lang, ref) {
  const { brain, branch } = repos(env);
  const res = await gh(env, `/repos/${brain}/contents/pending/${lang}?ref=${encodeURIComponent(branch)}`);
  if (res.status === 404) return null;
  const list = must(res, 200);
  for (const f of Array.isArray(list) ? list : []) {
    if (f.type === "file" && f.name.endsWith(".md") && (await draftRef(f.name.slice(0, -3))) === ref) {
      return { slug: f.name.slice(0, -3), path: f.path };
    }
  }
  return null;
}

export async function readDraft(env, draft) {
  const { brain, branch } = repos(env);
  const file = must(await gh(env, `/repos/${brain}/contents/${draft.path}?ref=${encodeURIComponent(branch)}`), 200);
  return { md: b64utf8(file.content), version: file.sha };
}

/** One atomic commit: write `put` (path -> text), delete `del` (paths). Retries when the branch moved meanwhile. */
export async function commit(env, message, put, del) {
  const { brain, branch } = repos(env);
  for (let attempt = 0; attempt < 3; attempt++) {
    const head = must(await gh(env, `/repos/${brain}/git/ref/heads/${encodeURIComponent(branch)}`), 200).object.sha;
    const base = must(await gh(env, `/repos/${brain}/git/commits/${head}`), 200).tree.sha;
    const tree = [];
    for (const [path, text] of Object.entries(put)) {
      const blob = must(await gh(env, `/repos/${brain}/git/blobs`, { method: "POST", body: JSON.stringify({ content: text, encoding: "utf-8" }) }), 201);
      tree.push({ path, mode: "100644", type: "blob", sha: blob.sha });
    }
    for (const path of del) tree.push({ path, mode: "100644", type: "blob", sha: null });
    const t = must(await gh(env, `/repos/${brain}/git/trees`, { method: "POST", body: JSON.stringify({ base_tree: base, tree }) }), 201);
    const c = must(await gh(env, `/repos/${brain}/git/commits`, { method: "POST", body: JSON.stringify({ message, tree: t.sha, parents: [head] }) }), 201);
    const upd = await gh(env, `/repos/${brain}/git/refs/heads/${encodeURIComponent(branch)}`, { method: "PATCH", body: JSON.stringify({ sha: c.sha, force: false }) });
    if (upd.status === 200) return;
    if (upd.status !== 422 && upd.status !== 409) throw new Error(`github ${upd.status}`);   // 422: not a fast-forward, someone pushed
  }
  throw new Error("branch kept moving");
}

export async function deploy(env) {
  const { engine, eb } = repos(env);
  const d = await gh(env, `/repos/${engine}/actions/workflows/deploy.yml/dispatches`, { method: "POST", body: JSON.stringify({ ref: eb }) });
  return d.status === 204;
}

async function tg(env, method, payload) {
  try {
    await fetch(`${TG}/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  } catch { /* best effort; never log the URL (it holds the token) */ }
}

// ---------------- the actions ----------------

/** edits: null (publish as saved) or {title, summary, body}. Returns {key, title?}. Idempotent: a second tap finds no draft. */
export async function act(env, kind, lang, ref, { edits = null, allowFailures = false, version = null } = {}) {
  const { slug, path } = (await findDraft(env, lang, ref)) || {};
  if (!slug) return { key: "gone" };
  if (kind === "r") {
    await commit(env, `reject: ${slug} (telegram)`, {}, [path]);
    return { key: "rejected" };
  }
  const cur = await readDraft(env, { path });
  if (version && version !== cur.version) return { key: "stale" };
  let md = cur.md;
  if (edits) md = applyEdits(md, edits);
  if (kind === "s") {
    await commit(env, `edit: ${slug} (telegram)`, { [path]: md }, []);
    return { key: "saved" };
  }
  const out = publishable(md, allowFailures);
  if (out.refuse === "gate") return { key: "gate" };
  if (out.refuse) throw new Error(out.refuse);
  await commit(env, `approve: ${slug} (telegram)`, { [`content/${lang}/${slug}.md`]: out.md }, [path]);
  return { key: (await deploy(env)) ? "approved" : "approvedNoDeploy" };
}

// ---------------- the draft page ----------------

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");

function inline(t) {
  let s = esc(t);
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" rel="noopener noreferrer nofollow">$1</a>');
  return s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
}

/** A small Markdown preview: headings, paragraphs, lists, quotes, bold / italic, http(s) links. Everything else is shown as text. */
export function preview(md) {
  const out = [];
  let para = [], list = [];
  const flush = () => {
    if (para.length) out.push(`<p>${inline(para.join(" "))}</p>`);
    if (list.length) out.push(`<ul>${list.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`);
    para = []; list = [];
  };
  for (const raw of md.replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    let m;
    if (!line.trim()) flush();
    else if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { flush(); out.push(`<h${m[1].length + 1}>${inline(m[2])}</h${m[1].length + 1}>`); }
    else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { if (para.length) flush(); list.push(m[1]); }
    else if ((m = line.match(/^>\s?(.*)$/))) { flush(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); }
    else { if (list.length) flush(); para.push(line.trim()); }
  }
  flush();
  return out.join("\n");
}

const CSS = `:root{color-scheme:light dark;--bg:#fff;--fg:#1b1b1b;--mut:#666;--card:#f4f4f5;--line:#d4d4d8;--ok:#15803d;--bad:#b91c1c;--acc:#1d4ed8}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--fg:#ececec;--mut:#a1a1aa;--card:#1c1c20;--line:#3f3f46;--ok:#4ade80;--bad:#f87171;--acc:#60a5fa}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.65 system-ui,-apple-system,"Noto Sans Bengali","Segoe UI",sans-serif}
main{max-width:46rem;margin:0 auto;padding:1rem 1rem 6rem}h1{font-size:1.3rem;line-height:1.3}h2,h3,h4,h5{line-height:1.3}
.card{background:var(--card);border:1px solid var(--line);border-radius:.6rem;padding:.8rem 1rem;margin:1rem 0}.mut{color:var(--mut);font-size:.9rem}
label{display:block;font-weight:600;margin:1rem 0 .3rem}input[type=text],input[type=password],textarea{width:100%;font:inherit;padding:.6rem;border:1px solid var(--line);border-radius:.5rem;background:var(--bg);color:var(--fg)}
textarea{min-height:22rem;line-height:1.5}.bar{position:sticky;bottom:0;background:var(--bg);border-top:1px solid var(--line);padding:.6rem 0;display:flex;gap:.5rem;flex-wrap:wrap}
button{font:inherit;font-weight:600;padding:.7rem 1.1rem;border-radius:.5rem;border:1px solid var(--line);background:var(--card);color:var(--fg)}
.ok{background:var(--ok);color:#fff;border-color:var(--ok)}.bad{color:var(--bad);border-color:var(--bad)}pre{white-space:pre-wrap;word-break:break-word;font-size:.8rem}
blockquote{margin:1rem 0;padding-left:1rem;border-left:3px solid var(--line);color:var(--mut)}details{margin:.6rem 0}summary{cursor:pointer;font-weight:600}.warn{border-color:var(--bad)}`;

function html(status, body, title = "NewsBot") {
  return new Response(`<!doctype html><html lang="bn"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>${esc(title)}</title><style>${CSS}</style></head><body><main>${body}</main></body></html>`, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow",
      "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    },
  });
}

const notice = (text, ok = true) => `<div class="card${ok ? "" : " warn"}"><strong>${esc(text)}</strong></div>`;

function editorPage(env, lang, ref, sig, cur, message = "", bad = false) {
  const p = split(cur.md), title = readField(p.fm, "title"), summary = readField(p.fm, "summary");
  const pend = (p.fm + "\n").match(/^pending:[^\n]*\n(?:[ \t]+[^\n]*\n)*/m);
  const failed = hasGateFailures(cur.md);
  const body = `${message ? notice(message, !bad) : ""}
<p class="mut">খসড়া · ভাষা: ${esc(lang)} · এখনো প্রকাশ হয়নি</p>
<h1>${esc(title ?? "(শিরোনাম পড়া যায়নি)")}</h1>
<p><em>${esc(summary ?? "")}</em></p>
${pend ? `<div class="card${failed ? " warn" : ""}"><strong>আটকে আছে কারণ:</strong><pre>${esc(pend[0].replace(/^pending:\n/, "").trimEnd())}</pre></div>` : ""}
<div class="card">${preview(p.body)}</div>
<details><summary>ফ্যাক্ট-শিট ও সূত্র (শুধু দেখার জন্য)</summary><pre>${esc(p.fm)}</pre></details>
<form method="post" action="/d/${lang}/${ref}?s=${sig}">
<h2>সম্পাদনা</h2>
<input type="hidden" name="v" value="${esc(cur.version)}">
<label for="t">শিরোনাম</label><input id="t" type="text" name="title" value="${esc(title ?? "")}" ${title === null ? "readonly" : ""}>
<label for="s">সারাংশ</label><textarea id="s" name="summary" style="min-height:6rem" ${summary === null ? "readonly" : ""}>${esc(summary ?? "")}</textarea>
<label for="b">লেখা (Markdown)</label><textarea id="b" name="body">${esc(p.body.trim())}</textarea>
${failed ? `<label><input type="checkbox" name="ack" value="1"> মান-যাচাইয়ে ব্যর্থ ছিল, আমি নিজে সব নাম, সংখ্যা ও উদ্ধৃতি যাচাই করেছি</label>` : ""}
${env.EDIT_PIN ? `<label for="p">PIN</label><input id="p" type="password" name="pin" autocomplete="off" inputmode="numeric">` : ""}
<div class="bar"><button class="ok" name="action" value="approve">✅ অনুমোদন ও প্রকাশ</button><button name="action" value="save">💾 সংরক্ষণ</button><button class="bad" name="action" value="reject">🗑 বাতিল</button></div>
<p class="mut">“অনুমোদন” চাপলে ফর্মের বর্তমান লেখাই সংরক্ষণ হয়ে প্রকাশ হবে।</p>
</form>`;
  return html(200, body, title || "NewsBot");
}

async function pageRequest(request, env, lang, ref) {
  const url = new URL(request.url), sig = url.searchParams.get("s") || "";
  if (!env.TELEGRAM_BOT_TOKEN || !safeEqual(sig, await sign(env, lang, ref))) return html(403, notice("লিংকটি বৈধ নয়।", false));
  const r = repos(env);
  if (!REPO.test(r.brain || "") || !REPO.test(r.engine || "")) return html(500, notice("Worker-এর BRAIN_REPO / ENGINE_REPO ঠিক নেই।", false));
  try {
    const draft = await findDraft(env, lang, ref);
    if (!draft) return html(200, notice(MSG.gone));
    if (request.method === "GET") return editorPage(env, lang, ref, sig, await readDraft(env, draft));
    const f = await request.formData();
    const get = (k) => String(f.get(k) ?? "");
    const action = get("action");
    if (env.EDIT_PIN && !safeEqual(get("pin"), env.EDIT_PIN)) return editorPage(env, lang, ref, sig, await readDraft(env, draft), MSG.pin, true);
    if (action === "reject") {
      const res = await act(env, "r", lang, ref);
      await tg(env, "sendMessage", { chat_id: env.TELEGRAM_CHAT_ID, text: `🗑 বাতিল: ${draft.slug}` });
      return html(200, notice(MSG[res.key]));
    }
    if (action !== "save" && action !== "approve") return html(400, notice("অজানা কাজ।", false));
    const edits = { title: get("title").trim(), summary: get("summary").trim(), body: get("body") };
    if (!edits.title || !edits.summary || !edits.body.trim() || edits.title.length > 300 || edits.summary.length > 1000 || edits.body.length > 80000) {
      return editorPage(env, lang, ref, sig, await readDraft(env, draft), MSG.invalid, true);
    }
    const res = await act(env, action === "save" ? "s" : "a", lang, ref, { edits, allowFailures: get("ack") === "1", version: get("v") });
    if (res.key === "approved" || res.key === "approvedNoDeploy") {
      await tg(env, "sendMessage", { chat_id: env.TELEGRAM_CHAT_ID, text: `✅ অনুমোদিত: ${edits.title}` });
      return html(200, notice(MSG[res.key]));
    }
    if (res.key === "gone") return html(200, notice(MSG.gone));
    const fresh = await findDraft(env, lang, ref);
    if (!fresh) return html(200, notice(MSG[res.key]));
    return editorPage(env, lang, ref, sig, await readDraft(env, fresh), MSG[res.key], res.key !== "saved");
  } catch {
    return html(500, notice(MSG.error, false));
  }
}

// ---------------- entry point ----------------

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const pm = PAGE.exec(url.pathname);
    if (pm && (request.method === "GET" || request.method === "POST")) return pageRequest(request, env, pm[1], pm[2]);
    if (request.method !== "POST" || url.pathname !== "/telegram") return new Response("ok");
    if (!env.WEBHOOK_SECRET || !safeEqual(request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "", env.WEBHOOK_SECRET)) {
      return new Response("unauthorized", { status: 401 });
    }
    let update;
    try { update = await request.json(); } catch { return new Response("ok"); }
    const cq = update && update.callback_query;
    if (!cq) return new Response("ok");
    const owner = String(env.TELEGRAM_CHAT_ID);
    if (String(cq.from && cq.from.id) !== owner || String(cq.message && cq.message.chat && cq.message.chat.id) !== owner) {
      return new Response("ok");                                   // not the owner: do nothing, say nothing
    }
    const answer = (text) => tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text, show_alert: false });
    const m = CALLBACK.exec(String(cq.data || ""));
    if (!m) { await answer(""); return new Response("ok"); }
    const r = repos(env);
    if (!REPO.test(r.brain || "") || !REPO.test(r.engine || "")) { await answer(MSG.error); return new Response("ok"); }
    let key;
    try { key = (await act(env, m[1], m[2], m[3])).key; } catch { key = "error"; }
    await answer(MSG[key].slice(0, 190));
    if (key !== "error" && cq.message) {
      await tg(env, "editMessageReplyMarkup", {
        chat_id: cq.message.chat.id, message_id: cq.message.message_id,
        reply_markup: { inline_keyboard: [[{ text: MSG[key].slice(0, 60), callback_data: "n" }]] },
      });
    }
    return new Response("ok");
  },
};
