// Drafts (pending/) and live articles (content/): list, open, edit, approve, reject, hide from the site.
import { act, draftRef, applyEdits, split, readField, commit, deploy, listDir, getFile, heads, findDraft } from "./github.js";
import { HttpError, must, isObj, SLUG, LANG } from "./util.js";
import { preview } from "../../worker/index.js";

const PAGE = 15;

const checkLang = (lang) => must(typeof lang === "string" && LANG.test(lang), 400, "bad language");
const checkSlug = (slug) => must(typeof slug === "string" && SLUG.test(slug), 400, "bad article name");

export async function list(env, dir, lang, offset = 0, q = "") {
  checkLang(lang);
  let names = (await listDir(env, `${dir}/${lang}`)).map((f) => f.name).filter((n) => n.endsWith(".md"));
  const total = names.length;
  if (q) names = names.filter((n) => n.toLowerCase().includes(String(q).toLowerCase().slice(0, 60)));
  names.sort();
  const slice = names.slice(offset, offset + PAGE);
  const h = await heads(env, `${dir}/${lang}`, slice);
  return { total, matched: names.length, offset, items: slice.map((n) => ({ slug: n.slice(0, -3), head: h[n] || "" })) };
}

function describe(md) {
  const p = split(md);
  if (!p) throw new HttpError(422, "this file has no front matter");
  const pend = (p.fm + "\n").match(/^pending:[^\n]*\n(?:[ \t]+[^\n]*\n)*/m);
  return { title: readField(p.fm, "title"), summary: readField(p.fm, "summary"), body: p.body.trim(), front: p.fm, pending: pend ? pend[0].replace(/^pending:\n/, "").trimEnd() : "",
    hidden: /^draft:\s*true\s*$/m.test(p.fm), html: preview(p.body) };
}

export async function open(env, dir, lang, slug) {
  checkLang(lang); checkSlug(slug);
  const f = await getFile(env, `${dir}/${lang}/${slug}.md`);
  must(f, 404, "article not found");
  return { lang, slug, version: f.sha, ...describe(f.text) };
}

const editsOf = (e) => {
  must(isObj(e), 400, "nothing to save");
  const title = String(e.title ?? "").trim(), summary = String(e.summary ?? "").trim(), body = String(e.body ?? "");
  must(title && summary && body.trim() && title.length <= 300 && summary.length <= 1000 && body.length <= 80000, 400, "title, summary and text are all required (title up to 300, summary up to 1000 characters)");
  return { title, summary, body };
};

/** action: save | approve | reject  (drafts) */
export async function draftAction(env, body) {
  checkLang(body.lang); checkSlug(body.slug);
  const ref = await draftRef(body.slug);
  const kind = { save: "s", approve: "a", reject: "r" }[body.action];
  must(kind, 400, "unknown action");
  const res = await act(env, kind, body.lang, ref, { edits: kind === "r" ? null : editsOf(body.edits), allowFailures: body.ack === true, version: body.version || null });
  return res;
}

/** action: save | hide | show  (live articles; every change redeploys the site) */
export async function articleAction(env, body) {
  checkLang(body.lang); checkSlug(body.slug);
  const path = `content/${body.lang}/${body.slug}.md`;
  const cur = await getFile(env, path);
  must(cur, 404, "article not found");
  if (body.version && body.version !== cur.sha) throw new HttpError(409, "The article changed meanwhile. Reload and try again.");
  let md = cur.text, what;
  if (body.action === "save") { md = applyEdits(md, editsOf(body.edits)); what = "edit"; }
  else if (body.action === "hide" || body.action === "show") { md = setDraft(md, body.action === "hide"); what = body.action === "hide" ? "hide" : "show"; }
  else throw new HttpError(400, "unknown action");
  if (md === cur.text) return { key: "nochange" };
  await commit(env, `${what}: ${body.slug} (dashboard)`, { [path]: md }, []);
  const deployed = await deploy(env);
  return { key: deployed ? "published" : "publishedNoDeploy" };
}

/** Hugo leaves out a page with `draft: true`: the article stays in content/ (so the engine's state stays right) but disappears from the site. */
export function setDraft(md, hidden) {
  const p = split(md);
  must(p, 422, "this file has no front matter");
  let lines = p.fm.split("\n").filter((l) => !/^draft:/.test(l));
  if (hidden) lines.push("draft: true");
  return "---\n" + lines.join("\n") + "\n---\n" + p.body;
}

export { findDraft };
