import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { scan, get, set, remove, listInsert, listRemove, flatten, parseScalar, formatScalar } from "../src/yamledit.js";

const dir = new URL("../../brain-template/config/", import.meta.url);
const NEWS = fs.readFileSync(new URL("newsbot.yaml", dir), "utf8");
const LLM = fs.readFileSync(new URL("llm.yaml", dir), "utf8");
const diffLines = (a, b) => { const x = a.split("\n"), y = b.split("\n"); return { removed: x.filter((l) => !y.includes(l)), added: y.filter((l) => !x.includes(l)) }; };

test("reads block scalars, flow maps, flow lists, quoted keys and list-item maps", () => {
  assert.equal(get(NEWS, ["publish", "mode"]), "review");
  assert.equal(typeof get(NEWS, ["limits", "max_new_per_day"]), "number");
  assert.deepEqual(get(NEWS, ["site", "languages"]), ["bn", "en"]);
  assert.equal(typeof get(NEWS, ["scoring", "weights", "velocity"]), "number");
  assert.equal(typeof get(NEWS, ["qa", "min_words", "bn"]), "number");
  assert.equal(get(LLM, ["llm", "tiers", "strong", 0, "provider"]), "workers_ai");
  assert.ok(Object.keys(flatten(LLM)).some((k) => k.startsWith("llm.neuron_rates.@cf/openai/gpt-oss-120b.")));      // quoted key with slash and dots
  assert.ok(get(NEWS, ["sensitive", "keywords", "death"]).includes("died"));
});

test("a change replaces one token only: comments, spacing and every other line stay", () => {
  const a = set(NEWS, ["limits", "max_new_per_day"], 3);
  const d = diffLines(NEWS, a);
  assert.equal(d.removed.length, 1); assert.equal(d.added.length, 1);
  assert.equal(get(a, ["limits", "max_new_per_day"]), 3);
  const b = set(NEWS, ["publish", "mode"], "auto");
  const db = diffLines(NEWS, b);
  assert.equal(db.removed.length, 1); assert.equal(db.added.length, 1);
  assert.match(db.added[0], /^ +mode: auto( +#.*)?$/);                         // the trailing comment (if any) is kept
  assert.equal(db.removed[0].replace(/ *#.*$/, "").replace("review", "auto"), db.added[0].replace(/ *#.*$/, ""));
  assert.equal(db.removed[0].split("#")[1], db.added[0].split("#")[1]);
  assert.equal(get(b, ["publish", "mode"]), "auto");
  const c = set(NEWS, ["site", "name"], 'My "News" ✓');
  assert.equal(get(c, ["site", "name"]), 'My "News" ✓');
  assert.equal(set(NEWS, ["limits", "max_new_per_day"], get(NEWS, ["limits", "max_new_per_day"])), NEWS);
});

test("flow-map entries and flow lists", () => {
  const a = set(NEWS, ["scoring", "weights", "velocity"], 0.5);
  assert.equal(get(a, ["scoring", "weights", "velocity"]), 0.5);
  assert.equal(get(a, ["scoring", "weights", "breadth"]), get(NEWS, ["scoring", "weights", "breadth"]));
  const b = set(NEWS, ["site", "blocked_topics"], ["gossip", "two words", "x"]);
  assert.deepEqual(get(b, ["site", "blocked_topics"]), ["gossip", "two words", "x"]);
  const c = set(NEWS, ["sensitive", "keywords", "crime"], ["arrest*", "হত্যা"]);
  assert.deepEqual(get(c, ["sensitive", "keywords", "crime"]), ["arrest*", "হত্যা"]);
});

test("missing keys are inserted into an existing block or an empty flow map; unknown parents are refused", () => {
  const a = set(NEWS.replace(/^ *approve_buttons:.*\n/m, ""), ["notify", "telegram", "approve_buttons"], true);
  assert.equal(get(a, ["notify", "telegram", "approve_buttons"]), true);
  assert.equal(get(a, ["notify", "telegram", "max_lines"]), get(NEWS, ["notify", "telegram", "max_lines"]));
  const b = set(NEWS, ["ads", "slots", "after_summary"], "123");
  assert.equal(get(b, ["ads", "slots", "after_summary"]), "123");
  assert.throws(() => set(NEWS, ["nope", "x"], 1), /not found/);
  assert.throws(() => set(NEWS, ["scoring", "weights"], 1), /single value/);
});

test("llm chains: change a model, insert and remove an entry", () => {
  const a = set(LLM, ["llm", "tiers", "small", 1, "model"], "openai/gpt-oss-120b-v2");
  assert.equal(get(a, ["llm", "tiers", "small", 1, "model"]), "openai/gpt-oss-120b-v2");
  assert.equal(get(a, ["llm", "tiers", "small", 0, "model"]), get(LLM, ["llm", "tiers", "small", 0, "model"]));
  const n = Object.keys(flatten(LLM)).filter((k) => /^llm\.tiers\.small\.\d+\.provider$/.test(k)).length;
  const b = listInsert(LLM, ["llm", "tiers", "small"], 1, { provider: "workers_ai", model: "@cf/x/y" });
  assert.equal(get(b, ["llm", "tiers", "small", 1, "model"]), "@cf/x/y");
  assert.equal(get(b, ["llm", "tiers", "small", 2, "provider"]), get(LLM, ["llm", "tiers", "small", 1, "provider"]));
  const c = listRemove(b, ["llm", "tiers", "small"], 1);
  assert.equal(c, LLM);
  assert.equal(Object.keys(flatten(c)).filter((k) => /^llm\.tiers\.small\.\d+\.provider$/.test(k)).length, n);
  const one = "llm:\n  tiers:\n    small:\n      - {provider: workers_ai, model: m}\n";
  assert.throws(() => listRemove(one, ["llm", "tiers", "small"], 0), /at least one/);
});

test("quoted keys with dots and slashes can be edited and removed", () => {
  const key = Object.keys(flatten(LLM)).find((k) => k.includes("neuron_rates.@cf/openai/gpt-oss-120b.in"));
  const path = ["llm", "neuron_rates", "@cf/openai/gpt-oss-120b", "in"];
  assert.ok(key);
  const a = set(LLM, path, 123456);
  assert.equal(get(a, path), 123456);
  const b = remove(a, path);
  assert.equal(get(b, path), undefined);
});

test("scalar helpers", () => {
  assert.equal(parseScalar('"a # b"'), "a # b");
  assert.equal(parseScalar("'it''s'"), "it's");
  assert.equal(parseScalar("0.45"), 0.45);
  assert.equal(parseScalar("true"), true);
  assert.equal(formatScalar("review"), "review");
  assert.equal(formatScalar("true"), '"true"');                                // a string that looks like a boolean is quoted
  assert.equal(formatScalar("a: b"), '"a: b"');
  assert.throws(() => formatScalar("a\nb"), /multi-line/);
  assert.throws(() => formatScalar(Infinity), /bad number/);
});
