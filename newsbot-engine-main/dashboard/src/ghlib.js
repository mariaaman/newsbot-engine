// GitHub helpers for the dashboard (moved here from the removed Telegram approve Worker): REST calls, one-commit writes,
// the deploy trigger, front-matter editing and a small Markdown preview. No dependencies.

const GH = "https://api.github.com";

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

// ---------------- GitHub ----------------

export async function gh(env, path, init = {}) {
  const r = await fetch(GH + path, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "newsbot-dashboard", ...(init.body ? { "Content-Type": "application/json" } : {}),
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

