// A small comment-preserving editor for the brain's YAML files (config/newsbot.yaml, config/llm.yaml).
// It understands what those files use: block maps, quoted keys, scalars, one-line flow maps/lists, and block lists whose items are one-line flow maps.
// It never re-serialises the file: a change replaces exactly one value token (or inserts/removes one line), so comments, order and spacing stay.
// Paths are arrays, e.g. ["scoring","weights","velocity"] or ["llm","tiers","strong",0,"model"].

function skipQuoted(s, i) {                      // s[i] is " or ' ; returns the index after the closing quote
  const q = s[i];
  for (let j = i + 1; j < s.length; j++) {
    if (q === '"' && s[j] === "\\") { j++; continue; }
    if (q === "'" && s[j] === "'" && s[j + 1] === "'") { j++; continue; }
    if (s[j] === q) return j + 1;
  }
  return s.length;
}

/** Splits "value   # comment" into the value's [start,end) offsets inside `s` (end excludes trailing spaces). */
function valueSpan(s, from) {
  let i = from;
  while (s[i] === " " || s[i] === "\t") i++;
  if (i >= s.length || s[i] === "#") return null;
  const start = i;
  if (s[i] === '"' || s[i] === "'") i = skipQuoted(s, i);
  else if (s[i] === "{" || s[i] === "[") {
    let depth = 0;
    for (; i < s.length; i++) {
      if (s[i] === '"' || s[i] === "'") { i = skipQuoted(s, i) - 1; continue; }
      if (s[i] === "{" || s[i] === "[") depth++;
      else if (s[i] === "}" || s[i] === "]") { depth--; if (depth === 0) { i++; break; } }
    }
  } else {
    for (; i < s.length; i++) if (s[i] === "#" && (s[i - 1] === " " || s[i - 1] === "\t")) break;
    while (i > start && (s[i - 1] === " " || s[i - 1] === "\t")) i--;
  }
  return [start, i];
}

export function parseScalar(raw) {
  const t = raw.trim();
  if (t.startsWith('"') && t.endsWith('"') && t.length >= 2) { try { return JSON.parse(t); } catch { return t.slice(1, -1); } }
  if (t.startsWith("'") && t.endsWith("'") && t.length >= 2) return t.slice(1, -1).replace(/''/g, "'");
  if (t === "true") return true;
  if (t === "false") return false;
  if (t === "null" || t === "~" || t === "") return null;
  if (/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(t)) return Number(t);
  return t;
}

export function formatScalar(v, wasQuoted = false) {
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") { if (!Number.isFinite(v)) throw new Error("bad number"); return String(v); }
  if (v === null) return "null";
  const s = String(v);
  if (s.includes("\n")) throw new Error("multi-line text is not supported here");
  const bare = /^[A-Za-z_@][A-Za-z0-9_.@\/-]*$/.test(s) && !["true", "false", "null", "yes", "no", "on", "off"].includes(s.toLowerCase());
  return wasQuoted || !bare ? JSON.stringify(s) : s;
}

function splitFlow(inner) {                       // [{start,end}] of top-level comma separated parts of a flow body
  const parts = [];
  let depth = 0, start = 0;
  for (let i = 0; i < inner.length; i++) {
    const c = inner[i];
    if (c === '"' || c === "'") { i = skipQuoted(inner, i) - 1; continue; }
    if (c === "{" || c === "[") depth++;
    else if (c === "}" || c === "]") depth--;
    else if (c === "," && depth === 0) { parts.push([start, i]); start = i + 1; }
  }
  parts.push([start, inner.length]);
  return parts.filter(([a, b]) => inner.slice(a, b).trim() !== "");
}

function keyOf(line, from) {                      // reads `key:` at `from`; returns {key, after} or null
  let key, i = from;
  if (line[i] === '"' || line[i] === "'") {
    const end = skipQuoted(line, i);
    key = parseScalar(line.slice(i, end));
    i = end;
  } else {
    const m = /^[^\s:#\[\]{},'"][^:#]*?(?=:(\s|$))/.exec(line.slice(i));
    if (!m) return null;
    key = m[0].trim(); i += m[0].length;
  }
  if (line[i] !== ":") return null;
  return { key: String(key), after: i + 1 };
}

/** Indexes every editable value. Returns { leaves: Map(pathJSON -> leaf), blocks: Map(pathJSON -> block) , lines, offsets }. */
export function scan(text) {
  const lines = text.split("\n");
  const offsets = [];
  let pos = 0;
  for (const l of lines) { offsets.push(pos); pos += l.length + 1; }
  const leaves = new Map(), blocks = new Map();
  const stack = [{ indent: -1, path: [], isList: false, count: 0 }];
  const addFlowMap = (path, base, span, line) => {
    const body = line.slice(span[0] + 1, span[1] - 1);
    const entries = [];
    for (const [a, b] of splitFlow(body)) {
      const part = body.slice(a, b);
      const lead = part.length - part.trimStart().length;
      const k = keyOf(part, lead);
      if (!k) continue;
      const vs = valueSpan(part, k.after);
      if (!vs) continue;
      const abs = base + span[0] + 1 + a;
      const sub = { path: [...path, k.key], kind: /^[\[{]/.test(part.slice(vs[0], vs[0] + 1)) ? "flowlist" : "scalar", start: abs + vs[0], end: abs + vs[1], entryStart: base + span[0] + 1 + a, entryEnd: base + span[0] + 1 + b };
      sub.raw = text.slice(sub.start, sub.end);
      entries.push(sub);
      leaves.set(JSON.stringify(sub.path), sub);
    }
    return entries;
  };
  for (let li = 0; li < lines.length; li++) {
    const line = lines[li];
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const indent = line.length - line.trimStart().length;
    while (stack.length > 1 && stack[stack.length - 1].indent >= indent) {
      const top = stack[stack.length - 1];
      // a compact list ("- x" at the key's own indent) belongs to the key above it
      if (!(top.pendingList && trimmed.startsWith("- ") && top.indent === indent)) stack.pop(); else break;
    }
    const parent = stack[stack.length - 1];
    const base = offsets[li];
    if (trimmed.startsWith("- ") || trimmed === "-") {
      const idx = parent.count++;
      const path = [...parent.path, idx];
      const after = indent + 1;
      const span = valueSpan(line, after);
      if (!span) continue;
      const ch = line[span[0]];
      const item = { path, line: li, start: base + span[0], end: base + span[1] };
      if (ch === "{") {
        item.kind = "flowmap"; item.raw = text.slice(item.start, item.end);
        item.entries = addFlowMap(path, base, span, line);
      } else if (ch === "[") { item.kind = "flowlist"; item.raw = text.slice(item.start, item.end); }
      else { item.kind = "scalar"; item.raw = text.slice(item.start, item.end); }
      leaves.set(JSON.stringify(path), item);
      const blk = blocks.get(JSON.stringify(parent.path));
      if (blk) { blk.items.push(li); blk.last = li; blk.itemIndent = indent; }
      continue;
    }
    const k = keyOf(line, indent);
    if (!k) continue;
    const path = [...parent.path, k.key];
    const span = valueSpan(line, k.after);
    const pj = JSON.stringify(parent.path);
    if (blocks.has(pj)) { blocks.get(pj).last = li; }
    if (!span) {                                  // a block: children follow
      blocks.set(JSON.stringify(path), { path, line: li, indent, last: li, items: [], childIndent: null });
      const top = { indent, path, count: 0, pendingList: true };
      stack.push(top);
      if (blocks.has(pj) && blocks.get(pj).childIndent === null) blocks.get(pj).childIndent = indent;
      continue;
    }
    if (blocks.has(pj) && blocks.get(pj).childIndent === null) blocks.get(pj).childIndent = indent;
    const first = line[span[0]];
    const leaf = { path, line: li, start: base + span[0], end: base + span[1] };
    leaf.raw = text.slice(leaf.start, leaf.end);
    if (first === "{") { leaf.kind = "flowmap"; leaf.entries = addFlowMap(path, base, span, line); }
    else if (first === "[") leaf.kind = "flowlist";
    else leaf.kind = "scalar";
    leaf.quoted = first === '"' || first === "'";
    leaves.set(JSON.stringify(path), leaf);
    // propagate "last line of block" up the stack
    for (const s of stack) { const b = blocks.get(JSON.stringify(s.path)); if (b) b.last = li; }
  }
  // blocks' last line also covers their list items / nested content
  for (const [, b] of blocks) {
    for (const [pj, other] of blocks) {
      const o = other.path, p = b.path;
      if (o.length > p.length && p.every((x, i) => String(x) === String(o[i])) && other.last > b.last) b.last = other.last;
    }
  }
  return { leaves, blocks, lines, offsets, text };
}

export function readLeaf(leaf) {
  if (!leaf) return undefined;
  if (leaf.kind === "scalar") return parseScalar(leaf.raw);
  if (leaf.kind === "flowlist") {
    const body = leaf.raw.slice(1, -1);
    return splitFlow(body).map(([a, b]) => parseScalar(body.slice(a, b)));
  }
  if (leaf.kind === "flowmap") {
    const out = {};
    for (const e of leaf.entries || []) out[e.path[e.path.length - 1]] = readLeaf(e);
    return out;
  }
  return undefined;
}

/** Scan once, read many: `const read = reader(text); read(["a","b"])`. Use this instead of get() in loops (the Worker's CPU budget is small). */
export function reader(text) {
  const sc = scan(text);
  return (path) => readLeaf(sc.leaves.get(JSON.stringify(path)));
}

export function get(text, path) {
  return readLeaf(scan(text).leaves.get(JSON.stringify(path)));
}

function token(value, leaf) {
  if (Array.isArray(value)) return "[" + value.map((v) => formatScalar(v)).join(", ") + "]";
  return formatScalar(value, !!(leaf && leaf.quoted));
}

/** Sets one value. Existing leaf: replaces just that token. Missing key under an existing block / flow map: inserts it. */
export function set(text, path, value) {
  const sc = scan(text);
  const leaf = sc.leaves.get(JSON.stringify(path));
  if (leaf) {
    if (leaf.kind === "flowmap" || leaf.kind === "block") throw new Error("not a single value: " + path.join("."));
    return text.slice(0, leaf.start) + token(value, leaf) + text.slice(leaf.end);
  }
  const parentPath = path.slice(0, -1), key = String(path[path.length - 1]);
  const pj = JSON.stringify(parentPath);
  const pleaf = sc.leaves.get(pj);
  if (pleaf && pleaf.kind === "flowmap") {            // {} or {a: 1}: rebuild the braces, keep the existing entry text as is
    const parts = (pleaf.entries || []).map((e) => text.slice(e.entryStart, e.entryEnd).trim());
    parts.push(`${formatScalar(key)}: ${token(value)}`);
    return text.slice(0, pleaf.start) + "{" + parts.join(", ") + "}" + text.slice(pleaf.end);
  }
  const blk = sc.blocks.get(pj);
  if (blk && !pleaf) {
    const indent = " ".repeat(blk.childIndent ?? blk.indent + 2);
    const at = blk.last;
    const lines = sc.lines.slice();
    lines.splice(at + 1, 0, `${indent}${formatScalar(key)}: ${token(value)}`);
    return lines.join("\n");
  }
  throw new Error("key not found and its parent is not editable: " + path.join("."));
}

/** Removes one key (a scalar line, a flow-map entry). */
export function remove(text, path) {
  const sc = scan(text);
  const leaf = sc.leaves.get(JSON.stringify(path));
  if (!leaf) throw new Error("not found: " + path.join("."));
  const parent = sc.leaves.get(JSON.stringify(path.slice(0, -1)));
  if (parent && parent.kind === "flowmap") {
    const parts = parent.entries.filter((e) => e !== leaf).map((e) => text.slice(e.entryStart, e.entryEnd).trim());
    return text.slice(0, parent.start) + "{" + parts.join(", ") + "}" + text.slice(parent.end);
  }
  const lines = sc.lines.slice();
  lines.splice(leaf.line, 1);
  return lines.join("\n");
}

/** Block list of one-line flow maps (llm.tiers.*): insert a new item at `index` (0..n) or remove one. */
export function listInsert(text, path, index, obj) {
  const sc = scan(text);
  const blk = sc.blocks.get(JSON.stringify(path));
  if (!blk || !blk.items.length) throw new Error("list not found: " + path.join("."));
  const indent = " ".repeat(blk.itemIndent);
  const item = indent + "- {" + Object.entries(obj).map(([k, v]) => `${k}: ${formatScalar(v, k === "model")}`).join(", ") + "}";
  const lines = sc.lines.slice();
  const at = index <= 0 ? blk.items[0] : blk.items[Math.min(index, blk.items.length) - 1] + 1;
  lines.splice(at, 0, item);
  return lines.join("\n");
}

export function listRemove(text, path, index) {
  const sc = scan(text);
  const blk = sc.blocks.get(JSON.stringify(path));
  if (!blk || !(index >= 0 && index < blk.items.length)) throw new Error("list item not found");
  if (blk.items.length <= 1) throw new Error("a chain needs at least one entry");
  const lines = sc.lines.slice();
  lines.splice(blk.items[index], 1);
  return lines.join("\n");
}

/** Every leaf as a plain object { "a.b.c": value } for display and tests. */
export function flatten(text) {
  const out = {};
  for (const [k, leaf] of scan(text).leaves) {
    if (leaf.kind === "flowmap") continue;
    out[JSON.parse(k).join(".")] = readLeaf(leaf);
  }
  return out;
}
