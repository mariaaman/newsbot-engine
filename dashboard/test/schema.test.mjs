import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { FIELDS, coerce, findField, GROUPS } from "../src/schema.js";
import { get } from "../src/yamledit.js";

const dir = new URL("../../brain-template/config/", import.meta.url);
const files = { "config/newsbot.yaml": fs.readFileSync(new URL("newsbot.yaml", dir), "utf8"), "config/llm.yaml": fs.readFileSync(new URL("llm.yaml", dir), "utf8") };

test("every field has a known group, a unique path and sane bounds", () => {
  const groups = new Set(GROUPS.map((g) => g[0]));
  const seen = new Set();
  for (const f of FIELDS) {
    assert.ok(groups.has(f.group), f.group);
    const id = f.file + JSON.stringify(f.path);
    assert.ok(!seen.has(id), "duplicate " + id); seen.add(id);
    if (f.min !== undefined && f.max !== undefined) assert.ok(f.min <= f.max);
  }
});

test("values present in the template pass their own schema", () => {
  let checked = 0;
  for (const f of FIELDS) {
    const v = get(files[f.file], f.path);
    if (v === undefined) continue;
    assert.doesNotThrow(() => coerce(f, v), `${f.path.join(".")} = ${JSON.stringify(v)}`);
    checked++;
  }
  assert.ok(checked > 30, "template covers the schema: " + checked);
});

test("coerce enforces type, range, enum, pattern and list rules", () => {
  const f = (p) => findField("config/newsbot.yaml", p);
  assert.equal(coerce(f(["limits", "feed_workers"]), "8"), 8);
  assert.throws(() => coerce(f(["limits", "feed_workers"]), 33), /at most 32/);
  assert.throws(() => coerce(f(["limits", "feed_workers"]), 2.5), /whole/);
  assert.throws(() => coerce(f(["limits", "max_new_per_run"]), 0), /at least 1/);
  assert.throws(() => coerce(f(["limits", "max_item_age_hours"]), 0), /greater than 0/);
  assert.equal(coerce(f(["clustering", "embedding_threshold"]), 0.74), 0.74);
  assert.throws(() => coerce(f(["clustering", "embedding_threshold"]), 1.5), /at most 1/);
  assert.throws(() => coerce(f(["publish", "mode"]), "yolo"), /one of/);
  assert.equal(coerce(f(["publish", "mode"]), "auto"), "auto");
  assert.throws(() => coerce(f(["ads", "enabled"]), "true"), /true or false/);
  assert.equal(coerce(f(["ads", "publisher_id"]), "pub-1234567890123456"), "pub-1234567890123456");
  assert.throws(() => coerce(f(["ads", "publisher_id"]), "x"), /does not look right/);
  assert.throws(() => coerce(f(["site", "base_url"]), "http://x.com"), /does not look right/);
  assert.throws(() => coerce(f(["site", "name"]), "a\nb"), /single line/);
  assert.deepEqual(coerce(f(["site", "blocked_topics"]), "gossip, adult ,,x"), ["gossip", "adult", "x"]);
  assert.throws(() => coerce(f(["site", "blocked_topics"]), "a, b]"), /cannot contain/);
  assert.throws(() => coerce(f(["limits", "max_new_per_day"]), NaN), /number/);
});
