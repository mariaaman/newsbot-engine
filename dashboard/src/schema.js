// What the Settings page can edit. Every entry mirrors a field of newsbot/config.py (type and bounds); tests/test_dashboard_schema.py
// checks that against the real pydantic models, so the dashboard can never accept a value the engine would reject.
// file: which brain file holds the value. Fields missing from the file are shown with "default" and can be added.

const N = "config/newsbot.yaml", L = "config/llm.yaml";
const f = (file, path, type, group, label, help, extra = {}) => ({ file, path, type, group, label, help, ...extra });
const int = (file, path, group, label, help, min, max) => f(file, path, "int", group, label, help, { min, ...(max !== undefined ? { max } : {}) });
const num = (file, path, group, label, help, min, max, exclusiveMin = false) => f(file, path, "float", group, label, help, { min, ...(max !== undefined ? { max } : {}), ...(exclusiveMin ? { exclusiveMin: true } : {}) });
const bool = (file, path, group, label, help) => f(file, path, "bool", group, label, help);
const str = (file, path, group, label, help, extra) => f(file, path, "str", group, label, help, extra);
const en = (file, path, group, label, help, options) => f(file, path, "enum", group, label, help, { options });
const list = (file, path, group, label, help) => f(file, path, "list", group, label, help);

export const GROUPS = [
  ["publish", "প্রকাশ"], ["limits", "সীমা ও গতি"], ["scoring", "স্কোরিং"], ["clustering", "ক্লাস্টারিং"], ["research", "গবেষণা"],
  ["qa", "মান-যাচাই"], ["write", "লেখা"], ["site", "সাইট"], ["ads", "বিজ্ঞাপন"], ["telegram", "টেলিগ্রাম"], ["llm", "মডেল ও বাজেট"], ["sensitive", "সংবেদনশীল শব্দ"],
];

export const FIELDS = [
  en(N, ["publish", "mode"], "publish", "প্রকাশের মোড", "review = আপনি দেখে অনুমোদন দেন; auto = নিজে প্রকাশ (সংবেদনশীল খসড়া তবুও আটকে থাকে)", ["review", "auto"]),

  int(N, ["limits", "max_new_per_day"], "limits", "দিনে সর্বোচ্চ নতুন আর্টিকেল", "দিনের সীমা (সাইটের সময়ে)", 0),
  int(N, ["limits", "max_new_per_run"], "limits", "প্রতি রানে নতুন আর্টিকেল", "এক ঘণ্টার রানে কয়টা লেখা হবে। বেশি = বেশি টোকেন ও সময়", 1),
  int(N, ["limits", "max_updates_per_story_per_day"], "limits", "একই স্টোরির দৈনিক আপডেট", "", 0),
  num(N, ["limits", "min_hours_between_updates"], "limits", "আপডেটের মধ্যে ন্যূনতম ঘণ্টা", "", 0),
  int(N, ["limits", "min_independent_sources"], "limits", "ন্যূনতম স্বাধীন সূত্র", "কয়টা আলাদা সংস্থা কভার করলে স্টোরি লেখা হবে", 1),
  int(N, ["limits", "max_sources_per_story"], "limits", "স্টোরিপ্রতি সর্বোচ্চ সূত্র", "বেশি = পূর্ণাঙ্গ লেখা, বেশি টোকেন", 1),
  en(N, ["limits", "routing_mode"], "limits", "ভাষা-রাউটিং", "cross_language = যেকোনো ভাষার খবর সব ভাষায় লেখা হয়", ["cross_language", "same_language"]),
  int(N, ["limits", "routing_min_orgs"], "limits", "রাউটিংয়ে ন্যূনতম সংস্থা", "", 1),
  int(N, ["limits", "routing_min_domains_per_language"], "limits", "ভাষাপ্রতি ন্যূনতম ডোমেইন", "শুধু same_language মোডে কাজে লাগে", 1),
  num(N, ["limits", "max_item_age_hours"], "limits", "ফিড আইটেমের সর্বোচ্চ বয়স (ঘণ্টা)", "এর চেয়ে পুরনো আইটেম বাদ", 0, undefined, true),
  int(N, ["limits", "max_items_per_feed"], "limits", "ফিডপ্রতি সর্বোচ্চ আইটেম", "", 1),
  int(N, ["limits", "max_items_per_run"], "limits", "প্রতি রানে সর্বোচ্চ আইটেম", "", 10),
  int(N, ["limits", "feed_workers"], "limits", "সমান্তরাল ফিড-পাঠক", "ফিড একসঙ্গে কয়টা আনা হবে", 1, 32),
  int(N, ["limits", "diversity_window"], "limits", "বৈচিত্র্যের জানালা", "শেষ কয়টা স্টোরি ধরে বৈচিত্র্য দেখা হবে", 1),
  num(N, ["limits", "diversity_penalty"], "limits", "বৈচিত্র্যের জরিমানা", "০ = বন্ধ", 0, 1),
  int(N, ["limits", "min_update_fact_words"], "limits", "আপডেটের ন্যূনতম তথ্য-শব্দ", "", 1),
  num(N, ["limits", "insufficient_sources_cooldown_hours"], "limits", "অপর্যাপ্ত সূত্রের বিরতি (ঘণ্টা)", "০ = প্রতি ঘণ্টায় আবার চেষ্টা", 0),

  num(N, ["scoring", "threshold"], "scoring", "স্কোর-সীমা", "এর নিচের স্টোরি লেখা হয় না", 0),
  num(N, ["scoring", "weights", "velocity"], "scoring", "ওজন: গতি", "", 0),
  num(N, ["scoring", "weights", "breadth"], "scoring", "ওজন: বিস্তৃতি", "", 0),
  num(N, ["scoring", "weights", "trends"], "scoring", "ওজন: ট্রেন্ড", "", 0),
  num(N, ["scoring", "weights", "recency"], "scoring", "ওজন: সাম্প্রতিকতা", "", 0),
  num(N, ["scoring", "weights", "topic_fit"], "scoring", "ওজন: বিষয়-মিল", "", 0),
  num(N, ["scoring", "weights", "saturation"], "scoring", "ওজন: সংপৃক্ততা", "", 0),
  num(N, ["scoring", "weights", "global_reach"], "scoring", "ওজন: বৈশ্বিক পরিধি", "একাধিক ভাষায় কভার হলে বোনাস, ০ = বন্ধ", 0),
  num(N, ["scoring", "recency_half_life_hours"], "scoring", "সাম্প্রতিকতার অর্ধায়ু (ঘণ্টা)", "", 0, undefined, true),
  num(N, ["scoring", "velocity_window_hours"], "scoring", "গতির জানালা (ঘণ্টা)", "", 0, undefined, true),
  num(N, ["scoring", "velocity_norm"], "scoring", "গতির নর্ম", "", 0, undefined, true),
  num(N, ["scoring", "breadth_norm"], "scoring", "বিস্তৃতির নর্ম", "", 0, undefined, true),
  int(N, ["scoring", "trends_top_n"], "scoring", "ট্রেন্ডের শীর্ষ N", "", 1),
  int(N, ["scoring", "saturation_start"], "scoring", "সংপৃক্ততা শুরু (ডোমেইন)", "", 1),
  int(N, ["scoring", "saturation_span"], "scoring", "সংপৃক্ততার পরিসর", "", 1),

  num(N, ["clustering", "lexical_threshold"], "clustering", "শব্দগত সীমা", "", 0, 1),
  num(N, ["clustering", "embedding_threshold"], "clustering", "এমবেডিং সীমা", "ভিন্ন খবর মিশলে বাড়ান, একই খবর ভাঙলে কমান", 0, 1),
  num(N, ["clustering", "story_match_threshold"], "clustering", "স্টোরি-মিলের সীমা", "", 0, 1),
  int(N, ["clustering", "min_shared_tokens"], "clustering", "ন্যূনতম সাধারণ টোকেন", "", 1),

  num(N, ["research", "attach_threshold"], "research", "সংযুক্তির সীমা", "সম্পর্কিত একক আইটেম টানার ঘনিষ্ঠতা। ০ = বন্ধ। অন্য ঘটনা ঢুকলে বাড়ান", 0, 1),
  int(N, ["research", "max_extra_items"], "research", "অতিরিক্ত আইটেম", "", 0),
  num(N, ["research", "window_hours"], "research", "সময়ের জানালা (ঘণ্টা)", "", 0, undefined, true),
  int(N, ["research", "source_chars"], "research", "সূত্র-পাতার অক্ষর", "Groq ফ্রি টিয়ারে টোকেন সীমা আছে", 1000, 30000),
  int(N, ["research", "claims_per_source"], "research", "সূত্রপ্রতি তথ্য", "বেশি = সমৃদ্ধ লেখা", 5, 60),

  int(N, ["qa", "min_words", "bn"], "qa", "ন্যূনতম শব্দ (বাংলা)", "কম শব্দের লেখা pending-এ যায়", 50),
  int(N, ["qa", "min_words", "en"], "qa", "ন্যূনতম শব্দ (ইংরেজি)", "", 50),
  num(N, ["qa", "max_ngram_overlap"], "qa", "সূত্রের সঙ্গে সর্বোচ্চ মিল", "বেশি মিললে নকলের সন্দেহ", 0, 1),
  int(N, ["qa", "ngram_size"], "qa", "n-gram আকার", "", 3),
  int(N, ["qa", "min_confirmed_facts"], "qa", "ন্যূনতম নিশ্চিত তথ্য", "কম হলে pending-এ যায়", 0),
  int(N, ["qa", "max_quote_words"], "qa", "উদ্ধৃতির সর্বোচ্চ শব্দ", "", 1),
  num(N, ["qa", "duplicate_title_similarity"], "qa", "শিরোনামের ডুপ্লিকেট-সীমা", "", 0, 1),
  num(N, ["qa", "duplicate_embedding_threshold"], "qa", "এমবেডিং ডুপ্লিকেট-সীমা", "", 0, 1),
  int(N, ["qa", "recent_articles_checked"], "qa", "ডুপ্লিকেট-যাচাইয়ে সাম্প্রতিক আর্টিকেল", "", 1),
  list(N, ["qa", "banned_phrases"], "qa", "নিষিদ্ধ বাক্যাংশ", "কমা দিয়ে আলাদা করুন"),

  int(N, ["write", "max_tokens"], "write", "লেখার সর্বোচ্চ টোকেন", "বাংলায় বেশি টোকেন লাগে", 500),
  int(N, ["write", "own_source_chars"], "write", "নিজ-ভাষার সূত্রের অক্ষর", "", 0),
  str(N, ["write", "local_angle", "bn"], "write", "স্থানীয় দৃষ্টিকোণ (বাংলা)", "লেখকের নির্দেশনা", { multiline: true }),
  str(N, ["write", "local_angle", "en"], "write", "স্থানীয় দৃষ্টিকোণ (ইংরেজি)", "", { multiline: true }),

  str(N, ["site", "name"], "site", "সাইটের নাম", "", { maxLen: 80 }),
  str(N, ["site", "base_url"], "site", "সাইটের ঠিকানা", "কাস্টম ডোমেইন যোগ করলে বদলান", { pattern: "^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[^\\s]*)?$" }),
  en(N, ["site", "default"], "site", "ডিফল্ট ভাষা", "", ["bn", "en"]),
  bool(N, ["site", "bengali_font"], "site", "বাংলা ফন্ট ফাইল আছে", "ফন্ট ফাইল যোগ করার পরই true করুন"),
  num(N, ["site", "timezone_offset_hours"], "site", "সময়-অঞ্চলের অফসেট (ঘণ্টা)", "ঢাকা = ৬", -12, 14),
  list(N, ["site", "categories"], "site", "বিভাগ", "কমা দিয়ে আলাদা করুন"),
  list(N, ["site", "allowed_topics"], "site", "অনুমোদিত বিষয়", "ফাঁকা = সব"),
  list(N, ["site", "blocked_topics"], "site", "নিষিদ্ধ বিষয়", ""),
  str(N, ["site", "ai_disclosure", "bn"], "site", "এআই ঘোষণা (বাংলা)", "প্রতিটি আর্টিকেলে দেখায়", { multiline: true }),
  str(N, ["site", "ai_disclosure", "en"], "site", "এআই ঘোষণা (ইংরেজি)", "", { multiline: true }),

  bool(N, ["ads", "enabled"], "ads", "বিজ্ঞাপন চালু", "AdSense অনুমোদনের পরই true করুন"),
  str(N, ["ads", "publisher_id"], "ads", "পাবলিশার আইডি", "যেমন pub-1234567890123456", { pattern: "^((ca-)?pub-[0-9]{10,20})?$" }),
  bool(N, ["ads", "consent_required"], "ads", "সম্মতি-ব্যানার বাধ্যতামূলক", ""),

  bool(N, ["notify", "telegram", "enabled"], "telegram", "টেলিগ্রাম বার্তা চালু", ""),
  en(N, ["notify", "telegram", "language"], "telegram", "বার্তার ভাষা", "", ["bn", "en"]),
  bool(N, ["notify", "telegram", "report_idle_runs"], "telegram", "ফাঁকা রানেও বার্তা", ""),
  bool(N, ["notify", "telegram", "include_summary"], "telegram", "সারাংশসহ বার্তা", "সারাংশ টেলিগ্রামের সার্ভার দিয়ে যায়"),
  int(N, ["notify", "telegram", "max_lines"], "telegram", "রিপোর্টের সর্বোচ্চ লাইন", "", 10, 200),
  bool(N, ["notify", "telegram", "approve_buttons"], "telegram", "অনুমোদন/বাতিল বাটন", "approve Worker চালু থাকলে"),
  str(N, ["notify", "telegram", "edit_url"], "telegram", "approve Worker-এর ঠিকানা", "খসড়ার ব্যক্তিগত লিংকের জন্য", { pattern: "^(https://[A-Za-z0-9.-]+(:[0-9]+)?/?)?$" }),

  num(L, ["llm", "budget", "stop_fraction"], "llm", "বাজেটের কতটুকুতে থামবে", "০.৯ = সীমার ৯০%-এ পরের মডেলে যাবে", 0, 1, true),
  int(L, ["llm", "retry", "max_retries"], "llm", "429-এ সর্বোচ্চ পুনরায় চেষ্টা", "ফ্রি টিয়ার প্রতি মিনিটে সীমা দেয়", 0),
  num(L, ["llm", "retry", "base_delay_s"], "llm", "প্রথম অপেক্ষা (সেকেন্ড)", "", 0),
  num(L, ["llm", "retry", "max_delay_s"], "llm", "সর্বোচ্চ অপেক্ষা (সেকেন্ড)", "", 0),
];

for (const cat of ["death", "crime", "health", "election", "religion", "minors", "financial_advice"]) {
  FIELDS.push(list(N, ["sensitive", "keywords", cat], "sensitive", `সংবেদনশীল: ${cat}`, "ইংরেজি: পুরো শব্দ, stem* = উপসর্গ। বাংলা: অংশ-মিল"));
}

export const TASKS = ["extract", "verify", "write_bn", "write_en", "qa_check"];
export const PROVIDERS = ["groq", "workers_ai", "gemini", "openrouter"];
export const TIERS = ["small", "strong"];

const key = (f) => `${f.file}|${JSON.stringify(f.path)}`;
export const FIELD_INDEX = new Map(FIELDS.map((f) => [key(f), f]));
export const findField = (file, path) => FIELD_INDEX.get(`${file}|${JSON.stringify(path)}`);

/** Validates and normalises one edited value against its schema entry. Throws Error(message) in Bengali-free plain English (the UI shows it). */
export function coerce(field, value) {
  switch (field.type) {
    case "bool":
      if (typeof value !== "boolean") throw new Error("must be true or false");
      return value;
    case "int":
    case "float": {
      const n = typeof value === "string" && value.trim() !== "" ? Number(value) : value;
      if (typeof n !== "number" || !Number.isFinite(n)) throw new Error("must be a number");
      if (field.type === "int" && !Number.isInteger(n)) throw new Error("must be a whole number");
      if (field.min !== undefined && (field.exclusiveMin ? n <= field.min : n < field.min)) throw new Error(`must be ${field.exclusiveMin ? "greater than" : "at least"} ${field.min}`);
      if (field.max !== undefined && n > field.max) throw new Error(`must be at most ${field.max}`);
      return n;
    }
    case "enum":
      if (!field.options.includes(value)) throw new Error("must be one of: " + field.options.join(", "));
      return value;
    case "str": {
      if (typeof value !== "string") throw new Error("must be text");
      const s = value.trim();
      if (/[\r\n]/.test(s)) throw new Error("must be a single line");
      if (field.maxLen && s.length > field.maxLen) throw new Error(`at most ${field.maxLen} characters`);
      if (field.pattern && !new RegExp(field.pattern).test(s)) throw new Error("does not look right");
      return s;
    }
    case "list": {
      const items = Array.isArray(value) ? value : String(value).split(",");
      const out = items.map((x) => String(x).trim()).filter(Boolean);
      if (out.some((x) => /[\r\n\[\]{}"#]/.test(x))) throw new Error("items cannot contain [ ] { } \" # or line breaks");
      return out;
    }
    default:
      throw new Error("unknown field type");
  }
}

// Dynamic, non-schema editors (their own checks live in the server code): chains, budgets, tasks, embedding.
export const KEY_RE = /^[A-Za-z0-9_@./:-]{1,120}$/;
export const MODEL_RE = /^[A-Za-z0-9_@./:+-]{1,160}$/;
