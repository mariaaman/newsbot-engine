"""The dashboard's settings schema (dashboard/src/schema.js) must match newsbot/config.py: same paths, same types, and bounds at least as strict.
Read with ast, so it runs without pydantic."""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def models():
    tree = ast.parse((ROOT / "newsbot" / "config.py").read_text(encoding="utf-8"))
    out, lang = {}, None
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "Lang":
            lang = [e.value for e in node.value.slice.elts]
        if isinstance(node, ast.ClassDef):
            fields = {}
            for st in node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    kw = {}
                    if isinstance(st.value, ast.Call) and getattr(st.value.func, "id", "") == "Field":
                        kw = {k.arg: ast.literal_eval(k.value) for k in st.value.keywords if k.arg in ("ge", "gt", "le", "lt", "pattern")}
                    fields[st.target.id] = (ast.unparse(st.annotation), kw)
            out[node.name] = fields
    return out, lang


def resolve(m, path):
    """-> (annotation, bounds) of the leaf, descending through nested models and dict[...] maps."""
    cls = "Config"
    ann, kw = "", {}
    for i, seg in enumerate(path):
        if isinstance(seg, int):
            return ann, kw
        if cls is None:                                   # inside a dict[...] or list[...]
            return ann, kw
        assert seg in m[cls], f"{'.'.join(map(str, path))}: no field {seg!r} in {cls}"
        ann, kw = m[cls][seg]
        cls = ann if ann in m else None
        if ann.startswith("dict[") and i < len(path) - 1:
            return ann, kw                                # a map: the next segment is a key
    return ann, kw


def test_schema_matches_config_py():
    if not shutil.which("node"):
        return
    r = subprocess.run(["node", "--input-type=module", "-e", "import {FIELDS} from './dashboard/src/schema.js'; console.log(JSON.stringify(FIELDS))"],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    fields = json.loads(r.stdout)
    m, lang = models()
    checked = 0
    for f in fields:
        path = f["path"]
        if path[0] == "llm" and f["file"] != "config/llm.yaml":
            continue
        # a map value (qa.min_words.bn, write.local_angle.en, site.ai_disclosure.bn, sensitive.keywords.death) is checked on its map field
        walk = path
        if path[:2] in (["qa", "min_words"], ["write", "local_angle"], ["site", "ai_disclosure"], ["sensitive", "keywords"]):
            walk = path[:2]
        ann, kw = resolve(m, walk)
        name = ".".join(map(str, path))
        t = f["type"]
        if walk != path:
            assert ann.startswith("dict["), f"{name}: expected a map, got {ann}"
            if path[0] in ("qa", "write", "site"):
                assert path[-1] in lang, f"{name}: key must be a site language {lang}"
            if path[0] == "qa":
                assert "int" in ann, f"{name}: {ann}"
            if path[0] == "sensitive":
                assert "list[str]" in ann
            continue
        if t == "bool":
            assert ann == "bool", f"{name}: {ann}"
        elif t == "int":
            assert ann == "int", f"{name}: {ann}"
        elif t == "float":
            assert ann in ("float", "int"), f"{name}: {ann}"
        elif t == "list":
            assert ann.startswith("list[str]"), f"{name}: {ann}"
        elif t == "str":
            assert ann == "str", f"{name}: {ann}"
        elif t == "enum":
            assert ann.startswith("Literal[") or ann == "Lang", f"{name}: {ann}"
            opts = lang if ann == "Lang" else list(ast.literal_eval(ann[len("Literal"):]))
            assert sorted(f["options"]) == sorted(opts), f"{name}: schema {f['options']} vs engine {opts}"
        if t in ("int", "float"):
            lo, hi = f.get("min"), f.get("max")
            if "ge" in kw:
                assert lo is not None and lo >= kw["ge"] and not f.get("exclusiveMin") or (f.get("exclusiveMin") and lo >= kw["ge"]), f"{name}: min {lo} looser than ge={kw['ge']}"
            if "gt" in kw:
                assert lo is not None and (lo > kw["gt"] or (lo == kw["gt"] and f.get("exclusiveMin"))), f"{name}: min {lo} looser than gt={kw['gt']}"
            if "le" in kw:
                assert hi is not None and hi <= kw["le"], f"{name}: max {hi} looser than le={kw['le']}"
        if t == "str" and "pattern" in kw:
            assert f.get("pattern") == kw["pattern"], f"{name}: pattern differs from the engine's"
        checked += 1
    assert checked > 60, checked
