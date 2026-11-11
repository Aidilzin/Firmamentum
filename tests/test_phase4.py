"""Phase 4: reviewer, repair loop, approvals, answers, merge. FakeAdapter-style fakes only; no real CLI."""

import json
import os
from pathlib import Path

import pytest
import yaml
from git_helpers import git
from plan_factory import good_plan, step
from pydantic import ValidationError
from test_executor import FILE_CMD, PASS_CMD, FakeBuilder, edit, env, fail, report  # noqa: F401  (env is a fixture)
from typer.testing import CliRunner

from orch import budget, cli, review as review_mod, workspace
from orch.adapters import AntigravityAdapter, ClaudeCodeAdapter, AgentRequest
from orch.adapters.base import AgentResult, ErrorKind
from orch.adapters.process import child_env
from orch.config import ResolvedRepo, default_config_path, load_config
from orch.executor import ExecError, Executor, repair_args, reviewer_args
from orch.merge import MergeError, merge_run
from orch.runs import load_meta
from orch.review import Issue, Review

ON = {"review": "per_step", "max_repair_rounds": 3}


def issue(severity="major", file="src/s1.py", line=3, problem="bug", fix="fix it"):
    return {"severity": severity, "file": file, "line": line, "problem": problem, "fix": fix}


def review(verdict="approve", issues=None, session="rs", tokens=50):
    def action(req):
        return AgentResult(
            agent="claude", ok=True, error_kind=ErrorKind.OK, session_id=session, total_tokens=tokens,
            structured={"verdict": verdict, "issues": issues or [], "summary": "sum"},
        )  # fmt: skip

    return action


def raw_review(structured):
    return lambda req: AgentResult(agent="claude", ok=True, error_kind=ErrorKind.OK, structured=structured,
                                   session_id="rs", total_tokens=50)  # fmt: skip


def delete(*paths):
    def action(req):
        for p in paths:
            (req.cwd / p).unlink()
        return AgentResult(agent="claude", ok=True, error_kind=ErrorKind.OK, structured=report(), session_id="s",
                           total_tokens=100)  # fmt: skip

    return action


def is_review_call(req):
    return "Read,Grep,Glob" in req.extra_args and "Edit" not in ",".join(req.extra_args)


# ---------- review model ----------


def test_review_verdict_must_match_severity():
    ok = Review(verdict="reject", issues=[Issue(**issue("blocker"))], summary="x")
    assert ok.blocking_issues
    Review(verdict="approve", issues=[Issue(**issue("minor"))], summary="x")
    Review(verdict="approve", issues=[], summary="x")
    for verdict, sev in (("approve", "blocker"), ("approve", "major"), ("reject", "minor")):
        with pytest.raises(ValidationError):
            Review(verdict=verdict, issues=[Issue(**issue(sev))], summary="x")
    with pytest.raises(ValidationError):
        Review(verdict="reject", issues=[], summary="x")
    with pytest.raises(ValidationError):
        Issue(**issue("catastrophic"))
    Issue(**issue(line=None))


def test_review_schema_file_matches_model():
    assert review_mod.load_schema_file() == review_mod.review_json_schema()


# ---------- permissions ----------


def test_reviewer_and_repair_permissions_are_exact(tmp_path):
    a, inline = reviewer_args("claude", "ROLE", tmp_path)
    assert inline is None and "--restricted" in a and "--strict-mcp-config" in a
    assert a[a.index("--tools") + 1] == "Read,Grep,Glob" and a[a.index("--permission-prompts") + 1] == "none"
    assert a[a.index("--add-dir") + 1] == str(tmp_path) and a[a.index("--append-system-prompt") + 1] == "ROLE"
    assert "--allowedTools" not in a and "Edit" not in ",".join(a) and "Bash" not in ",".join(a)
    a, inline = reviewer_args("agy", "ROLE", tmp_path)
    assert a[:3] == ["--mode", "plan", "--sandbox"] and inline == "ROLE" and "--append-system-prompt" not in a

    r = repair_args("uv run pytest -q", "uv run pytest -q --lf", "ROLE", tmp_path / "round1")
    allowed = r[r.index("--allowedTools") + 1].split(",")
    assert allowed[5:] == ["Bash(uv run pytest -q)", "Bash(uv run pytest -q --lf)"]
    assert r[r.index("--tools") + 1] == "Read,Grep,Glob,Edit,Write,Bash"
    assert "--restricted" in r and "--strict-mcp-config" in r
    assert "Bash(git *)" in r[r.index("--disallowedTools") + 1]
    assert r[r.index("--add-dir") + 1] == str(tmp_path / "round1") and r.count("--add-dir") == 1


# ---------- review + repair flow ----------


def test_review_approve_path(env, isolated_worktree_root):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"}), review("approve", [issue("minor")])], **ON)
    assert ex.start().status == "done"
    meta = load_meta(run_dir)
    s1 = meta["steps"]["s1"]
    assert s1["status"] == "done" and s1["commit"] and s1["rounds_total"] == 0
    assert s1["reviews"][0]["verdict"] == "approve" and s1["reviews"][0]["issues"][0]["severity"] == "minor"
    assert s1["roles_tokens"] == {"builder": 100, "reviewer": 50} and meta["role_tokens"]["reviewer"] == 50
    req = fb.requests[1]
    sdir = (run_dir / "steps" / "s1").resolve()
    assert req.cwd == Path(meta["exec"]["worktree"]) and req.model == "opus"
    assert req.extra_args[req.extra_args.index("--add-dir") + 1] == str(sdir)
    assert str(sdir / "diff.patch") in req.prompt and "+a = 1" not in req.prompt  # diff goes by file, not argv/prompt
    assert req.json_schema == review_mod.review_json_schema()
    assert "+a = 1" in (sdir / "diff.patch").read_text(encoding="utf-8")


def test_empty_diff_skips_review(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, fb = executor(run_dir, rr, [edit({})], **ON)
    assert ex.start().status == "done" and len(fb.requests) == 1


def test_review_reject_repair_then_approve(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, fb = executor(
        run_dir, rr,
        [edit({"src/s1.py": "a = 1\n"}), review("reject", [issue("major")]), edit({"src/s1.py": "a = 2\n"}), review("approve")],
        **ON,
    )  # fmt: skip
    assert ex.start().status == "done"
    s1 = load_meta(run_dir)["steps"]["s1"]
    assert s1["rounds_used"] == 1 and s1["rounds"][0]["reasons"] == ["review"] and s1["rounds"][0]["session"] == "resumed"
    assert [r["verdict"] for r in s1["reviews"]] == ["reject", "approve"] and s1["rounds"][0]["verdict"] == "approve"
    repair = fb.requests[2]
    assert repair.resume_id == "s"  # builder's session
    assert "[major] src/s1.py:3: bug -> fix: fix it" in repair.prompt
    rin = (run_dir / "steps" / "s1" / "repair_input" / "round1").resolve()
    assert repair.extra_args[repair.extra_args.index("--add-dir") + 1] == str(rin)
    assert (rin / "diff.patch").is_file() and (rin / "test_tail.txt").is_file()
    assert json.loads((rin / "review_issues.json").read_text(encoding="utf-8"))[0]["severity"] == "major"
    assert str(run_dir.resolve() / "steps" / "s1") not in " ".join(repair.extra_args).replace(str(rin), "")
    wt = Path(load_meta(run_dir)["exec"]["worktree"])
    assert (wt / "src" / "s1.py").read_text(encoding="utf-8") == "a = 2\n"
    assert git(wt, "log", "--format=%s", "-1").strip() == "orch(s1): Do s1"


def test_test_fail_repair_pass_with_retest(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=FILE_CMD)
    rr = rr.model_copy(update={"retest_command": PASS_CMD})  # quick command passes; only the full one gates
    ex, fb = executor(
        run_dir, rr,
        [edit({"src/s1.py": "a = 1\n"}), edit({"src/s1.py": "a = 2\n"}), edit({"ok.txt": "1"})],
        review="off", max_repair_rounds=3,
    )  # fmt: skip
    assert ex.start().status == "done"
    s1 = load_meta(run_dir)["steps"]["s1"]
    assert s1["rounds_used"] == 2 and s1["commit"]
    r1, r2 = s1["rounds"]
    assert r1["retest"]["passed"] and not r1["tests"]["passed"]  # retest passed but the full command failed
    assert r2["retest"]["passed"] and r2["tests"]["passed"]
    sdir = run_dir / "steps" / "s1"
    assert (sdir / "retest_r1.txt").is_file() and (sdir / "tests_r2.txt").is_file()
    assert "tests.txt" in (sdir / "tests.txt").name and not s1["tests"]["error"]
    assert "Quick re-test first: " + PASS_CMD in fb.requests[1].prompt
    assert f"Bash({PASS_CMD})" in fb.requests[1].extra_args[fb.requests[1].extra_args.index("--allowedTools") + 1]


def test_failing_retest_skips_full_run(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=FILE_CMD)
    rr = rr.model_copy(update={"retest_command": 'python -c "import sys; sys.exit(1)"'})
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"ok.txt": "1"})], review="off", max_repair_rounds=1)
    assert ex.start().status == "awaiting_approval"
    s1 = load_meta(run_dir)["steps"]["s1"]
    assert s1["rounds"][0]["retest"]["passed"] is False and s1["rounds"][0]["tests"] is None
    assert s1["commit"] is None  # the failing retest never reached the full gate


def test_rounds_exhausted_awaits_approval_and_commits_nothing(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=FILE_CMD)
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"x.py": "1\n"}), edit({"x.py": "2\n"})],
                      review="off", max_repair_rounds=2)  # fmt: skip
    out = ex.start()
    assert out.status == "awaiting_approval" and len(fb.requests) == 3
    meta = load_meta(run_dir)
    s1 = meta["steps"]["s1"]
    assert s1["stage"] == "repairs_exhausted" and s1["rule"] == "tests_fail_after_3_repairs"
    assert s1["rounds_used"] == 2 and s1["commit"] is None and "repair round" in s1["summary"]
    wt = Path(meta["exec"]["worktree"])
    assert git(wt, "log", "--format=%H", f"{meta['exec']['base_sha']}..HEAD").strip() == ""
    assert (wt / "src" / "s1.py").exists()  # edits kept for --retry


def test_rounds_exhausted_by_review_rejection(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    bad = review("reject", [issue("blocker")])
    ex, _ = executor(run_dir, rr, [edit({"a.py": "1\n"}), bad, edit({"a.py": "2\n"}), bad], review="per_step", max_repair_rounds=1)
    assert ex.start().status == "awaiting_approval"
    s1 = load_meta(run_dir)["steps"]["s1"]
    assert s1["stage"] == "repairs_exhausted" and "blocker/major review issue" in s1["summary"] and s1["commit"] is None


def test_repair_resume_failure_falls_back_to_fresh(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=FILE_CMD)
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), fail(ErrorKind.EXIT_NONZERO), edit({"ok.txt": "1"})],
                      review="off", max_repair_rounds=3)  # fmt: skip
    assert ex.start().status == "done"
    assert [r.resume_id for r in fb.requests] == [None, "s", None]
    r1 = load_meta(run_dir)["steps"]["s1"]["rounds"][0]
    assert r1["session"] == "fresh" and r1["resume_failed"] is True
    assert "REPAIR mode" in fb.requests[1].prompt and "REPAIR mode" not in fb.requests[2].prompt


def test_invalid_review_retries_once_then_fails(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    bad = raw_review({"verdict": "approve", "issues": [issue("major")], "summary": "x"})
    ex, fb = executor(run_dir, rr, [edit({"a.py": "1\n"}), bad, review("approve")], **ON)
    assert ex.start().status == "done"
    assert fb.requests[2].resume_id == "rs" and "invalid" in fb.requests[2].prompt
    run_dir2, rr2 = setup(plan=good_plan(steps=[step("s1")]))
    ex2, _ = executor(run_dir2, rr2, [edit({"b.py": "1\n"}), bad, bad, bad], **ON)
    out = ex2.start()
    assert out.status == "failed" and load_meta(run_dir2)["steps"]["s1"]["commit"] is None


def test_review_off_never_calls_reviewer(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, fb = executor(run_dir, rr, [edit({"a.py": "1\n"})], review="off")
    assert ex.start().status == "done" and len(fb.requests) == 1


# ---------- rate limits ----------


def test_rate_limit_in_review_pauses_and_resumes_in_place(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    # opus is limited -> claude:* cools -> the one allowed hop goes to agy:default, which is limited too -> pause
    ex, fb = executor(run_dir, rr, [edit({"a.py": "1\n"}), fail(ErrorKind.RATE_LIMITED), fail(ErrorKind.RATE_LIMITED)], **ON)
    out = ex.start()
    assert out.status == "paused"
    assert fb.requests[2].extra_args[:2] == ["--mode", "plan"]
    assert load_meta(run_dir)["resume_after"]
    s1 = load_meta(run_dir)["steps"]["s1"]
    assert s1["status"] == "pending" and s1["resume_phase"] == "review"
    budget.clear_cooldowns()
    ex2, fb2 = executor(run_dir, rr, [review("approve")], **ON)
    assert ex2.resume().status == "done"
    assert len(fb2.requests) == 1 and is_review_call(fb2.requests[0])  # the builder did not run again
    assert load_meta(run_dir)["steps"]["s1"]["commit"]


def test_rate_limit_in_repair_pauses_and_resumes_in_place(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=FILE_CMD)
    ex, _ = executor(run_dir, rr, [edit({"a.py": "1\n"}), fail(ErrorKind.RATE_LIMITED)], review="off", max_repair_rounds=3)
    assert ex.start().status == "paused"
    assert load_meta(run_dir)["steps"]["s1"]["resume_phase"] == "need_repair"
    budget.clear_cooldowns()
    ex2, fb2 = executor(run_dir, rr, [edit({"ok.txt": "1"})], review="off", max_repair_rounds=3)
    assert ex2.resume().status == "done"
    s1 = load_meta(run_dir)["steps"]["s1"]
    # Phase 5b: the rate-limited call counted as round 1, the resumed call is round 2
    assert [r["outcome"] for r in s1["rounds"]] == ["rate_limited", "ok"] and s1["rounds_used"] == 2
    assert len(fb2.requests) == 1 and (Path(load_meta(run_dir)["exec"]["worktree"]) / "a.py").exists()


# ---------- tests_removed ----------


@pytest.fixture
def repo_with_tests(env):
    repo, setup, executor = env
    (repo / "tests").mkdir()
    (repo / "tests" / "test_old.py").write_text("def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add tests")
    return repo, setup, executor


def test_deleted_test_file_stops(repo_with_tests):
    repo, setup, executor = repo_with_tests
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, _ = executor(run_dir, rr, [delete("tests/test_old.py")])
    out = ex.start()
    s1 = load_meta(run_dir)["steps"]["s1"]
    assert out.status == "awaiting_approval" and s1["rule"] == "tests_removed" and s1["flagged"] == ["tests/test_old.py"]
    assert s1["stage"] == "post_check" and s1["commit"] is None and s1["tests"] is None


def test_removed_test_function_stops_but_added_one_does_not(repo_with_tests):
    repo, setup, executor = repo_with_tests
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    one = {"tests/test_old.py": "def test_a():\n    assert 1\n"}
    ex, _ = executor(run_dir, rr, [edit(one)])
    assert ex.start().status == "awaiting_approval"
    assert load_meta(run_dir)["steps"]["s1"]["flagged"] == ["tests/test_old.py::test_b"]
    run_dir2, rr2 = setup(plan=good_plan(steps=[step("s1")]))
    more = {"tests/test_old.py": "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 1\n\n\ndef test_c():\n    assert 2\n"}
    ex2, _ = executor(run_dir2, rr2, [edit(more)])
    assert ex2.start().status == "done"


def test_removed_test_rule_is_a_valid_plan_flag():
    assert "tests_removed" in load_config().approval_rules
    from orch.plan import Plan

    Plan.model_validate(good_plan(steps=[step("s1", flags=["tests_removed"])]))


# ---------- approve ----------


def test_approve_pre_builder_runs_the_step(env):
    repo, setup, executor = env
    plan = good_plan(steps=[step("s1"), step("s2", deps=["s1"], flags=["db_migration_or_schema"])])
    run_dir, rr = setup(plan=plan)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "x\n"})])
    assert ex.start().status == "awaiting_approval"
    ex2, fb2 = executor(run_dir, rr, [edit({"src/s2.py": "y\n"})])
    out = ex2.approve()  # only one step waits, so --step is optional
    assert out.status == "done" and len(fb2.requests) == 1
    assert load_meta(run_dir)["steps"]["s2"]["commit"]


def test_approve_post_check_is_per_file_and_per_new_file(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=FILE_CMD)
    cfgo = {"review": "off", "max_repair_rounds": 3}
    ex, _ = executor(run_dir, rr, [edit({"token_utils.py": "v1\n"})], **cfgo)
    assert ex.start().status == "awaiting_approval"
    s1 = load_meta(run_dir)["steps"]["s1"]
    assert s1["stage"] == "post_check" and s1["flagged"] == ["token_utils.py"] and s1["tests"] is None
    # approve; tests fail (no ok.txt); repair 1 re-touches the approved file -> no new stop;
    # repair 2 writes .env (a NEW protected file) -> stops again
    ex2, fb2 = executor(run_dir, rr, [edit({"token_utils.py": "v2\n"}), edit({".env": "K=1\n", "ok.txt": "1"})], **cfgo)
    out = ex2.approve("s1")
    s1 = load_meta(run_dir)["steps"]["s1"]
    assert out.status == "awaiting_approval" and len(fb2.requests) == 2
    assert s1["flagged"] == [".env"] and s1["rounds_used"] == 2 and s1["commit"] is None
    assert s1["approved"] == [{"file": "token_utils.py", "rule": "touches_auth_payments_secrets_env"}]
    ex3, fb3 = executor(run_dir, rr, [], **cfgo)
    assert ex3.approve("s1").status == "done" and fb3.requests == []  # straight to tests -> commit
    s1 = load_meta(run_dir)["steps"]["s1"]
    assert s1["commit"] and {a["file"] for a in s1["approved"]} == {"token_utils.py", ".env"}


def test_approve_after_rounds_ran_out_needs_retry(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=FILE_CMD)
    cfgo = {"review": "off", "max_repair_rounds": 1}
    ex, _ = executor(run_dir, rr, [edit({"a.py": "1\n"}), edit({"a.py": "2\n"})], **cfgo)
    assert ex.start().status == "awaiting_approval"
    ex2, fb2 = executor(run_dir, rr, [], **cfgo)
    with pytest.raises(ExecError, match="--retry"):
        ex2.approve("s1")
    assert fb2.requests == [] and load_meta(run_dir)["steps"]["s1"]["status"] == "awaiting_approval"
    ex3, fb3 = executor(run_dir, rr, [edit({"ok.txt": "1"})], **cfgo)
    assert ex3.approve("s1", retry=True).status == "done"
    s1 = load_meta(run_dir)["steps"]["s1"]
    assert s1["rounds_total"] == 2 and s1["rounds_used"] == 1 and s1["commit"]


def test_approve_refuses_steps_that_are_not_waiting(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, _ = executor(run_dir, rr, [edit(rep=report(blocked=True, question="Which db?"))])
    assert ex.start().status == "needs_input"
    ex2, _ = executor(run_dir, rr, [])
    with pytest.raises(ExecError, match="no step is awaiting approval"):
        ex2.approve()
    with pytest.raises(ExecError, match="needs_input"):
        ex2.approve("s1")


# ---------- reject / answer ----------


def test_reject_stops_the_run(env):
    repo, setup, executor = env
    plan = good_plan(steps=[step("s1", flags=["db_migration_or_schema"])])
    run_dir, rr = setup(plan=plan)
    ex, _ = executor(run_dir, rr, [])
    assert ex.start().status == "awaiting_approval"
    ex2, _ = executor(run_dir, rr, [])
    assert ex2.reject("s1").status == "rejected"
    meta = load_meta(run_dir)
    assert meta["status"] == "rejected" and meta["steps"]["s1"]["status"] == "rejected"
    ex3, fb3 = executor(run_dir, rr, [edit({})])
    for call in (ex3.resume, lambda: ex3.approve("s1")):
        with pytest.raises(ExecError, match="rejected"):
            call()
    assert fb3.requests == []


def test_answer_reruns_the_builder_with_the_answer(env):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, _ = executor(run_dir, rr, [edit(rep=report(blocked=True, question="Which db?"))])
    assert ex.start().status == "needs_input"
    ex2, fb2 = executor(run_dir, rr, [edit({"src/s1.py": "db = 1\n"})])
    assert ex2.answer(None, "use sqlite").status == "done"
    assert "use sqlite" in fb2.requests[0].prompt and "Answers from the human" in fb2.requests[0].prompt
    assert load_meta(run_dir)["steps"]["s1"]["answers"] == ["use sqlite"]


# ---------- CLI ----------


def write_cfg(tmp_path, monkeypatch, repo, test_command=PASS_CMD, **over):
    data = yaml.safe_load(open(default_config_path(), encoding="utf-8"))
    data["repos"] = {"demo": {"path": str(repo), "test_command": test_command}}
    data.update({"review": "off", "max_repair_rounds": 0, **over})
    f = tmp_path / "config.yaml"
    f.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("ORCH_CONFIG", str(f))


def test_cli_status_next_commands_approve_reject(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    write_cfg(tmp_path, monkeypatch, repo, test_command=FILE_CMD)
    run_dir, _ = setup(plan=good_plan(steps=[step("s1")]))
    fb = FakeBuilder([edit({"a.py": "1\n"})])
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    runner = CliRunner()
    r = runner.invoke(cli.app, ["exec", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 2 and "Result: awaiting_approval" in r.output
    assert f"orch approve {run_dir.name} --step s1 --retry" in r.output and "Next:" in r.output
    r = runner.invoke(cli.app, ["approve", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 2 and "--retry" in r.output
    r = runner.invoke(cli.app, ["status", run_dir.name, "--repo", "demo"])
    assert "Needs from you" in r.output and "rounds" in r.output
    r = runner.invoke(cli.app, ["reject", run_dir.name, "--step", "s1", "--repo", "demo"])
    assert r.exit_code == 1 and "rejected" in r.output


def test_cli_status_shows_rounds_verdicts_and_issues(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    write_cfg(tmp_path, monkeypatch, repo, review="per_step", max_repair_rounds=3)
    run_dir, _ = setup(plan=good_plan(steps=[step("s1")]))
    fb = FakeBuilder([edit({"a.py": "1\n"}), review("reject", [issue("major", problem="off by one")]),
                      edit({"a.py": "2\n"}), review("approve", [issue("minor", problem="naming")])])  # fmt: skip
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    runner = CliRunner()
    r = runner.invoke(cli.app, ["exec", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 0, r.output
    for needle in ("round 1: review", "off by one", "naming", "approve", "tokens by role"):
        assert needle in r.output
    assert f"orch merge {run_dir.name}" in r.output


def test_cli_answer_for_step(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    write_cfg(tmp_path, monkeypatch, repo)
    run_dir, _ = setup(plan=good_plan(steps=[step("s1")]))
    fb = FakeBuilder([edit(rep=report(blocked=True, question="Which db?")), edit({"a.py": "1\n"})])
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    runner = CliRunner()
    r = runner.invoke(cli.app, ["exec", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 2 and "orch answer" in r.output and "Which db?" in r.output
    r = runner.invoke(cli.app, ["answer", run_dir.name, "use sqlite", "--step", "s1", "--repo", "demo"])
    assert r.exit_code == 0, r.output
    assert "use sqlite" in fb.requests[1].prompt


def test_cli_answer_for_plan(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    write_cfg(tmp_path, monkeypatch, repo)

    def planner(structured):
        return lambda req: AgentResult(agent="claude", ok=True, error_kind=ErrorKind.OK, structured=structured,
                                       session_id="p", total_tokens=70)  # fmt: skip

    ask = good_plan(needs_input=True, question="Which database?", steps=[])
    fb = FakeBuilder([planner(ask), planner(good_plan(steps=[step("s1")]))])
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    runner = CliRunner()
    r = runner.invoke(cli.app, ["plan", "add storage", "--repo", "demo"])
    assert r.exit_code == 2 and "orch answer" in r.output
    run_id = r.output.split("Run: ")[1].split()[0]
    r = runner.invoke(cli.app, ["answer", run_id, "sqlite", "--repo", "demo"])
    assert r.exit_code == 0, r.output
    assert "Answer: sqlite" in fb.requests[1].prompt and "Question: Which database?" in fb.requests[1].prompt
    assert f"orch exec {run_id}" in r.output
    run_dir = repo / ".orch" / "runs" / run_id
    meta = load_meta(run_dir)
    assert meta["status"] == "planned" and meta["total_tokens"] == 140
    assert (run_dir / "planner_raw.1.json").is_file() and "sqlite" in (run_dir / "request.md").read_text(encoding="utf-8")


# ---------- merge ----------


@pytest.fixture
def done_run(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a = 1\n"})])
    assert ex.start().status == "done"
    git(repo, "checkout", "-q", "-b", "work")
    return repo, run_dir, rr


def test_merge_success_no_ff_never_pushes(done_run):
    repo, run_dir, rr = done_run
    r = merge_run(rr, run_dir)
    assert r.target == "work" and r.tests_passed is True
    assert git(repo, "log", "-1", "--format=%s").strip() == "Merge orch run: do the thing"
    assert len(git(repo, "log", "-1", "--format=%P").split()) == 2  # a real merge commit
    assert (repo / "src" / "s1.py").is_file() and load_meta(run_dir)["merged_sha"] == r.sha
    assert git(repo, "remote").strip() == ""
    with pytest.raises(MergeError, match="already merged"):
        merge_run(rr, run_dir)


def test_merge_refusals(env, done_run, tmp_path):
    repo, run_dir, rr = done_run
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(MergeError, match="uncommitted"):
        merge_run(rr, run_dir)
    git(repo, "checkout", "--", "README.md")
    git(repo, "checkout", "-q", "main")
    with pytest.raises(MergeError, match="allow-main"):
        merge_run(rr, run_dir)
    git(repo, "checkout", "-q", "--detach")
    with pytest.raises(MergeError, match="detached"):
        merge_run(rr, run_dir)
    git(repo, "checkout", "-q", "work")
    meta = load_meta(run_dir)
    meta["status"] = "awaiting_approval"
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(MergeError, match="not fully done"):
        merge_run(rr, run_dir)
    assert git(repo, "log", "-1", "--format=%s").strip() == "init"  # nothing was merged


def test_merge_into_main_with_allow_main(done_run):
    repo, run_dir, rr = done_run
    git(repo, "checkout", "-q", "main")
    assert merge_run(rr, run_dir, allow_main=True).target == "main"


def test_merge_reports_failing_tests_but_keeps_the_merge(done_run):
    repo, run_dir, rr = done_run
    rr = rr.model_copy(update={"test_command": 'python -c "import sys; sys.exit(1)"'})
    r = merge_run(rr, run_dir)
    assert r.tests_passed is False and load_meta(run_dir)["merged_sha"]


def test_cli_merge(done_run, tmp_path, monkeypatch):
    repo, run_dir, rr = done_run
    write_cfg(tmp_path, monkeypatch, repo)
    runner = CliRunner()
    git(repo, "checkout", "-q", "main")
    r = runner.invoke(cli.app, ["merge", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 1 and "allow-main" in r.output
    git(repo, "checkout", "-q", "work")
    r = runner.invoke(cli.app, ["merge", run_dir.name, "--repo", "demo"])
    assert r.exit_code == 0, r.output
    assert "not pushed" in r.output and "Tests after merge: PASS" in r.output


# ---------- environment ----------


def test_virtual_env_is_removed_from_every_child_env(monkeypatch, tmp_path):
    for k in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("KEEP_ME", "1")
    env = child_env()
    assert "VIRTUAL_ENV" not in env and "VIRTUAL_ENV_PROMPT" not in env and env["KEEP_ME"] == "1"
    claude = ClaudeCodeAdapter().build_command(AgentRequest(prompt="p", cwd=tmp_path)).env
    agy = AntigravityAdapter().build_command(AgentRequest(prompt="p", cwd=tmp_path)).env
    for e in (claude, agy):
        assert "VIRTUAL_ENV" not in e and "VIRTUAL_ENV_PROMPT" not in e and e["KEEP_ME"] == "1"
    assert "ANTHROPIC_API_KEY" not in claude


def test_orchestrator_test_runs_have_no_virtual_env(env, monkeypatch):
    repo, setup, executor = env
    monkeypatch.setenv("VIRTUAL_ENV", "/some/venv")
    cmd = 'python -c "import os,sys; sys.exit(1 if \'VIRTUAL_ENV\' in os.environ else 0)"'
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=cmd)
    ex, _ = executor(run_dir, rr, [edit({"a.py": "1\n"})])
    assert ex.start().status == "done"


def test_retest_command_comes_from_config_only():
    cfg = load_config()
    assert cfg.repos["orch"].retest_command == "uv run pytest -q --lf"
    assert cfg.review == "per_step" and cfg.max_repair_rounds == 3
    from orch.config import resolve_repo

    assert resolve_repo(cfg, "orch").retest_command == "uv run pytest -q --lf"
    assert "retest_command" not in __import__("orch.plan", fromlist=["Plan"]).Plan.model_fields
