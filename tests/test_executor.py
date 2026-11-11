import json
import os
from pathlib import Path

import pytest
import yaml
from git_helpers import git, make_repo
from plan_factory import good_plan, step
from typer.testing import CliRunner

from orch import budget, cli, workspace
from orch.adapters.base import AgentAdapter, AgentResult, ErrorKind
from orch.config import ResolvedRepo, default_config_path, load_config
from orch.checks import classify, is_self_hosting_repo, outside_plan
from orch.config import PostChecksConfig
from orch.executor import ExecError, Executor, builder_args, build_prompt
from orch.plan import Plan
from orch.runs import create_run, load_meta, write_meta

PASS_CMD = 'python -c "import sys; sys.exit(0)"'
FILE_CMD = 'python -c "import sys,pathlib; sys.exit(0 if pathlib.Path(\'ok.txt\').exists() else 1)"'


class Boom(Exception):
    pass


class FakeBuilder(AgentAdapter):
    """Each action is called with the AgentRequest, edits files in request.cwd and returns an AgentResult."""

    name = "claude"

    def __init__(self, actions):
        self.actions = list(actions)
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return self.actions.pop(0)(request)


def report(**over):
    r = {"summary": "did it", "files_changed": [], "tests_added": [], "blocked": False, "question": None, "notes": ""}
    r.update(over)
    return r


def edit(files=None, rep=None, tokens=100):
    def action(req):
        for name, content in (files or {}).items():
            p = req.cwd / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return AgentResult(
            agent="claude", ok=True, error_kind=ErrorKind.OK, structured=rep or report(), session_id="s",
            total_tokens=tokens,
        )  # fmt: skip

    return action


def fail(kind=ErrorKind.AGENT_ERROR):
    return lambda req: AgentResult(agent="claude", ok=False, error_kind=kind, text="nope")


def crash(files):
    def action(req):
        for name, content in files.items():
            (req.cwd / name).write_text(content, encoding="utf-8")
        raise Boom("simulated crash")

    return action


@pytest.fixture
def env(tmp_path):
    repo = make_repo(tmp_path / "proj")

    def setup(plan=None, test_command=PASS_CMD, name="demo"):
        run_dir = create_run(repo, "do the thing")
        plan = plan or good_plan(steps=[step("s1"), step("s2", deps=["s1"])])
        (run_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        write_meta(run_dir, {"run_id": run_dir.name, "status": "planned", "total_tokens": 0})
        rr = ResolvedRepo(name=name, path=repo, test_command=test_command)
        return run_dir, rr

    def executor(run_dir, rr, actions, logs=None, **cfg_over):
        fb = FakeBuilder(actions)
        over = {"review": "off", "max_repair_rounds": 0, **cfg_over}  # Phase 3 behaviour unless a test opts in
        cfg = load_config().model_copy(update=over)
        ex = Executor(rr, run_dir, cfg, lambda n: fb, log=(logs.append if logs is not None else lambda m: None))
        return ex, fb

    return repo, setup, executor


def test_two_steps_pass_commit_and_stay_outside_repo(env, isolated_worktree_root):
    repo, setup, executor = env
    run_dir, rr = setup()
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"}), edit({"src/s2.py": "b = 2\n"})])
    out = ex.start()
    assert out.status == "done"
    meta = load_meta(run_dir)
    assert meta["status"] == "done" and meta["exec_tokens"] == 200
    s1, s2 = meta["steps"]["s1"], meta["steps"]["s2"]
    assert s1["status"] == s2["status"] == "done"
    assert s1["tests"]["passed"] and s1["tests"]["exit_code"] == 0 and s1["tokens"] == 100
    assert s1["commit"] != s2["commit"]
    wt = isolated_worktree_root / "demo" / run_dir.name
    assert meta["exec"]["worktree"] == str(wt) and wt.is_dir()
    assert repo.resolve() not in wt.resolve().parents
    log = git(wt, "log", "--format=%s", f"{meta['exec']['base_sha']}..HEAD").splitlines()
    assert log == ["orch(s2): Do s2", "orch(s1): Do s1"]
    assert git(repo, "branch", "--show-current").strip() == "main"
    assert not (repo / "src").exists()  # main checkout untouched
    assert (run_dir / "steps" / "s1" / "tests.txt").is_file()
    assert "+a = 1" in (run_dir / "steps" / "s1" / "diff.patch").read_text(encoding="utf-8")
    assert fb.requests[0].cwd == wt
    # the second prompt lists the first step as done
    assert "Do s1" in fb.requests[1].prompt.split("Already done")[1]


def test_builder_permissions_are_exact(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=PASS_CMD)
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "x\n"})])
    ex.start()
    sent = fb.requests[0].extra_args
    assert sent[sent.index("--allowedTools") + 1].split(",")[5:] == [f"Bash({PASS_CMD})"]
    a = builder_args("uv run pytest -q", "ROLE")  # the realistic config command, no process spawned
    assert a[a.index("--append-system-prompt") + 1] == "ROLE"
    assert a[a.index("--permission-prompts") + 1] == "none"
    assert "--restricted" in a and "--strict-mcp-config" in a
    assert a[a.index("--tools") + 1] == "Read,Grep,Glob,Edit,Write,Bash"
    allowed = a[a.index("--allowedTools") + 1].split(",")
    assert allowed[:5] == ["Read", "Grep", "Glob", "Edit", "Write"]
    assert allowed[5:] == ["Bash(uv run pytest -q)"]
    denied = a[a.index("--disallowedTools") + 1]
    for d in ("Bash(git *)", "Bash(uv add*)", "Bash(pip *)"):
        assert d in denied
    assert "python -c" not in a[a.index("--allowedTools") + 1]
    assert "--max-turns" not in a and "--max-budget-usd" not in sent
    assert fb.requests[0].timeout_s == 20 * 60 and fb.requests[0].model == "sonnet"


def test_plan_test_command_is_never_used(env):
    repo, setup, executor = env
    plan = good_plan(test_command="git push", steps=[step("s1")])
    run_dir, rr = setup(plan=plan)
    logs = []
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "x\n"})], logs)
    assert ex.start().status == "done"
    req = fb.requests[0]
    assert "git push" not in " ".join(req.extra_args) and "git push" not in req.prompt
    assert PASS_CMD in req.prompt
    tests_txt = (run_dir / "steps" / "s1" / "tests.txt").read_text(encoding="utf-8")
    assert tests_txt.startswith(f"$ {PASS_CMD}") and "git push" not in tests_txt
    assert any("differs from config" in m for m in logs)
    assert load_meta(run_dir)["exec"]["warnings"]


def test_refuses_without_configured_test_command(env):
    repo, setup, executor = env
    run_dir, rr = setup(test_command=None)
    ex, fb = executor(run_dir, rr, [])
    with pytest.raises(ExecError, match="test_command"):
        ex.start()
    assert "exec" not in load_meta(run_dir) and fb.requests == []


def test_dirty_repo_is_refused(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    ex, fb = executor(run_dir, rr, [])
    with pytest.raises(workspace.WorkspaceError, match="uncommitted"):
        ex.start()
    assert fb.requests == []


def test_risk_flag_stops_before_running_the_builder(env):
    repo, setup, executor = env
    plan = good_plan(steps=[step("s1"), step("s2", deps=["s1"], flags=["db_migration_or_schema"])])
    run_dir, rr = setup(plan=plan)
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "x\n"}), edit({"src/s2.py": "y\n"})])
    out = ex.start()
    assert out.status == "awaiting_approval" and out.step_id == "s2"
    assert len(fb.requests) == 1  # builder never called for s2
    meta = load_meta(run_dir)
    assert meta["steps"]["s1"]["status"] == "done"
    assert meta["steps"]["s2"]["status"] == "awaiting_approval" and meta["status"] == "awaiting_approval"
    # resume does not skip the approval gate
    ex2, fb2 = executor(run_dir, rr, [edit({})])
    assert ex2.resume().status == "awaiting_approval" and fb2.requests == []


@pytest.mark.parametrize("path", [".env", ".env.local", "keys/server.pem", "id.key", "deploy/id_rsa.pub", ".npmrc",
                                   "config/secrets.yaml", "src/auth-token.py"])  # fmt: skip
def test_protected_path_stops_with_awaiting_approval(env, path):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, fb = executor(run_dir, rr, [edit({path: "x\n", "src/s1.py": "ok\n"})])
    out = ex.start()
    assert out.status == "awaiting_approval"
    s = load_meta(run_dir)["steps"]["s1"]
    assert s["status"] == "awaiting_approval" and s["rule"] == "touches_auth_payments_secrets_env"
    assert s["commit"] is None and s["tests"] is None  # tests never ran, nothing committed
    assert not (run_dir / "steps" / "s1" / "tests.txt").exists()


@pytest.mark.parametrize("name", ["pyproject.toml", "uv.lock", "requirements-dev.txt", "web/package.json", "composer.lock"])
def test_dependency_file_stops(env, name):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, _ = executor(run_dir, rr, [edit({name: "x\n"})])
    out = ex.start()
    assert out.status == "awaiting_approval"
    s = load_meta(run_dir)["steps"]["s1"]
    assert s["rule"] == "new_dependency" and s["commit"] is None


PC = PostChecksConfig()


def rules(path, selfhost=False):
    return classify(path, PC, selfhost)


def test_default_protected_paths():
    for p in (".env", "x/.env.prod", "a.pem", "b/c.key", "c.pfx", "d.p12", "ssh/id_rsa", "id_rsa.pub", ".npmrc",
              "my_secret.txt", "Secrets.yaml", "src/auth-token.py", "pw/password.txt", "credentials.json", "A\\TOKEN.md",
              "src/auth/login.py", "auth.py", "auth_utils.py", "src/Payment.py", "payment_service.py", "billing/payments.py"):
        assert "touches_auth_payments_secrets_env" in rules(p), p
    for p in ("src/app.py", "README.md", "tests/test_x.py", "environment.md", "authors.md", "secretary.md", "tokenizer.py",
              "author.py", "oauthlib.py", "paymentsx.md"):
        assert "touches_auth_payments_secrets_env" not in rules(p), p


def test_default_executes_later_dependency_and_migration():
    for p in (".claude/settings.json", "a/CLAUDE.md", "AGENTS.md", ".mcp.json", ".github/workflows/ci.yml", ".githooks/pre-commit",
              ".husky/pre-commit", "Makefile", "tests/conftest.py", "pytest.ini", "tox.ini", "setup.cfg", ".gitattributes",
              ".gitmodules"):
        assert "executes_later" in rules(p), p
    for p in ("pyproject.toml", "sub/uv.lock", "requirements.txt", "requirements-dev.txt", "package.json", "package-lock.json",
              "pnpm-lock.yaml", "yarn.lock", "composer.json", "composer.lock"):
        assert "new_dependency" in rules(p), p
    for p in ("app/migrations/0001_init.py", "database/migrations/x.php", "alembic/env.py"):
        assert "db_migration_or_schema" in rules(p), p
    assert rules("README.md") == [] and rules("src/app.py") == []
    assert "executes_later" not in rules("docs/CLAUDE.mdx")


def test_self_hosting_rule_only_applies_to_orch_itself(tmp_path):
    assert "self_hosting" not in rules("src/orch/executor.py") and "self_hosting" not in rules("config.yaml")
    assert "self_hosting" in rules("src/orch/executor.py", True) and "self_hosting" in rules("config.yaml", True)
    assert not is_self_hosting_repo(tmp_path)
    assert is_self_hosting_repo(Path(__file__).resolve().parents[1])


def test_patterns_come_from_config_and_case_is_ignored_on_windows():
    cfg = PostChecksConfig(protected=["vault/**"], protected_words=[], executes_later=[], dependency=[], migration=[])
    assert classify("vault/a.txt", cfg) == ["touches_auth_payments_secrets_env"] and classify(".env", cfg) == []
    if os.name == "nt":
        assert "executes_later" in rules("MAKEFILE") and "executes_later" in rules(".Claude\\Settings.json")


def test_out_of_plan_files_warn_but_continue(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1", files=["src/s1.py"])]))
    logs = []
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n", "extra.py": "b\n"}, rep=report(files_changed=["src/s1.py"]))], logs)
    assert ex.start().status == "done"
    s = load_meta(run_dir)["steps"]["s1"]
    assert any("extra.py" in w for w in s["warnings"]) and not any("src/s1.py" in w for w in s["warnings"])
    assert any(m.startswith("warning [s1]") for m in logs)
    assert outside_plan(["a/b.py", "docs/x.md"], ["a/", "docs/x.md"]) == []
    # dotfiles are not mangled (lstrip("./") used to turn .gitignore into gitignore); ./ prefixes are removed
    assert outside_plan([".gitignore", "src/a.py"], [".gitignore", "./src/a.py"]) == []
    assert outside_plan(["ab/c.py"], ["a/"]) == ["ab/c.py"]
    if os.name == "nt":
        assert outside_plan(["SRC\\A.py"], ["src/a.py"]) == []


def test_changed_files_come_from_git_not_from_the_report(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    # the builder lies: claims an innocent file but writes .env
    ex, _ = executor(run_dir, rr, [edit({".env": "K=1\n"}, rep=report(files_changed=["src/s1.py"]))])
    assert ex.start().status == "awaiting_approval"


def test_tests_fail_stops_and_saves_output(env):
    repo, setup, executor = env
    run_dir, rr = setup(test_command=FILE_CMD)
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "x\n"}), edit({"ok.txt": "1"})])
    out = ex.start()
    assert out.status == "awaiting_approval" and out.step_id == "s1"  # no repair rounds allowed here
    assert len(fb.requests) == 1  # s2 not attempted
    meta = load_meta(run_dir)
    s1 = meta["steps"]["s1"]
    assert s1["status"] == "awaiting_approval" and s1["tests"]["exit_code"] == 1 and not s1["tests"]["passed"]
    assert s1["stage"] == "repairs_exhausted" and s1["rule"] == "tests_fail_after_3_repairs"
    assert s1["commit"] is None and meta["status"] == "awaiting_approval" and meta["steps"]["s2"]["status"] == "pending"
    assert "[exit 1]" in (run_dir / "steps" / "s1" / "tests.txt").read_text(encoding="utf-8")


def test_pass_fail_is_decided_by_exit_code_not_by_the_report(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=FILE_CMD)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "x\n"}, rep=report(summary="all tests pass!", notes="green"))])
    assert ex.start().status == "awaiting_approval"  # tests failed; the report claim is ignored


def test_blocked_builder_sets_needs_input(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    ex, fb = executor(run_dir, rr, [edit(rep=report(blocked=True, question="Which db?"))])
    out = ex.start()
    assert out.status == "needs_input" and "Which db?" in out.message
    meta = load_meta(run_dir)
    assert meta["steps"]["s1"]["status"] == "needs_input" and meta["steps"]["s1"]["question"] == "Which db?"
    assert meta["status"] == "needs_input" and len(fb.requests) == 1


def test_builder_error_fails_the_step(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    ex, _ = executor(run_dir, rr, [fail(ErrorKind.TIMEOUT)])
    assert ex.start().status == "failed"
    assert load_meta(run_dir)["steps"]["s1"]["status"] == "failed"


def test_rate_limited_pauses_and_resume_continues(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), fail(ErrorKind.RATE_LIMITED)])
    out = ex.start()
    assert out.status == "paused" and out.step_id == "s2"
    meta = load_meta(run_dir)
    assert meta["status"] == "paused" and meta["steps"]["s1"]["status"] == "done" and meta["steps"]["s2"]["status"] == "pending"
    assert load_meta(run_dir)["resume_after"]
    budget.clear_cooldowns()  # the limit has "reset"
    ex2, fb2 = executor(run_dir, rr, [edit({"src/s2.py": "b\n"})])
    assert ex2.resume().status == "done"
    assert "resume_after" not in load_meta(run_dir)
    assert len(fb2.requests) == 1  # s1 not repeated
    assert load_meta(run_dir)["status"] == "done"


def test_resume_after_crash_resets_partial_edits(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), crash({"partial.py": "half\n"})])
    with pytest.raises(Boom):
        ex.start()
    meta = load_meta(run_dir)  # what a crashed process leaves on disk
    assert meta["steps"]["s1"]["status"] == "done" and meta["steps"]["s2"]["status"] == "running"
    wt = meta["exec"]["worktree"]
    assert (workspace.Path(wt) / "partial.py").exists()
    ex2, fb2 = executor(run_dir, rr, [edit({"src/s2.py": "b\n"})])
    assert ex2.resume().status == "done"
    assert not (workspace.Path(wt) / "partial.py").exists()
    assert len(fb2.requests) == 1


def test_resume_does_not_auto_continue_a_stopped_step(env):
    repo, setup, executor = env
    run_dir, rr = setup(test_command=FILE_CMD)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "x\n"})])
    assert ex.start().status == "awaiting_approval"
    ex2, fb2 = executor(run_dir, rr, [])
    assert ex2.resume().status == "awaiting_approval" and fb2.requests == []


def test_steps_run_in_topological_order(env):
    repo, setup, executor = env
    plan = good_plan(steps=[step("s2", deps=["s1"]), step("s1")])
    run_dir, rr = setup(plan=plan)
    ex, fb = executor(run_dir, rr, [edit({"a.py": "1\n"}), edit({"b.py": "2\n"})])
    assert ex.start().status == "done"
    assert "[s1]" in fb.requests[0].prompt and "[s2]" in fb.requests[1].prompt


def test_start_twice_is_refused(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, _ = executor(run_dir, rr, [edit({"a.py": "1\n"})])
    ex.start()
    ex2, _ = executor(run_dir, rr, [])
    with pytest.raises(ExecError, match="resume"):
        ex2.start()


def test_build_prompt_contents():
    plan = Plan.model_validate(good_plan(steps=[step("s1"), step("s2", deps=["s1"])]))
    p = build_prompt(plan, plan.steps[1], ["Do s1"], "pytest -q")
    for needle in ("Add a version command", "importlib.metadata", "[s2] Do s2", "src/s2.py", "tests for s2 pass", "- Do s1", "pytest -q"):
        assert needle in p


# ---- CLI ----


def test_cli_exec_status_diff_clean(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    data = yaml.safe_load(open(default_config_path(), encoding="utf-8"))
    data["repos"] = {"demo": {"path": str(repo), "test_command": PASS_CMD}}
    data.update(review="off", max_repair_rounds=0)
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("ORCH_CONFIG", str(cfgfile))
    run_dir, _ = setup()
    fb = FakeBuilder([edit({"src/s1.py": "a = 1\n"}), edit({"src/s2.py": "b = 2\n"})])
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    runner = CliRunner()
    r = runner.invoke(cli.app, ["exec", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 0, r.output
    assert "Result: done" in r.output
    r = runner.invoke(cli.app, ["status", run_dir.name, "--repo", "demo"])
    assert "s1" in r.output and "pass" in r.output and "done" in r.output
    r = runner.invoke(cli.app, ["diff", run_dir.name, "--repo", "demo"])
    assert "+a = 1" in r.output and "+b = 2" in r.output
    r = runner.invoke(cli.app, ["diff", run_dir.name, "--step", "s2", "--repo", "demo"])
    assert "+b = 2" in r.output and "+a = 1" not in r.output
    wt = load_meta(run_dir)["exec"]["worktree"]
    r = runner.invoke(cli.app, ["clean", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 0 and "branch" in r.output
    assert not workspace.Path(wt).exists()
    assert "orch/" in git(repo, "branch", "--list", "orch/*")
    r = runner.invoke(cli.app, ["diff", run_dir.name, "--repo", "demo"])
    assert "+a = 1" in r.output  # still viewable after clean


def test_cli_exit_codes(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    data = yaml.safe_load(open(default_config_path(), encoding="utf-8"))
    data["repos"] = {"demo": {"path": str(repo), "test_command": PASS_CMD}}
    data.update(review="off", max_repair_rounds=0)
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("ORCH_CONFIG", str(cfgfile))
    run_dir, _ = setup()
    fb = FakeBuilder([fail(ErrorKind.RATE_LIMITED)])
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    r = CliRunner().invoke(cli.app, ["exec", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 3 and "paused" in r.output


def test_a_commit_made_by_the_agent_stops_the_step(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))

    def sneaky(req):
        r = edit({"src/s1.py": "a = 1\n"})(req)
        git(req.cwd, "add", "-A")
        git(req.cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "sneaky")
        return r

    ex, fb = executor(run_dir, rr, [sneaky])
    out = ex.start()
    assert out.status == "failed"
    st = load_meta(run_dir)["steps"]["s1"]
    assert st["status"] == "failed" and st["stage"] == "git_state_changed" and st["commit"] is None
