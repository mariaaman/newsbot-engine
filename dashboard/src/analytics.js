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

async function cf(env, f, method, path, body) {
  const r = await f(API + path, { method, headers: { Authorization: `Bearer ${env.CF_ANALYTICS_TOKEN}`, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  let j = null;
  try { j = await r.json(); } catch { /* not JSON */ }
  if (!r.ok || !j || j.success === false) {
    const why = (j && (j.errors || [])[0] && j.errors[0].message) || `http ${r.status}`;
    throw new HttpError(502, `Cloudflare said: ${String(why).slice(0, 160)}`);
  }
  return j;
}

const QUERY = `query($acc: String!, $tags: [string!], $since: Time!, $until: Time!) { viewer { accounts(filter: {accountTag: $acc}) {
  daily: rumPageloadEventsAdaptiveGroups(limit: 2000, filter: {siteTag_in: $tags, datetime_geq: $since, datetime_leq: $until}, orderBy: [date_ASC]) { count sum { visits } dimensions { date siteTag } }
  pages: rumPageloadEventsAdaptiveGroups(limit: 200, filter: {siteTag_in: $tags, datetime_geq: $since, datetime_leq: $until}, orderBy: [count_DESC]) { count dimensions { requestPath siteTag } }
} } }`;

export async function visitors(env, days = 7, f = (...a) => fetch(...a)) {
  must([1, 7, 30].includes(days), 400, "days: 1, 7 or 30");
  if (!env.CF_ANALYTICS_TOKEN || !env.CF_ACCOUNT_ID) return { configured: false, why: "token" };
  const file = await gh.getFile(env, "config/newsbot.yaml");
  const hosts = hostsOf(file ? file.text : "");
  const acc = env.CF_ACCOUNT_ID;
  const list = await cf(env, f, "GET", `/accounts/${acc}/rum/site_info/list?per_page=100`);
  const tagOfHost = {};
  for (const s of list.result || []) {
    const h = ((s.ruleset && s.ruleset.zone_name) || s.host || "").toLowerCase();
    if (h && s.site_tag) tagOfHost[h] = s.site_tag;
  }
  const langOfTag = {};
  for (const [lang, host] of Object.entries(hosts)) if (tagOfHost[host]) langOfTag[tagOfHost[host]] = lang;
  const tags = Object.keys(langOfTag);
  const missing = Object.entries(hosts).filter(([, h]) => !tagOfHost[h]).map(([l]) => l);
  if (!tags.length) return { configured: true, days, sites: [], missing, total: { visits: 0, views: 0 } };
  const until = new Date(), since = new Date(until.getTime() - days * 86400000);
  const g = await cf(env, f, "POST", "/graphql", { query: QUERY, variables: { acc, tags, since: since.toISOString(), until: until.toISOString() } });
  if (g.errors && g.errors.length) throw new HttpError(502, `Cloudflare said: ${String(g.errors[0].message).slice(0, 160)}`);
  const a = (((g.data || {}).viewer || {}).accounts || [])[0] || {};
  const per = {};
  for (const l of Object.keys(hosts)) per[l] = { lang: l, host: hosts[l], visits: 0, views: 0, daily: {}, pages: [] };
  for (const r of a.daily || []) {
    const s = per[langOfTag[r.dimensions.siteTag]]; if (!s) continue;
    const v = (r.sum && r.sum.visits) || 0;
    s.visits += v; s.views += r.count; s.daily[r.dimensions.date] = { visits: v, views: r.count };
  }
  for (const r of a.pages || []) {
    const s = per[langOfTag[r.dimensions.siteTag]]; if (!s || s.pages.length >= 8) continue;
    s.pages.push({ path: String(r.dimensions.requestPath || "/").slice(0, 120), views: r.count });
  }
  const sites = Object.values(per).filter((s) => tagOfHost[s.host]).map((s) => ({ ...s, daily: Object.entries(s.daily).sort().map(([d, x]) => ({ d, ...x })) }));
  return { configured: true, days, missing, sites, total: { visits: sites.reduce((n, s) => n + s.visits, 0), views: sites.reduce((n, s) => n + s.views, 0) } };
}
