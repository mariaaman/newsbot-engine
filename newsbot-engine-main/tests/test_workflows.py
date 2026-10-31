"""M6: lint every workflow with PyYAML (no network, no Actions runner), and exercise scripts/brain_commit.sh against
real local git repos with a stubbed `gh`."""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from tests.helpers import ROOT

WF_DIR = ROOT / ".github" / "workflows"
FILES = sorted(WF_DIR.glob("*.yml"))
SECRET_NAME = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD)", re.I)
COLLECT_ENV = "ALL_SECRETS_JSON"
TAG = re.compile(r"^v\d+(\.\d+){0,2}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
HARDCODED = re.compile(r"(ghp_[A-Za-z0-9]{20,}|github_pat_\w{20,}|gsk_\w{20,}|AIza[\w-]{20,}|cfut_\w{20,})")


def load(path):
    wf = yaml.safe_load(path.read_text(encoding="utf-8"))
    wf["on"] = wf.get("on", wf.get(True))            # PyYAML (YAML 1.1) reads the key `on` as True
    return wf


def steps(wf):
    return [s for j in wf["jobs"].values() for s in j["steps"]]


def run_scripts(wf):
    return [s["run"] for s in steps(wf) if "run" in s]


def test_expected_workflows_exist_and_parse():
    assert {f.name for f in FILES} >= {"ci.yml", "hourly.yml", "setup.yml", "keepalive.yml"}
    for f in FILES:
        assert load(f)["jobs"]


def test_every_workflow_declares_permissions_concurrency_and_timeout():
    for f in FILES:
        wf = load(f)
        assert isinstance(wf.get("permissions"), dict), f.name
        assert wf["concurrency"]["group"], f.name
        for name, job in wf["jobs"].items():
            assert job["timeout-minutes"] == (30 if f.name == "hourly.yml" else 15), (f.name, name)   # hourly: rate-limit waits
    assert load(WF_DIR / "hourly.yml")["permissions"] == {"contents": "read"}
    assert load(WF_DIR / "setup.yml")["permissions"] == {"contents": "read"}
    assert load(WF_DIR / "hourly.yml")["concurrency"]["cancel-in-progress"] is False   # runs never overlap or get cut off


def test_triggers():
    hourly = load(WF_DIR / "hourly.yml")["on"]
    assert hourly["schedule"][0]["cron"].split()[1:] == ["*", "*", "*", "*"] and "workflow_dispatch" in hourly
    assert "workflow_dispatch" in load(WF_DIR / "setup.yml")["on"]
    assert load(WF_DIR / "keepalive.yml")["on"]["schedule"][0]["cron"].split()[2] == "1"      # monthly
    ci = load(WF_DIR / "ci.yml")["on"]
    assert "pull_request" in ci


def test_actions_are_pinned_tag_with_verify_or_sha():
    for f in FILES:
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(?:-\s*)?uses:\s*(\S+)(.*)", line)
            if not m:
                continue
            ref = m.group(1).split("@", 1)[1] if "@" in m.group(1) else ""
            assert SHA.match(ref) or TAG.match(ref), (f.name, line)
            if TAG.match(ref):
                assert "VERIFY" in line, (f.name, line)      # tags are mutable until pinned to a SHA


def test_secrets_only_through_secrets_context_and_never_echoed():
    for f in FILES:
        text = f.read_text(encoding="utf-8")
        assert not HARDCODED.search(text), f.name
        wf = load(f)
        for s in steps(wf):
            for k, v in (s.get("env") or {}).items():
                if k == COLLECT_ENV:                      # the ONE exception: the step that collects the dashboard's LLM accounts
                    assert v == "${{ toJSON(secrets) }}" and s.get("run") == "python scripts/collect_llm_accounts.py" and s.get("name") == "Collect LLM accounts", (f.name, k)
                    continue
                if SECRET_NAME.search(k):
                    assert re.fullmatch(r"\$\{\{ secrets\.[A-Z_]+ \}\}", str(v)), (f.name, k)
            for k, v in (s.get("with") or {}).items():
                if k.lower() in ("token", "password", "secret", "api-key", "api_key", "ssh-key"):   # not actions/cache `key`
                    assert re.fullmatch(r"\$\{\{ secrets\.[A-Z_]+ \}\}", str(v)), (f.name, k)
        for script in run_scripts(wf):
            assert "${{" not in script, (f.name, "expression inside run: pass values through env instead")
            assert "secrets." not in script
            for line in script.splitlines():
                if re.match(r"\s*(echo|printf|env|printenv|set -x|cat)\b", line):
                    assert not re.search(r"\$\{?\w*(TOKEN|KEY|SECRET|PASSWORD)", line, re.I), (f.name, line)
            assert not re.search(r"\b(printenv|set -x)\b", script)


def test_hourly_flow_order_and_guards():
    wf = load(WF_DIR / "hourly.yml")
    st = steps(wf)
    cmds = [s.get("run", "") + " " + s.get("uses", "") for s in st]

    def idx(needle):
        return next(i for i, c in enumerate(cmds) if needle in c)

    order = [idx("-m newsbot run"), idx("brain_commit.sh brain \"$MODE\""), idx("deploy-needed"),
             idx("--sync-content"), idx("check_file_count.py"), idx("wrangler"), idx("mark-deployed")]
    assert order == sorted(order) and len(set(order)) == len(order)
    plan = next(s for s in st if s.get("id") == "plan")
    for needle in ("--sync-content", "check_file_count.py", "wrangler", "mark-deployed", "actions-hugo"):
        s = st[idx(needle)]
        assert s["if"] == "steps.plan.outputs.deploy == 'true'", needle      # nothing changed -> no Hugo, no deploy
        assert not s.get("continue-on-error")                                # a file-count failure blocks the deploy
    for needle in ("brain_commit.sh brain \"$MODE\"", "deploy-needed"):
        assert st[idx(needle)]["if"] == "env.MODE != 'dry-run'"
    assert "env.MODE" in plan["if"] and wf["jobs"]["run"]["env"]["MODE"].endswith("'review' }}")   # default is review
    brain_co = next(s for s in st if s.get("with", {}).get("path") == "brain")
    assert "vars.BRAIN_REPO" in brain_co["with"]["repository"] and "secrets.BRAIN_REPO_TOKEN" in brain_co["with"]["token"]
    assert next(s for s in st if "-m newsbot run" in s.get("run", ""))["run"].count("--brain brain") == 1


def test_hourly_secrets_are_scoped_to_the_steps_that_need_them():
    wf = load(WF_DIR / "hourly.yml")
    assert not any(SECRET_NAME.search(k) for k in wf["jobs"]["run"]["env"])      # no secrets at job level
    for s in steps(wf):
        env = s.get("env") or {}
        assert not any(k in env for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"))
        if "CLOUDFLARE_API_TOKEN" in env and "wrangler" not in s.get("run", ""):
            assert "-m newsbot run" in s["run"]


def test_setup_workflow_runs_the_checks_and_prints_status_only():
    wf = load(WF_DIR / "setup.yml")
    scripts = " ".join(run_scripts(wf))
    for needle in ("scripts/setup_check.py", "-m newsbot validate", "-m newsbot health"):
        assert needle in scripts
    brain_co = next(s for s in steps(wf) if s.get("with", {}).get("path") == "brain")
    assert brain_co["continue-on-error"] is True                               # a failed checkout is reported as FAIL


def test_only_keepalive_and_the_two_helper_workflows_write():
    for f in FILES:
        perms = load(f)["permissions"]
        assert (perms.get("contents") == "write") == (f.name in ("keepalive.yml", "unzip.yml", "pin-actions.yml")), f.name


def test_ci_builds_hugo_and_runs_pytest():
    scripts = " ".join(run_scripts(load(WF_DIR / "ci.yml")))
    assert "pytest" in scripts and "hugo --minify" in scripts and "bash -n scripts/brain_commit.sh" in scripts


# ---- brain_commit.sh against real git repos (gh is stubbed; a real PR cannot be opened here)
def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout


def _setup(tmp: Path):
    origin, brain, bindir = tmp / "origin.git", tmp / "brain", tmp / "bin"
    _git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp, "clone", "-q", str(origin), str(brain))
    _git(brain, "checkout", "-q", "-b", "main")
    for d in ("state", "pending", "content/en"):
        (brain / d).mkdir(parents=True)
        (brain / d / ".gitkeep").write_text("")
    _git(brain, "add", "-A")
    _git(brain, "commit", "-q", "-m", "init")
    _git(brain, "push", "-q", "origin", "main")
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(f"#!/bin/sh\necho \"$@\" >> {tmp}/gh.log\necho https://example.invalid/pr/1\n")
    gh.chmod(0o755)
    return origin, brain, {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "GIT_AUTHOR_NAME": "t",
                           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _script(brain, mode, env):
    return subprocess.run(["bash", str(ROOT / "scripts" / "brain_commit.sh"), str(brain), mode], capture_output=True,
                          text=True, env=env)


def _write_run_output(brain):
    (brain / "state" / "stories.jsonl").write_text('{"story_id": "st-1"}\n')
    (brain / "pending" / "d.md").write_text("draft")
    (brain / "content" / "en" / "a.md").write_text("---\ntitle: a\n---\nbody\n")


def test_brain_commit_auto_puts_everything_on_main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        origin, brain, env = _setup(tmp)
        _write_run_output(brain)
        r = _script(brain, "auto", env)
        assert r.returncode == 0 and r.stdout.split() == ["committed"], r.stderr
        files = _git(origin, "ls-tree", "-r", "--name-only", "main").split()
        assert {"state/stories.jsonl", "pending/d.md", "content/en/a.md"} <= set(files)
        r2 = _script(brain, "auto", env)
        assert r2.stdout.split() == ["nothing-to-commit"]


def test_brain_commit_review_keeps_content_off_main_and_opens_a_pr():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        origin, brain, env = _setup(tmp)
        _write_run_output(brain)
        r = _script(brain, "review", env)
        assert r.returncode == 0 and r.stdout.split() == ["committed", "pr-opened"], r.stderr
        main_files = set(_git(origin, "ls-tree", "-r", "--name-only", "main").split())
        assert "state/stories.jsonl" in main_files and "pending/d.md" in main_files
        assert "content/en/a.md" not in main_files                       # not published until the PR is merged
        branches = _git(origin, "branch", "--list", "review/*").split()
        assert len(branches) == 1 and "content/en/a.md" in _git(origin, "ls-tree", "-r", "--name-only", branches[0]).split()
        assert _git(brain, "branch", "--show-current").strip() == "main"
        assert not (brain / "content" / "en" / "a.md").exists()          # the Hugo build sees main only
        log = (tmp / "gh.log").read_text()
        assert "pr create" in log and "--base main" in log
        assert "example.invalid" not in r.stdout                         # PR URL is never printed to the public log


def test_brain_commit_review_sees_modified_published_articles_and_keeps_the_old_text_on_main():
    """M7: an UPDATE rewrites an existing content file; in review mode the change goes to a PR, main keeps the old text."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        origin, brain, env = _setup(tmp)
        _write_run_output(brain)
        assert _script(brain, "auto", env).stdout.split() == ["committed"]
        (brain / "content" / "en" / "a.md").write_text("---\ntitle: a\n---\nbody UPDATED\n")      # modified, not new
        (brain / "state" / "stories.jsonl").write_text('{"story_id": "st-1", "updates": ["t"]}\n')
        r = _script(brain, "review", env)
        assert r.returncode == 0 and r.stdout.split() == ["committed", "pr-opened"], r.stderr
        assert "UPDATED" not in _git(origin, "show", "main:content/en/a.md")
        assert "updates" in _git(origin, "show", "main:state/stories.jsonl")        # state still goes straight to main
        branch = _git(origin, "branch", "--list", "review/*").split()[0]
        assert "UPDATED" in _git(origin, "show", f"{branch}:content/en/a.md")
        assert "UPDATED" not in (brain / "content" / "en" / "a.md").read_text()      # working tree is back on main


def test_brain_commit_state_mode_and_bad_mode():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        origin, brain, env = _setup(tmp)
        _write_run_output(brain)
        (brain / "state" / "deployed.json").write_text("{}")
        assert _script(brain, "state", env).stdout.split() == ["committed"]
        main_files = set(_git(origin, "ls-tree", "-r", "--name-only", "main").split())
        assert "state/deployed.json" in main_files and "content/en/a.md" not in main_files
        assert _script(brain, "nope", env).returncode == 2


def test_brain_commit_rebases_when_main_moved():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        origin, brain, env = _setup(tmp)
        other = tmp / "other"                                            # owner merges something while the run is going
        _git(tmp, "clone", "-q", str(origin), str(other))
        (other / "content" / "en" / "owner.md").write_text("---\ntitle: o\n---\nx\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "owner")
        _git(other, "push", "-q", "origin", "main")
        _write_run_output(brain)
        r = _script(brain, "auto", env)
        assert r.returncode == 0 and r.stdout.split() == ["committed"], r.stderr
        files = set(_git(origin, "ls-tree", "-r", "--name-only", "main").split())
        assert {"content/en/owner.md", "content/en/a.md", "state/stories.jsonl"} <= files


def test_llm_accounts_are_collected_once_before_every_step_that_calls_the_llm_and_never_exported_as_values():
    for name in ("hourly.yml", "setup.yml", "readiness.yml", "llm-check.yml"):
        st = steps(load(WF_DIR / name))
        collect = next(i for i, s in enumerate(st) if s.get("id") == "llm_accounts")
        users = [i for i, s in enumerate(st) if any(c in s.get("run", "") for c in ("-m newsbot run", "-m newsbot health", "-m newsbot llmtest", "-m newsbot llmcheck", "setup_check.py"))]
        assert users and all(collect < i for i in users), name
        assert sum(1 for s in st if "ALL_SECRETS_JSON" in (s.get("env") or {})) == 1, name          # the whole secrets context reaches ONE step only
    text = (ROOT / "scripts" / "collect_llm_accounts.py").read_text(encoding="utf-8")
    assert "NEWSBOT_CF_ACCOUNTS_FILE=" in text and "NEWSBOT_CF_ACCOUNTS=" not in text             # a file path goes to later steps, never the values


def test_llm_check_workflow_saves_its_result_to_the_brain_and_fails_when_an_account_fails():
    wf = load(WF_DIR / "llm-check.yml")
    st = steps(wf)
    assert wf["on"] == {"workflow_dispatch": None} or "workflow_dispatch" in wf["on"]
    assert any("brain_commit.sh brain state" in s.get("run", "") and s.get("if") == "always()" for s in st)
    assert next(s for s in st if s.get("id") == "check").get("continue-on-error") is True
    assert any('test "$CHECK_OUTCOME" = "success"' in s.get("run", "") for s in st)
