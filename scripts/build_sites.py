#!/usr/bin/env python3
"""Build every website of this deployment and write a manifest for scripts/deploy_sites.py.

  site.sites empty  -> ONE site with every language in /bn/ /en/ ... (public dir site/public, project from $PAGES_PROJECT)
  site.sites filled -> one separate site PER language (public dirs site/public-<lang>, project from site.sites.<lang>.pages_project)
The same engine, the same brain, the same articles: only where each language is published differs. Each build is size-checked
(Cloudflare Pages limits) before anything is deployed. Stdlib + PyYAML only; the engine repo is public, so only language codes and
counts are printed, never addresses or project names."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_one(gen, data: dict, brain: Path, site: Path, public: Path, hugo: str, run=subprocess.run, everything: dict | None = None):
    gen.write_site_files(data, site)
    gen.write_language_files(data, site)
    counts = gen.sync_content(brain, site, data["languages"])
    alts = site / "data" / "alts.json"
    if data.get("separate"):
        alts.write_text(json.dumps(gen.alts_map(brain, everything or data), ensure_ascii=False), encoding="utf-8")
    elif alts.exists():
        alts.unlink()                                         # a leftover from an earlier separate-sites build
    if public.exists():
        shutil.rmtree(public)
    r = run([hugo, "--minify", "-s", str(site), "--baseURL", data["base_url"], "--destination", str(public)], check=False)
    if getattr(r, "returncode", 0) != 0:
        return 1
    chk = run([sys.executable, str(ROOT / "scripts" / "check_file_count.py"), str(public)], check=False)
    return getattr(chk, "returncode", 0), counts


def main(argv=None, run=subprocess.run) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", required=True)
    ap.add_argument("--site", default=str(ROOT / "site"))
    ap.add_argument("--hugo", default="hugo")
    ap.add_argument("--manifest", default=None)
    a = ap.parse_args(argv)
    gen = _load("gen_site_config")
    brain, site = Path(a.brain), Path(a.site)
    data = gen.load_site_cfg(brain)
    manifest, failed = [], 0
    if data["sites"]:
        for lang in data["languages"]:
            d = gen.target_data(data, lang)
            if not d.get("analytics_token"):                       # token saved by scripts/ensure_analytics.py (optional)
                try:
                    d["analytics_token"] = json.loads((site / "data" / "analytics.json").read_text(encoding="utf-8")).get(lang, "")
                except (OSError, ValueError):
                    pass
            public = site / f"public-{lang}"
            res = build_one(gen, d, brain, site, public, a.hugo, run, data)
            code = res if isinstance(res, int) else res[0]
            print(f"build lang={lang} status={'ok' if code == 0 else 'fail'}" + (f" articles={res[1].get(lang)}" if isinstance(res, tuple) else ""))
            failed += code != 0
            manifest.append({"lang": lang, "public": str(public), "project": data["sites"][lang]["pages_project"]})
    else:
        public = site / "public"
        res = build_one(gen, data, brain, site, public, a.hugo, run)
        code = res if isinstance(res, int) else res[0]
        print(f"build site=all status={'ok' if code == 0 else 'fail'}")
        failed += code != 0
        manifest.append({"lang": "all", "public": str(public), "project": os.environ.get("PAGES_PROJECT", "")})
    out = Path(a.manifest or site / ".build-manifest.json")
    out.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
