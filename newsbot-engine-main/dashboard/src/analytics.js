// Visitors: Cloudflare Web Analytics (free, cookieless) read through Cloudflare's GraphQL Analytics API.
// Needs the Worker secrets CF_ANALYTICS_TOKEN (permissions: Account Analytics: Read + Account Settings: Read) and CF_ACCOUNT_ID.
// Read only: nothing is changed on Cloudflare and nothing is stored. Never throws for "not configured": the screen shows what is missing.
import * as gh from "./github.js";
import { reader } from "./yamledit.js";
import { HttpError, must } from "./util.js";

const API = "https://api.cloudflare.com/client/v4";
const HOST = /^[a-z0-9.-]+$/;

/** language (or "all") -> host, from config/newsbot.yaml: site.sites, else site.auto_sites, else the one base_url. */
export function hostsOf(yamlText) {
  const rd = reader(yamlText || "");
  const langs = rd(["site", "languages"]) || ["bn", "en"];
  const hostOf = (u) => { try { return new URL(u).hostname.toLowerCase(); } catch { return ""; } };
  const out = {};
  const pre = rd(["site", "auto_sites"]);
  let any = false;
  for (const l of langs) {
    const u = rd(["site", "sites", l, "base_url"]);
    if (u) { out[l] = hostOf(u); any = true; }
  }
  if (!any && pre) { for (const l of langs) out[l] = `${String(pre).toLowerCase()}-${l}.pages.dev`; any = true; }
  if (!any) out.all = hostOf(rd(["site", "base_url"]));
  for (const k of Object.keys(out)) if (!HOST.test(out[k] || "")) delete out[k];
  return out;
}

async function cf(env, f, method, path, body, step = "") {
  const r = await f(API + path, { method, headers: { Authorization: `Bearer ${env.CF_ANALYTICS_TOKEN}`, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  let j = null;
  try { j = await r.json(); } catch { /* not JSON */ }
  if (!r.ok || !j || j.success === false) {
    const why = (j && (j.errors || [])[0] && j.errors[0].message) || `http ${r.status}`;
    throw new HttpError(502, `${step ? step + ": " : ""}Cloudflare said: ${String(why).slice(0, 160)}`);
  }
  return j;
}

const BY_TAG = { decl: "$tags: [string!]", filt: "siteTag_in: $tags", dim: "siteTag" };
const BY_HOST = { decl: "$hosts: [string!]", filt: "requestHost_in: $hosts", dim: "requestHost" };
const query = (m) => `query($acc: String!, ${m.decl}, $since: Time!, $until: Time!) { viewer { accounts(filter: {accountTag: $acc}) {
  daily: rumPageloadEventsAdaptiveGroups(limit: 2000, filter: {${m.filt}, datetime_geq: $since, datetime_leq: $until}, orderBy: [date_ASC]) { count sum { visits } dimensions { date ${m.dim} } }
  pages: rumPageloadEventsAdaptiveGroups(limit: 200, filter: {${m.filt}, datetime_geq: $since, datetime_leq: $until}, orderBy: [count_DESC]) { count dimensions { requestPath ${m.dim} } }
} } }`;

async function stats(env, f, m, vars, key) {
  const g = await cf(env, f, "POST", "/graphql", { query: query(m), variables: vars }, "ভিজিটর সংখ্যা (Account Analytics অনুমতি)");
  if (g.errors && g.errors.length) throw new HttpError(502, `ভিজিটর সংখ্যা (Account Analytics অনুমতি): Cloudflare said: ${String(g.errors[0].message).slice(0, 160)}`);
  return (((g.data || {}).viewer || {}).accounts || [])[0] || {};
}

export async function visitors(env, days = 7, f = (...a) => fetch(...a)) {
  must([1, 7, 30].includes(days), 400, "days: 1, 7 or 30");
  if (!env.CF_ANALYTICS_TOKEN || !env.CF_ACCOUNT_ID) return { configured: false, why: "token" };
  const file = await gh.getFile(env, "config/newsbot.yaml");
  const hosts = hostsOf(file ? file.text : "");
  const acc = env.CF_ACCOUNT_ID;
  const until = new Date(), since = new Date(until.getTime() - days * 86400000);
  const range = { acc, since: since.toISOString(), until: until.toISOString() };
  // 1) preferred: find each site's Web Analytics tag (needs Account Settings: Read). 2) fallback: ask by host name (Account Analytics: Read only).
  let a, keyOf = {}, shown, missing = [];
  let list = null;
  try { list = await cf(env, f, "GET", `/accounts/${acc}/rum/site_info/list?per_page=100`, undefined, "সাইটের তালিকা (Account Settings অনুমতি)"); } catch { list = null; }
  if (list) {
    const tagOfHost = {};
    for (const s of list.result || []) {
      const h = ((s.ruleset && s.ruleset.zone_name) || s.host || "").toLowerCase();
      if (h && s.site_tag) tagOfHost[h] = s.site_tag;
    }
    for (const [lang, host] of Object.entries(hosts)) if (tagOfHost[host]) keyOf[tagOfHost[host]] = lang;
    missing = Object.entries(hosts).filter(([, h]) => !tagOfHost[h]).map(([l]) => l);
    if (!Object.keys(keyOf).length) return { configured: true, days, sites: [], missing, total: { visits: 0, views: 0 } };
    a = await stats(env, f, BY_TAG, { ...range, tags: Object.keys(keyOf) });
    shown = (r) => keyOf[r.dimensions.siteTag];
  } else {
    for (const [lang, host] of Object.entries(hosts)) keyOf[host] = lang;
    a = await stats(env, f, BY_HOST, { ...range, hosts: Object.values(hosts) });
    shown = (r) => keyOf[String(r.dimensions.requestHost || "").toLowerCase()];
  }
  const per = {};
  for (const l of Object.keys(hosts)) per[l] = { lang: l, host: hosts[l], visits: 0, views: 0, daily: {}, pages: [] };
  for (const r of a.daily || []) {
    const s = per[shown(r)]; if (!s) continue;
    const v = (r.sum && r.sum.visits) || 0;
    s.visits += v; s.views += r.count; s.daily[r.dimensions.date] = { visits: v, views: r.count };
  }
  for (const r of a.pages || []) {
    const s = per[shown(r)]; if (!s || s.pages.length >= 8) continue;
    s.pages.push({ path: String(r.dimensions.requestPath || "/").slice(0, 120), views: r.count });
  }
  const sites = Object.values(per).filter((s) => s.views || s.visits || list).map((s) => ({ ...s, daily: Object.entries(s.daily).sort().map(([d, x]) => ({ d, ...x })) }));
  return { configured: true, days, missing, sites, total: { visits: sites.reduce((n, s) => n + s.visits, 0), views: sites.reduce((n, s) => n + s.views, 0) } };
}
