// NewsBot Control: the whole dashboard UI. Talks only to this Worker's /api/*; secrets are write-only; the brain repo on GitHub is the database.
import {
  h, $, flat, api, guard, toast, errorCard, ApiError, LANGS, DEFAULT_LANG, REQUIRED_SECRETS, RAW_FILES, WORKFLOW_LABELS, MSG,
  fmtTime, ago, num, runChip, titleOf, pendingOf, parsePending, reasonLabel,
} from "./lib.js";

const view = $("view");
const nav = $("nav");
const S = { lang: DEFAULT_LANG, q: "", offset: 0 };          // list state, kept while you move between screens
const PAGE = 15;                                               // the server's page size
const enc = encodeURIComponent;
const safeUrl = (u) => (/^https:\/\//.test(u || "") ? u : null);
const NEWS = "config/newsbot.yaml", LLM = "config/llm.yaml";

// ---------- navigation ----------
const TABS = [["", "🏠", "হোম"], ["drafts", "📝", "খসড়া"], ["articles", "📰", "আর্টিকেল"], ["settings", "⚙️", "সেটিংস"], ["keys", "🔑", "কী"], ["runs", "▶️", "রান"], ["more", "☰", "আরও"]];
const tabEls = TABS.map(([id, icon, label]) => h("a", { href: "#/" + id, class: "tab" }, h("b", {}, icon), label));
nav.replaceChildren(...tabEls);
const setNav = (name) => tabEls.forEach((el, i) => { el.className = "tab" + (TABS[i][0] === name ? " on" : ""); });

let token = 0;
async function route() {
  const my = ++token;
  const parts = (location.hash || "#/").replace(/^#\/?/, "").split("/").map((p) => { try { return decodeURIComponent(p); } catch { return p; } });
  const name = Object.prototype.hasOwnProperty.call(SCREENS, parts[0]) ? parts[0] : "";
  setNav(name === "draft" ? "drafts" : name === "article" ? "articles" : name);
  view.replaceChildren(h("p", { class: "muted" }, "লোড হচ্ছে…"));
  try {
    const out = await SCREENS[name](parts.slice(1));
    if (my !== token) return;                                  // a newer screen was asked for meanwhile
    view.replaceChildren(...flat(out));
    window.scrollTo(0, 0);
  } catch (e) {
    if (my === token) view.replaceChildren(errorCard(e));
  }
}
const goto = (hash) => { if (location.hash === hash) return route(); location.hash = hash; return Promise.resolve(); };

const select = (options, value, onchange) => {
  const s = h("select", { onchange }, options.map((o) => { const [v, l] = Array.isArray(o) ? o : [o, o]; return h("option", { value: v }, l); }));
  s.value = value ?? "";
  return s;
};
const lbl = (text) => h("label", { class: "lbl" }, text);

// ---------- home ----------
async function home() {
  const o = await api("/api/overview");
  const sum = (m) => Object.values(m || {}).reduce((a, b) => a + b, 0);
  const perLang = (m) => LANGS.map(([c]) => `${c} ${(m || {})[c] || 0}`).join(" · ");
  const published = sum(o.counts.published), pending = sum(o.counts.pending);
  const out = [];

  // site
  const chk = o.site.check;
  out.push(h("div", { class: "card" },
    h("h2", {}, o.site.name || "সাইট"),
    safeUrl(o.site.url) ? h("a", { href: o.site.url, target: "_blank", rel: "noopener noreferrer" }, o.site.url) : h("span", { class: "muted" }, "সাইটের ঠিকানা সেট করা নেই (সেটিংস, সাইট)"),
    h("div", {},
      !chk ? null : chk.status >= 200 && chk.status < 400 ? h("span", { class: "chip ok" }, `চালু · ${chk.status} · ${chk.ms} ms`)
        : chk.status === 0 ? h("span", { class: "chip bad" }, "সাইটে পৌঁছানো যাচ্ছে না") : h("span", { class: "chip warn" }, `সাইট ${chk.status} দিচ্ছে`),
      o.site.deployed ? h("span", { class: "chip" }, `শেষ ডিপ্লয় ${ago(o.site.deployed)}`) : h("span", { class: "chip warn" }, "এখনো ডিপ্লয় হয়নি"))));

  // mode
  const modeBtns = ["review", "auto"].map((m) => {
    const b = h("button", { class: "btn small" + (o.mode === m ? " primary" : ""), disabled: o.mode === m, onclick: () => guard(b, () => setMode(m)) }, m);
    return b;
  });
  out.push(h("div", { class: "card" },
    h("div", { class: "row between" }, h("h2", {}, "প্রকাশের মোড"), h("span", { class: "chip " + (o.mode === "auto" ? "warn" : "ok") }, o.mode)),
    h("p", { class: "muted small" }, "review: আপনি দেখে অনুমোদন দেন। auto: নিজে প্রকাশ হয় (সংবেদনশীল খসড়া তবুও আটকে থাকে)।"),
    h("div", { class: "row" }, modeBtns, h("span", { class: "chip " + (o.ads ? "ok" : "") }, o.ads ? "বিজ্ঞাপন চালু" : "বিজ্ঞাপন বন্ধ"))));

  // counts
  out.push(h("div", { class: "row" },
    h("a", { href: "#/articles", class: "card item grow" }, h("div", { class: "stat" }, `প্রকাশিত ${published}`), h("div", { class: "muted small" }, perLang(o.counts.published))),
    h("a", { href: "#/drafts", class: "card item grow" }, h("div", { class: "stat" }, `খসড়া ${pending}`), h("div", { class: "muted small" }, perLang(o.counts.pending)))));

  // run now
  const runBtn = h("button", { class: "btn primary", onclick: () => guard(runBtn, async () => { await api("/api/dispatch", { workflow: "hourly" }); toast("▶️ রান শুরু হয়েছে। রান ট্যাবে দেখুন।", "ok"); }) }, "এখনই চালান");
  const lh = o.lastHourly, ld = o.lastDeploy;
  out.push(h("div", { class: "card" },
    h("h2", {}, "রান"),
    h("div", {}, "শেষ hourly: ", runChip(lh), lh ? h("span", { class: "muted small" }, ` ${ago(lh.created)}`) : null),
    h("div", {}, "শেষ deploy: ", runChip(ld), ld ? h("span", { class: "muted small" }, ` ${ago(ld.created)}`) : null),
    o.cooldowns ? h("p", { class: "muted small" }, `বিরতিতে থাকা স্টোরি: ${o.cooldowns}`) : null,
    runBtn));

  // usage
  const counters = Object.entries(o.usage.counters || {});
  out.push(h("div", { class: "card" },
    h("h2", {}, "আজকের মডেল-ব্যবহার"), o.usage.day ? h("p", { class: "muted small" }, o.usage.day) : null,
    counters.length ? counters.map(([k, v]) => h("div", { class: "list-line" }, h("span", { class: "mono" }, k), ` — ${num(v.requests)} রিকোয়েস্ট · ${num(v.tokens)} টোকেন`)) : h("p", { class: "muted" }, "আজ এখনো কোনো ব্যবহার নেই।")));

  // secrets
  if (!o.secrets) {
    out.push(h("div", { class: "card warn" }, "সিক্রেটের তালিকা পড়া যায়নি (GitHub টোকেনে Secrets পড়ার অনুমতি লাগে)।"));
  } else {
    const missing = REQUIRED_SECRETS.filter((n) => !o.secrets.includes(n));
    out.push(h("div", { class: "card " + (missing.length ? "warn" : "ok") },
      missing.length ? h("div", {}, h("h2", {}, "অনুপস্থিত সিক্রেট"), missing.map((n) => h("span", { class: "chip bad" }, n)), h("p", {}, h("a", { href: "#/keys" }, "কী ট্যাবে গিয়ে বসান")))
        : h("div", {}, h("h2", {}, "সিক্রেট"), h("span", { class: "chip ok" }, "সব দরকারি সিক্রেট আছে")),
      h("p", { class: "muted small" }, "আছে: "), o.secrets.map((n) => h("span", { class: "chip" }, n))));
  }
  return out;
}

async function setMode(mode) {
  if (mode === "auto" && !confirm("auto মোডে সংবেদনশীল ছাড়া বাকি লেখা আপনার অনুমোদন ছাড়াই প্রকাশ হবে। চালু করবেন?")) return;
  await api("/api/settings", { changes: [{ file: NEWS, kind: "field", path: ["publish", "mode"], value: mode }] });
  toast(`✅ মোড এখন ${mode}`, "ok");
  await route();
}

// ---------- lists ----------
const langTabs = () => h("div", { class: "row" }, LANGS.map(([code, label]) =>
  h("button", { class: "btn small" + (S.lang === code ? " primary" : ""), onclick: () => { S.lang = code; S.offset = 0; S.q = ""; route(); } }, label)));

async function listScreen(kind) {
  const d = await api(`/api/${kind}?lang=${enc(S.lang)}&offset=${S.offset}&q=${enc(S.q)}`);
  const isDraft = kind === "drafts";
  const q = h("input", { type: "search", value: S.q, placeholder: "নামের অংশ লিখে খুঁজুন", maxLength: 60 });
  const form = h("form", { class: "row", onsubmit: (ev) => { ev.preventDefault(); S.q = q.value.trim(); S.offset = 0; route(); } },
    h("div", { class: "grow" }, q), h("button", { class: "btn", type: "submit" }, "খুঁজুন"));
  const items = d.items.map((it) => {
    const head = it.head || "", p = parsePending(pendingOf(head));
    return h("a", { class: "card item", href: `#/${isDraft ? "draft" : "article"}/${enc(S.lang)}/${enc(it.slug)}` },
      h("div", { class: "t" }, titleOf(head)),
      h("div", {},
        p.failures.length ? h("span", { class: "chip bad" }, `⛔ মান-যাচাই ${p.failures.length}`) : null,
        p.review.map((r) => h("span", { class: "chip warn" }, reasonLabel(r))),
        !isDraft && /^draft:\s*true\s*$/m.test(head) ? h("span", { class: "chip warn" }, "সাইটে লুকানো") : null,
        h("span", { class: "muted small mono" }, it.slug)));
  });
  const prev = h("button", { class: "btn small", disabled: S.offset <= 0, onclick: () => { S.offset = Math.max(0, S.offset - PAGE); route(); } }, "← আগের");
  const next = h("button", { class: "btn small", disabled: S.offset + PAGE >= d.matched, onclick: () => { S.offset += PAGE; route(); } }, "পরের →");
  return [
    h("h1", {}, isDraft ? "খসড়া" : "প্রকাশিত আর্টিকেল"), langTabs(),
    h("p", { class: "muted small" }, `মোট ${d.total} · মিলেছে ${d.matched}${S.q ? ` · খোঁজা: ${S.q}` : ""}`), form,
    items.length ? items : h("div", { class: "card muted" }, isDraft ? "কোনো খসড়া নেই। 🎉" : "এই ভাষায় কোনো প্রকাশিত আর্টিকেল নেই।"),
    d.matched > PAGE ? h("div", { class: "row between" }, prev, next) : null,
  ];
}
const drafts = () => listScreen("drafts");
const articles = () => listScreen("articles");

// ---------- one draft / one live article ----------
async function editor(kind, [lang, slug]) {
  const isDraft = kind === "draft";
  S.lang = lang || S.lang;
  const d = await api(`/api/${kind}?lang=${enc(lang)}&slug=${enc(slug)}`);
  const pend = parsePending(d.pending);
  const title = h("input", { type: "text", value: d.title, maxLength: 300 });
  const summary = h("textarea", { rows: 3, value: d.summary });
  const body = h("textarea", { class: "edit-body", rows: 16, value: d.body });
  const edits = () => ({ title: title.value, summary: summary.value, body: body.value });
  const ack = isDraft && pend.failures.length ? h("input", { type: "checkbox" }) : null;
  const pv = h("div", { class: "preview" });
  pv.innerHTML = d.html;                                       // the Worker escapes the article text before building this

  const act = async (action, extra = {}) => api(`/api/${kind}`, { action, lang, slug, version: d.version, ...extra });
  const after = (res, backHash) => {
    toast(MSG[res.key] || "✅ হয়েছে", res.key === "gate" || res.key === "stale" ? "bad" : "ok");
    if (res.key === "gate") return null;
    if (res.key === "stale") return route();
    return backHash ? goto(backHash) : route();
  };

  const buttons = [];
  if (isDraft) {
    const save = h("button", { class: "btn", onclick: () => guard(save, async () => after(await act("save", { edits: edits() })), null) }, "সংরক্ষণ");
    const approve = h("button", { class: "btn primary", onclick: () => guard(approve, async () => {
      if (pend.review.length && !confirm("এটি সংবেদনশীল হিসেবে চিহ্নিত। পড়ে দেখেছেন, প্রকাশ করবেন?")) return;
      after(await act("approve", { edits: edits(), ack: ack ? ack.checked : false }), "#/drafts");
    }) }, "অনুমোদন ও প্রকাশ");
    const reject = h("button", { class: "btn danger", onclick: () => guard(reject, async () => {
      if (!confirm("খসড়াটি মুছে ফেলবেন? এটা ফেরানো যাবে না।")) return;
      after(await api("/api/draft", { action: "reject", lang, slug }), "#/drafts");
    }) }, "বাতিল");
    buttons.push(save, approve, reject);
  } else {
    const save = h("button", { class: "btn primary", onclick: () => guard(save, async () => {
      if (!confirm("সংরক্ষণ করলে সাইট আবার ডিপ্লয় হবে। এগিয়ে যাবেন?")) return;
      after(await act("save", { edits: edits() }));
    }) }, "সংরক্ষণ ও প্রকাশ");
    const vis = h("button", { class: "btn", onclick: () => guard(vis, async () => {
      if (!d.hidden && !confirm("আর্টিকেলটি সাইট থেকে লুকাবেন? (ফাইল মুছবে না)")) return;
      after(await act(d.hidden ? "show" : "hide"));
    }) }, d.hidden ? "আবার দেখান" : "সাইট থেকে লুকান");
    buttons.push(save, vis);
  }

  return [
    h("a", { href: isDraft ? "#/drafts" : "#/articles" }, isDraft ? "← খসড়ার তালিকা" : "← আর্টিকেলের তালিকা"),
    h("h1", {}, d.title || slug),
    h("div", {}, h("span", { class: "chip" }, lang), d.hidden ? h("span", { class: "chip warn" }, "সাইটে লুকানো") : null,
      pend.review.map((r) => h("span", { class: "chip warn" }, reasonLabel(r))), pend.failures.map((f) => h("span", { class: "chip bad" }, "⛔ " + f))),
    pend.failures.length ? h("div", { class: "card warn" }, "মান-যাচাইয়ে আটকেছে। ঠিক করে নিন, নয়তো নিজে দেখে নিচের টিক দিয়ে প্রকাশ করুন।") : null,
    h("div", { class: "card" }, lbl("শিরোনাম"), title, lbl("সারাংশ"), summary, lbl("লেখা (Markdown)"), body,
      ack ? h("label", { class: "check" }, ack, h("span", {}, "আমি নিজে যাচাই করেছি; মান-যাচাইয়ে ব্যর্থ হলেও প্রকাশ করুন")) : null,
      h("div", { class: "row" }, buttons)),
    h("div", { class: "card" }, h("h2", {}, "প্রিভিউ"), h("p", { class: "muted small" }, "সংরক্ষিত লেখার প্রিভিউ। সম্পাদনার পর সংরক্ষণ দিলে হালনাগাদ হবে।"), pv),
    h("details", { class: "card alt" }, h("summary", {}, "ফ্রন্ট-ম্যাটার (শুধু দেখার জন্য)"), h("pre", {}, d.front || "")),
  ];
}
const draftScreen = (p) => editor("draft", p);
const articleScreen = (p) => editor("article", p);

// ---------- settings ----------
async function settings() {
  const d = await api("/api/settings");
  const changes = new Map();
  const barText = h("span", {}, "");
  const saveBtn = h("button", { class: "btn primary", onclick: () => guard(saveBtn, saveAll) }, "সংরক্ষণ");
  const dropBtn = h("button", { class: "btn", onclick: () => route() }, "বাতিল");
  const bar = h("div", { class: "bar", hidden: true }, barText, h("div", { class: "row" }, dropBtn, saveBtn));
  const refresh = () => { bar.hidden = changes.size === 0; barText.textContent = `${changes.size}টি বদল বাকি`; };

  async function saveAll() {
    const list = [...changes.values()], versions = {};
    for (const c of list) versions[c.file] = d.files[c.file] && d.files[c.file].sha;
    const r = await api("/api/settings", { changes: list, versions });
    toast(r.results.some((x) => x.changed) ? "✅ সংরক্ষিত। কনফিগ যাচাই (validate-config) চলছে, ফল রান ট্যাবে।" : "ℹ️ কিছু বদলায়নি।", "ok");
    await route();
  }
  async function applyNow(change, okMsg) {
    await api("/api/settings", { changes: [{ file: LLM, ...change }], versions: { [LLM]: d.files[LLM] && d.files[LLM].sha } });
    toast(okMsg || "✅ সংরক্ষিত। কনফিগ যাচাই চলছে।", "ok");
    await route();
  }

  function fieldRow(f) {
    const key = f.file + "|" + f.path.join(".");
    const box = h("div", { class: "field" });
    const err = h("div", { class: "err", hidden: true }, "");
    const mark = (value, same) => {
      if (value === undefined || same) changes.delete(key); else changes.set(key, { file: f.file, kind: "field", path: f.path, value });
      box.className = changes.has(key) ? "field changed" : "field";
      refresh();
    };
    const hint = f.help ? h("div", { class: "help" }, f.help) : null;
    const dflt = f.present ? "" : "ডিফল্ট";
    let control;
    if (f.type === "bool") {
      const cb = h("input", { type: "checkbox", checked: f.value === true, onchange: () => mark(cb.checked, cb.checked === (f.value === true)) });
      box.append(...flat(h("label", { class: "check" }, cb, h("span", { class: "name" }, f.label)), hint));
    } else {
      if (f.type === "int" || f.type === "float") {
        control = h("input", { type: "number", step: f.type === "int" ? "1" : "any", min: f.min, max: f.max, value: f.value ?? "", placeholder: dflt, inputMode: "decimal",
          oninput: () => {
            const raw = control.value.trim();
            if (raw === "") { err.hidden = true; return mark(undefined); }
            const n = Number(raw);
            err.hidden = Number.isFinite(n); err.textContent = Number.isFinite(n) ? "" : "সংখ্যা লিখুন";
            mark(Number.isFinite(n) ? n : undefined, n === f.value);
          } });
      } else if (f.type === "enum") {
        control = select(f.present ? f.options : [["", "(ডিফল্ট)"], ...f.options], f.value, () => mark(control.value === "" ? undefined : control.value, control.value === f.value));
      } else if (f.type === "list") {
        const orig = JSON.stringify(f.value || []);
        control = h("input", { type: "text", value: (f.value || []).join(", "), placeholder: dflt,
          oninput: () => { const items = control.value.split(",").map((s) => s.trim()).filter(Boolean); mark(items, JSON.stringify(items) === orig); } });
      } else {
        const same = () => control.value.trim() === (f.value ?? "");
        const ev = { placeholder: dflt, value: f.value ?? "", oninput: () => mark(control.value.trim(), same()) };
        control = f.multiline ? h("textarea", { rows: 3, ...ev }) : h("input", { type: "text", maxLength: f.maxLen, ...ev });
      }
      box.append(...flat(h("div", { class: "name" }, f.label), hint, control));
    }
    box.append(err);
    return box;
  }

  const groups = d.groups.map(([id, label], i) => {
    const fs = d.fields.filter((f) => f.group === id);
    return h("details", { class: "grp", open: i === 0 }, h("summary", {}, `${label} (${fs.length})`), h("div", { class: "grp-body" }, fs.map(fieldRow)));
  });

  // model chains
  const chainBox = d.tiers.map((tier) => {
    const list = d.chains[tier] || [];
    const rows = list.map((e, i) => {
      const prov = select(d.providers, e.provider), model = h("input", { type: "text", value: e.model });
      const ok = h("button", { class: "btn small", onclick: () => guard(ok, () => applyNow({ kind: "chain", op: "set", tier, index: i, provider: prov.value, model: model.value.trim() }, "✅ মডেল বদলেছে")) }, "সংরক্ষণ");
      const rm = h("button", { class: "btn small danger", onclick: () => guard(rm, async () => {
        if (list.length < 2) { toast("চেইনে অন্তত একটা মডেল থাকতে হবে", "bad"); return; }
        if (confirm("এই মডেলটি চেইন থেকে সরাবেন?")) await applyNow({ kind: "chain", op: "remove", tier, index: i }, "✅ সরানো হয়েছে");
      }) }, "সরান");
      return h("div", { class: "list-line" }, h("div", { class: "row" }, h("span", { class: "chip" }, `#${i + 1}`), h("div", { class: "grow" }, prov), h("div", { class: "grow" }, model), ok, rm));
    });
    const np = select(d.providers, d.providers[0]), nm = h("input", { type: "text", placeholder: "নতুন মডেল আইডি" });
    const add = h("button", { class: "btn small", onclick: () => guard(add, () => applyNow({ kind: "chain", op: "insert", tier, index: list.length, provider: np.value, model: nm.value.trim() }, "✅ যোগ হয়েছে")) }, "যোগ করুন");
    return h("div", { class: "stack" }, h("h3", {}, `স্তর: ${tier} (ওপর থেকে নিচে চেষ্টা হয়)`), rows, h("div", { class: "row" }, h("div", { class: "grow" }, np), h("div", { class: "grow" }, nm), add));
  });

  // daily budgets
  const budgetBox = ["tokens_per_day", "requests_per_day"].map((map) => {
    const entries = Object.entries(d.budget[map] || {});
    const rows = entries.map(([k, v]) => {
      const inp = h("input", { type: "number", step: "1", min: "0", value: v });
      const ok = h("button", { class: "btn small", onclick: () => guard(ok, () => applyNow({ kind: "budget", map, key: k, value: Number(inp.value) }, "✅ সীমা বদলেছে")) }, "সংরক্ষণ");
      const rm = h("button", { class: "btn small danger", onclick: () => guard(rm, async () => { if (confirm(`${k} এর সীমা মুছবেন?`)) await applyNow({ kind: "budget", map, key: k, op: "remove" }, "✅ সরানো হয়েছে"); }) }, "সরান");
      return h("div", { class: "list-line" }, h("div", { class: "mono" }, k), h("div", { class: "row" }, h("div", { class: "grow" }, inp), ok, rm));
    });
    const nk = h("input", { type: "text", placeholder: "provider/model" }), nv = h("input", { type: "number", step: "1", min: "0", placeholder: "সীমা" });
    const add = h("button", { class: "btn small", onclick: () => guard(add, () => applyNow({ kind: "budget", map, key: nk.value.trim(), value: Number(nv.value) }, "✅ যোগ হয়েছে")) }, "যোগ করুন");
    return h("div", { class: "stack" }, h("h3", {}, map === "tokens_per_day" ? "দৈনিক টোকেন সীমা" : "দৈনিক রিকোয়েস্ট সীমা"), rows, h("div", { class: "row" }, h("div", { class: "grow" }, nk), h("div", { class: "grow" }, nv), add));
  });

  // task -> tier, embedding
  const taskBox = d.taskNames.map((t) => {
    const s = select(d.tiers, d.tasks[t], () => guard(s, () => applyNow({ kind: "task", task: t, tier: s.value }, `✅ ${t} এখন ${s.value}`)));
    return h("div", { class: "list-line" }, h("div", { class: "row between" }, h("span", { class: "mono" }, t), h("div", { class: "grow" }, s)));
  });
  const ep = select(d.providers, d.embedding.provider), em = h("input", { type: "text", value: d.embedding.model || "" });
  const eok = h("button", { class: "btn small", onclick: () => guard(eok, () => applyNow({ kind: "embedding", provider: ep.value, model: em.value.trim() }, "✅ এমবেডিং বদলেছে")) }, "সংরক্ষণ");

  const llmSection = h("details", { class: "grp" }, h("summary", {}, "মডেল চেইন, বাজেট, কাজ ও এমবেডিং"),
    h("div", { class: "grp-body stack" }, chainBox, budgetBox, h("h3", {}, "কোন কাজ কোন স্তরে"), taskBox,
      h("h3", {}, "এমবেডিং"), h("div", { class: "row" }, h("div", { class: "grow" }, ep), h("div", { class: "grow" }, em), eok)));

  // raw files + history
  const rawBox = h("div", { class: "stack" });
  const pick = select(RAW_FILES, RAW_FILES[0]);
  async function openFile(path) {
    const r = await api(`/api/file?path=${enc(path)}`);
    const ta = h("textarea", { class: "edit-body mono", rows: 18, value: r.text });
    const write = async (allowStructure) => {
      try { await api("/api/file", { path, text: ta.value, sha: r.sha, ...(allowStructure ? { allowStructure: true } : {}) }); }
      catch (e) {
        if (e instanceof ApiError && e.status === 422 && !allowStructure && confirm(e.message + "\n\nতবুও সংরক্ষণ করবেন?")) return write(true);
        throw e;
      }
      toast("✅ সংরক্ষিত" + (path.endsWith(".yaml") ? "। কনফিগ যাচাই চলছে।" : ""), "ok");
      await openFile(path);
    };
    const ok = h("button", { class: "btn primary", onclick: () => guard(ok, () => write(false)) }, "ফাইল সংরক্ষণ");
    const hist = (r.history || []).map((c) => {
      const back = h("button", { class: "btn small", onclick: () => guard(back, async () => {
        if (!confirm(`${path} কে ${c.sha.slice(0, 7)} সংস্করণে ফেরাবেন?`)) return;
        await api("/api/restore", { path, ref: c.sha }); toast("✅ ফিরিয়ে আনা হয়েছে", "ok"); await openFile(path);
      }) }, "ফিরিয়ে আনুন");
      return h("div", { class: "list-line" }, h("div", {}, c.message || "(বার্তা নেই)"), h("div", { class: "muted small" }, `${c.sha.slice(0, 7)} · ${fmtTime(c.date)} · ${c.author || ""}`), back);
    });
    rawBox.replaceChildren(...flat(ta, h("div", { class: "row" }, ok), h("h3", {}, "ইতিহাস"), hist.length ? hist : h("p", { class: "muted" }, "ইতিহাস পাওয়া যায়নি।")));
  }
  const openBtn = h("button", { class: "btn", onclick: () => guard(openBtn, () => openFile(pick.value)) }, "খুলুন");
  const rawSection = h("details", { class: "grp" }, h("summary", {}, "কাঁচা ফাইল ও ইতিহাস"),
    h("div", { class: "grp-body stack" }, h("p", { class: "muted small" }, "config/*.yaml ও prompts/*.md সরাসরি সম্পাদনা। প্রতিটি সংরক্ষণ GitHub-এ একটা commit; ভুল হলে ইতিহাস থেকে ফেরান।"), h("div", { class: "row" }, h("div", { class: "grow" }, pick), openBtn), rawBox));

  refresh();
  return [h("h1", {}, "সেটিংস"), h("p", { class: "muted small" }, "বদলালে নিচে সংরক্ষণ-বার আসবে। মডেল, বাজেট ও কাঁচা ফাইলের বদল সঙ্গে সঙ্গে সংরক্ষিত হয়।"), groups, llmSection, rawSection, bar];
}

// ---------- keys ----------
async function keys() {
  const d = await api("/api/keys");
  const have = new Map(d.secrets.map((s) => [s.name, s]));
  const known = new Set(d.keys.map((k) => k.name));

  async function saveSecret(name, valEl, force) {
    const value = valEl.value.trim();
    if (!value) { toast("আগে মান লিখুন", "bad"); return; }
    let r;
    try { r = await api("/api/keys/set", { name, value, ...(force ? { force: true } : {}) }); }
    catch (e) {
      if (e instanceof ApiError && e.status === 422 && e.data && e.data.test) {
        if (confirm(`পরীক্ষায় ব্যর্থ: ${e.data.test.detail}\n\nতবুও সংরক্ষণ করবেন?`)) return saveSecret(name, valEl, true);
        return;
      }
      throw e;
    }
    valEl.value = "";                                          // the typed value never stays on the page
    toast(`✅ ${name} ${r.status === "created" ? "বসানো হয়েছে" : "বদলানো হয়েছে"}`, "ok");
    if (r.after && confirm(`${name} বদলেছে। এখনই ${r.after} চালাবেন? (নতুন মান Worker-এ পৌঁছাতে দরকার)`)) await api("/api/dispatch", { workflow: r.after });
    await route();
  }

  const cards = d.keys.map((k) => {
    const s = have.get(k.name);
    const val = h("input", { type: "password", placeholder: "নতুন মান লিখুন (পুরনোটা দেখানো হয় না)", autocomplete: "off" });
    const out = h("p", { class: "small" }, "");
    const test = h("button", { class: "btn small", onclick: () => guard(test, async () => {
      if (!val.value.trim()) { toast("আগে মান লিখুন", "bad"); return; }
      const r = await api("/api/keys/test", { name: k.name, value: val.value.trim() });
      out.textContent = r.ok === true ? `✅ কাজ করছে: ${r.detail}` : r.ok === false ? `❌ ${r.detail}` : `⚠️ নিশ্চিত হওয়া যায়নি: ${r.detail}`;
    }) }, "পরীক্ষা");
    const save = h("button", { class: "btn small primary", onclick: () => guard(save, () => saveSecret(k.name, val, false)) }, "সংরক্ষণ");
    return h("div", { class: "card" },
      h("div", { class: "row between" }, h("strong", {}, k.label), s ? h("span", { class: "chip ok" }, "সেট আছে") : h("span", { class: "chip bad" }, "সেট নেই")),
      h("div", { class: "mono muted" }, k.name), s && s.updated ? h("div", { class: "muted small" }, `শেষ বদল ${ago(s.updated)}`) : null,
      k.after ? h("div", { class: "muted small" }, `বদলালে পরে ${k.after} চালাতে হয়`) : null,
      val, out, h("div", { class: "row" }, test, save));
  });

  const others = d.secrets.filter((s) => !known.has(s.name));
  const nn = h("input", { type: "text", placeholder: "NAME_IN_CAPITALS" }), nv = h("input", { type: "password", placeholder: "মান", autocomplete: "off" });
  const nsave = h("button", { class: "btn small primary", onclick: () => guard(nsave, async () => {
    const name = nn.value.trim().toUpperCase(); nn.value = name;
    await saveSecret(name, nv, false);
  }) }, "সংরক্ষণ");
  const customCard = h("div", { class: "card alt" }, h("h2", {}, "অন্য সিক্রেট"),
    others.length ? h("div", {}, others.map((s) => h("span", { class: "chip" }, s.name))) : null,
    lbl("নতুন সিক্রেটের নাম"), nn, lbl("মান"), nv, h("div", { class: "row" }, nsave));

  const vn = h("input", { type: "text", placeholder: "VARIABLE_NAME" }), vv = h("input", { type: "text", placeholder: "মান (এক লাইন)" });
  const vsave = h("button", { class: "btn small primary", onclick: () => guard(vsave, async () => {
    const r = await api("/api/vars/set", { name: vn.value.trim().toUpperCase(), value: vv.value });
    toast(`✅ ভ্যারিয়েবল ${r.status === "created" ? "তৈরি" : "হালনাগাদ"} হয়েছে`, "ok"); await route();
  }) }, "সংরক্ষণ");
  const varCard = h("div", { class: "card alt" }, h("h2", {}, "ভ্যারিয়েবল"), h("p", { class: "muted small" }, "ভ্যারিয়েবলের মান গোপন নয়, তাই দেখা যায়।"),
    d.vars.map((v) => h("div", { class: "list-line" }, h("span", { class: "mono" }, v.name), " = ", h("span", { class: "mono" }, v.value))), lbl("নাম"), vn, lbl("মান"), vv, h("div", { class: "row" }, vsave));

  return [h("h1", {}, "কী ও সিক্রেট"),
    h("div", { class: "card alt" }, "সংরক্ষণের আগে কী-টা সত্যিই কাজ করে কি না পরীক্ষা হয়। মান GitHub-এর পাবলিক কী দিয়ে এনক্রিপ্ট হয়ে যায়; এই পাতা কোনো মান পড়তে বা দেখাতে পারে না।"),
    cards, customCard, varCard];
}

// ---------- runs ----------
async function runs() {
  const [r, dec] = await Promise.all([api("/api/runs"), api("/api/decisions")]);
  const mode = select([["", "(ডিফল্ট মোড)"], "review", "auto", "dry-run"], "");
  const hourly = h("button", { class: "btn primary", onclick: () => guard(hourly, async () => {
    if (mode.value === "auto" && !confirm("auto মোডে এই রান নিজে প্রকাশ করবে। চালাবেন?")) return;
    await api("/api/dispatch", { workflow: "hourly", ...(mode.value ? { mode: mode.value } : {}) });
    toast("▶️ hourly রান শুরু হয়েছে", "ok");
  }) }, "hourly রান চালান");
  const others = WORKFLOW_LABELS.filter(([w]) => w !== "hourly").map(([w, label]) => {
    const b = h("button", { class: "btn small", onclick: () => guard(b, async () => {
      if (!confirm(`${w} চালাবেন?`)) return;
      await api("/api/dispatch", { workflow: w }); toast(`▶️ ${w} শুরু হয়েছে`, "ok");
    }) }, label);
    return b;
  });
  const list = r.runs.map((x) => h("div", { class: "list-line" },
    h("div", { class: "row between" }, h("strong", {}, x.name), runChip(x)),
    h("div", { class: "muted small" }, `${x.event} · ${ago(x.created)}`, safeUrl(x.url) ? [" · ", h("a", { href: x.url, target: "_blank", rel: "noopener noreferrer" }, "GitHub-এ দেখুন")] : null)));
  const decs = dec.items.map((x) => {
    const { action, ts, ...rest } = x;
    const text = Object.entries(rest).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ").slice(0, 220);
    return h("div", { class: "list-line" }, h("span", { class: "chip" }, String(action || "?")), h("span", { class: "muted small" }, ts ? ago(ts) : ""), h("div", { class: "small" }, text));
  });
  return [h("h1", {}, "রান"),
    h("div", { class: "card" }, h("h2", {}, "চালু করুন"), h("div", { class: "row" }, h("div", { class: "grow" }, mode), hourly), h("div", { class: "row" }, others)),
    h("div", { class: "card" }, h("h2", {}, "সাম্প্রতিক রান"), list.length ? list : h("p", { class: "muted" }, "কোনো রান নেই।")),
    h("div", { class: "card" }, h("h2", {}, "সাম্প্রতিক সিদ্ধান্ত"), h("p", { class: "muted small" }, "কোন স্টোরি কেন লেখা হলো বা বাদ গেল।"), decs.length ? decs : h("p", { class: "muted" }, "এখনো কোনো সিদ্ধান্ত নেই।"))];
}

// ---------- more: pull requests, two-step, log out, activity ----------
async function more() {
  const [p, sess, au] = await Promise.all([api("/api/prs"), api("/api/session"), api("/api/audit")]);
  const prs = p.prs.map((x) => {
    const act = (action, text, cls) => {
      const b = h("button", { class: "btn small " + cls, onclick: () => guard(b, async () => {
        if (!confirm(`PR #${x.number} ${text}?`)) return;
        await api("/api/pr", { number: x.number, action }); toast("✅ হয়েছে", "ok"); await route();
      }) }, text);
      return b;
    };
    return h("div", { class: "list-line" }, h("div", {}, `#${x.number} ${x.title}`), h("div", { class: "muted small" }, `${x.user || ""} · ${ago(x.created)}`, safeUrl(x.url) ? [" · ", h("a", { href: x.url, target: "_blank", rel: "noopener noreferrer" }, "দেখুন")] : null),
      h("div", { class: "row" }, act("merge", "মার্জ", "primary"), act("close", "বন্ধ", "danger")));
  });

  const totpBox = h("div", { class: "stack" });
  const done = (msg) => { toast(msg, "ok"); setTimeout(() => { location.href = "/"; }, 900); };
  const showOn = () => {
    const pw = h("input", { type: "password", autocomplete: "off", placeholder: "পাসওয়ার্ড" }), code = h("input", { type: "text", inputMode: "numeric", maxLength: 6, placeholder: "অথেনটিকেটরের কোড" });
    const off = h("button", { class: "btn danger", onclick: () => guard(off, async () => {
      if (!confirm("২-ধাপ বন্ধ করবেন?")) return;
      await api("/api/totp/disable", { password: pw.value, code: code.value.trim() }); done("২-ধাপ বন্ধ হয়েছে। আবার লগইন করুন।");
    }) }, "২-ধাপ বন্ধ করুন");
    totpBox.replaceChildren(h("span", { class: "chip ok" }, "২-ধাপ চালু আছে"), lbl("পাসওয়ার্ড"), pw, lbl("বর্তমান কোড"), code, off);
  };
  const showOff = () => {
    const pw = h("input", { type: "password", autocomplete: "off", placeholder: "পাসওয়ার্ড" });
    const start = h("button", { class: "btn primary", onclick: () => guard(start, async () => {
      const r = await api("/api/totp/begin", { password: pw.value });
      const code = h("input", { type: "text", inputMode: "numeric", maxLength: 6, placeholder: "৬ সংখ্যার কোড" });
      const ok = h("button", { class: "btn primary", onclick: () => guard(ok, async () => { await api("/api/totp/confirm", { code: code.value.trim() }); done("২-ধাপ চালু হয়েছে। আবার লগইন করুন।"); }) }, "নিশ্চিত করুন");
      totpBox.replaceChildren(h("p", {}, "অথেনটিকেটর অ্যাপে (Google Authenticator বা অনুরূপ) নতুন অ্যাকাউন্ট যোগ করুন, “কী লিখে যোগ করুন” বেছে এই কী বসান (সময়-ভিত্তিক):"),
        h("pre", { class: "card alt" }, r.secret), h("p", { class: "muted small" }, "অথবা এই লিংক: "), h("pre", { class: "small" }, r.uri), lbl("অ্যাপ যে কোড দেখাচ্ছে"), code, ok);
    }) }, "শুরু করুন");
    totpBox.replaceChildren(h("span", { class: "chip warn" }, "২-ধাপ বন্ধ"), lbl("পাসওয়ার্ড দিয়ে শুরু করুন"), pw, start);
  };
  if (sess.totp) showOn(); else showOff();

  const logout = h("button", { class: "btn", onclick: () => guard(logout, async () => { await api("/api/logout", {}); location.href = "/"; }) }, "লগআউট");
  const logoutAll = h("button", { class: "btn danger", onclick: () => guard(logoutAll, async () => {
    if (!confirm("সব ডিভাইস থেকে লগআউট হবে। করবেন?")) return;
    await api("/api/logout-all", {}); location.href = "/";
  }) }, "সব ডিভাইস থেকে লগআউট");

  return [h("h1", {}, "আরও"),
    h("div", { class: "card" }, h("h2", {}, "খোলা PR"), prs.length ? prs : h("p", { class: "muted" }, "কোনো খোলা PR নেই।")),
    h("div", { class: "card" }, h("h2", {}, "২-ধাপ লগইন"), totpBox),
    h("div", { class: "card" }, h("h2", {}, "লগআউট"), h("div", { class: "row" }, logout, logoutAll)),
    h("div", { class: "card" }, h("h2", {}, "কার্যকলাপের লগ"), h("p", { class: "muted small" }, "শুধু নাম ও সময়, কোনো গোপন মান নয়।"),
      au.items.length ? au.items.slice(0, 40).map((x) => h("div", { class: "list-line" }, h("strong", {}, x.action), x.detail ? ` — ${x.detail}` : "", h("div", { class: "muted small" }, `${fmtTime(x.t)} · ${x.ip}`))) : h("p", { class: "muted" }, "কিছু নেই।"))];
}

const SCREENS = { "": home, drafts, articles, settings, keys, runs, more, draft: draftScreen, article: articleScreen };
window.addEventListener("hashchange", () => route());
route();
