"""Phase 5b: --hidden-tests and the live view. FakeAdapter only; no real CLI."""

from io import StringIO

import pytest
import yaml
from git_helpers import git
from plan_factory import good_plan, step
from rich.console import Console
from test_executor import PASS_CMD, FakeBuilder, edit, env  # noqa: F401  (env is a fixture)
from typer.testing import CliRunner

from orch import budget, cli
from orch.adapters.base import AgentResult, ErrorKind
from orch.config import default_config_path
from orch.executor import ExecError, check_hidden_tests
from orch.runs import load_meta
from orch.ui import LiveView, PlainView, footer_text, make_view, step_rows, stop_lines

# fails while the hidden file is present and fixed.txt is not
HID_CMD = (
    'python -c "import sys,pathlib; '
    "sys.exit(1 if pathlib.Path('tests/hid_extra.py').exists() and not pathlib.Path('fixed.txt').exists() else 0)\""
)


def hidden_dir(tmp_path, name="hid_extra.py", content="# hidden\n"):
    d = tmp_path / "hidden"
    d.mkdir(exist_ok=True)
    (d / name).write_text(content, encoding="utf-8")
    return d


def test_hidden_tests_copied_after_build_before_checks_and_committed(env, tmp_path, isolated_worktree_root):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]), test_command=HID_CMD)
    seen = []

    def build(req):
        seen.append((req.cwd / "tests" / "hid_extra.py").exists())  # the builder must NOT see it
        return edit({"src/s1.py": "a = 1\n"})(req)

    def repair(req):
        seen.append((req.cwd / "tests" / "hid_extra.py").exists())
        return edit({"fixed.txt": "ok\n"})(req)

    logs = []
    ex, fb = executor(run_dir, rr, [build, repair], logs=logs, max_repair_rounds=1)
    ex.hidden_tests = hidden_dir(tmp_path)
    out = ex.start()
    assert out.status == "done", out.message
    assert seen == [False, True]  # invisible to the builder, present for the repair round
    meta = load_meta(run_dir)
    s1 = meta["steps"]["s1"]
    assert s1["hidden_files"] == ["tests/hid_extra.py"] and s1["rounds_total"] == 1  # first run failed -> repair
    assert not any("hid_extra" in w for w in s1["warnings"])  # hidden file is not out-of-plan
    assert any("hidden tests copied" in m for m in logs)
    wt = isolated_worktree_root / "demo" / run_dir.name
    assert "tests/hid_extra.py" in git(wt, "show", "--name-only", "--format=", "HEAD").split()
    assert meta["exec"]["hidden_tests"]["step"] == "s1"


def test_hidden_tests_copied_once_per_run(env, tmp_path):
    repo, setup, executor = env
    run_dir, rr = setup(test_command=PASS_CMD)
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"src/s2.py": "b\n"})])
    ex.hidden_tests = hidden_dir(tmp_path)
    assert ex.start().status == "done"
    meta = load_meta(run_dir)
    assert meta["steps"]["s1"]["hidden_files"] == ["tests/hid_extra.py"]
    assert not meta["steps"]["s2"].get("hidden_files")


def test_hidden_tests_option_survives_resume(env, tmp_path):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))

    def rate(req):
        return AgentResult(agent="claude", ok=False, error_kind=ErrorKind.RATE_LIMITED, text="429")

    ex, _ = executor(run_dir, rr, [rate])
    ex.hidden_tests = hidden_dir(tmp_path)
    assert ex.start().status == "paused"
    budget.clear_cooldowns()
    ex2, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])  # no option given: read back from meta.json
    assert ex2.resume().status == "done"
    assert load_meta(run_dir)["steps"]["s1"]["hidden_files"] == ["tests/hid_extra.py"]


def test_hidden_tests_refusals(env, tmp_path, isolated_worktree_root):
    repo, setup, executor = env
    ok = hidden_dir(tmp_path)
    assert check_hidden_tests(ok, repo) == ["hid_extra.py"]
    inside_repo = repo / "secret_hidden"
    inside_repo.mkdir()
    (inside_repo / "t.py").write_text("x", encoding="utf-8")
    with pytest.raises(ExecError, match="inside the target repo"):
        check_hidden_tests(inside_repo, repo)
    wt_dir = isolated_worktree_root / "demo" / "run1" / "h"
    wt_dir.mkdir(parents=True)
    (wt_dir / "t.py").write_text("x", encoding="utf-8")
    with pytest.raises(ExecError, match="inside an orch worktree"):
        check_hidden_tests(wt_dir, repo)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ExecError, match="no files"):
        check_hidden_tests(empty, repo)
    sub = tmp_path / "withsub"
    (sub / "x").mkdir(parents=True)
    with pytest.raises(ExecError, match="subfolders"):
        check_hidden_tests(sub, repo)
    with pytest.raises(ExecError, match="not a directory"):
        check_hidden_tests(tmp_path / "nope", repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "hid_extra.py").write_text("# mine\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tests")
    with pytest.raises(ExecError, match="already has file"):
        check_hidden_tests(ok, repo)


def test_start_refuses_before_creating_a_worktree(env, tmp_path, isolated_worktree_root):
    repo, setup, executor = env
    run_dir, rr = setup(plan=good_plan(steps=[step("s1")]))
    ex, fb = executor(run_dir, rr, [])
    ex.hidden_tests = tmp_path / "missing"
    with pytest.raises(ExecError):
        ex.start()
    assert not (isolated_worktree_root / "demo").exists() and not fb.requests
    assert "exec" not in load_meta(run_dir)


def _cfg(tmp_path, monkeypatch, repo):
    data = yaml.safe_load(open(default_config_path(), encoding="utf-8"))
    data["repos"] = {"demo": {"path": str(repo), "test_command": PASS_CMD}}
    data.update(review="off", max_repair_rounds=0)
    f = tmp_path / "config.yaml"
    f.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("ORCH_CONFIG", str(f))


def test_cli_run_refuses_bad_hidden_dir_before_planning(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    _cfg(tmp_path, monkeypatch, repo)
    called = []
    monkeypatch.setattr(cli, "get_adapter", lambda name: called.append(name))
    r = CliRunner().invoke(cli.app, ["run", "do it", "--repo", "demo", "--hidden-tests", str(repo)])
    assert r.exit_code == 2 and "inside the target repo" in r.output
    assert not called and not (repo / ".orch" / "runs").exists()


def test_cli_exec_plain_output_and_stop_block(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    _cfg(tmp_path, monkeypatch, repo)
    run_dir, _ = setup(plan=good_plan(steps=[step("s1", files=["src/s1.py", ".env"])]))
    fb = FakeBuilder([edit({"src/s1.py": "a\n", ".env": "K=1\n"})])
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    r = CliRunner().invoke(cli.app, ["exec", run_dir.name, "--repo", "demo", "--plain"])
    assert r.exit_code == 2, r.output
    out = r.output
    assert "[s1] Do s1: running builder" in out  # log lines unchanged
    assert "Result: awaiting_approval" in out
    nxt = f"orch approve {run_dir.name} --step s1 --repo demo"
    assert f"Next: {nxt}" in out
    block = out.split("--- STOPPED ---")[1]
    assert "touches_auth_payments_secrets_env" in block and ".env" in block and nxt in block


def test_cli_done_run_has_no_stop_block(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    _cfg(tmp_path, monkeypatch, repo)
    run_dir, _ = setup(plan=good_plan(steps=[step("s1")]))
    monkeypatch.setattr(cli, "get_adapter", lambda name: FakeBuilder([edit({"src/s1.py": "a\n"})]))
    r = CliRunner().invoke(cli.app, ["exec", run_dir.name, "--repo", "demo"])  # CliRunner is not a tty -> plain
    assert r.exit_code == 0 and "Result: done" in r.output and "STOPPED" not in r.output


# ---------- ui helpers ----------

META = {
    "status": "executing", "exec_tokens": 12345,
    "steps": {
        "s1": {"title": "Slugify", "status": "running", "phase": "need_repair", "tokens": 2000, "rounds_total": 1,
               "roles_used": {"repair": {"entry": "claude:sonnet", "fallback": True}}},
        "s2": {"title": "Docs", "status": "pending", "tokens": 0},
    },
}  # fmt: skip


def test_step_rows_phase_role_entry_round():
    r1, r2 = step_rows(META, {"builder": "claude:sonnet"}, {"s1": 75.0})
    assert (r1["phase"], r1["role"], r1["entry"], r1["round"], r1["tokens"], r1["elapsed"]) == (
        "repair", "repair", "claude:sonnet (fallback)", 1, 2000, "1:15")
    assert r2["status"] == "pending" and r2["phase"] == r2["role"] == r2["entry"] == "-"
    building = {"steps": {"s1": {"title": "t", "status": "running", "phase": "build"}}}
    assert step_rows(building, {"builder": "claude:sonnet"}, {})[0]["entry"] == "claude:sonnet"  # first entry until used
    assert footer_text(META, 83) == "total 12,345 tokens | run: executing | 1:23"


def test_stop_lines_name_the_exact_next_command():
    meta = {"status": "needs_input", "steps": {"s1": {"question": "Which db?", "stage": "builder"}}}
    lines = stop_lines("needs_input", "s1", "builder is blocked", meta, ['orch answer r --step s1 "<your answer>"'])
    text = "\n".join(lines)
    assert "needs_input at step s1" in text and "question: Which db?" in text
    assert 'Next: orch answer r --step s1 "<your answer>"' in text
    paused = stop_lines("paused", "s1", "rate limited", {"resume_after": "2026-11-10T20:00:00+00:00", "steps": {}}, ["orch resume r"])
    assert "resume after 2026-11-10T20:00:00" in "\n".join(paused) and "Next: orch resume r" in paused


def test_make_view_plain_when_flag_or_not_a_terminal(monkeypatch):
    out = []
    assert isinstance(make_view(True, out.append, {}), PlainView) and not make_view(True, out.append, {}).live
    monkeypatch.setattr("orch.ui.is_interactive", lambda: False)
    assert not make_view(False, out.append, {}).live
    monkeypatch.setattr("orch.ui.is_interactive", lambda: True)
    assert isinstance(make_view(False, out.append, {}), LiveView)


def test_live_view_renders_table_and_panel():
    buf = StringIO()
    t = iter(range(0, 1000, 5))
    view = LiveView({"builder": "claude:sonnet"}, Console(file=buf, width=140, force_terminal=False), clock=lambda: next(t))
    with view:
        view.log("[s1] Slugify: running builder")
        view.on_update(META)
        view.show_stop("awaiting_approval", "s1", "tests_removed: tests/a.py", {"steps": {"s1": {"rule": "tests_removed"}}},
                       ["orch approve r --step s1"])  # fmt: skip
    text = buf.getvalue()
    assert "running builder" in text and "Slugify" in text and "claude:sonnet (fallback)" in text
    assert "total 12,345 tokens" in text and "orch approve r --step s1" in text and "Run awaiting_approval" in text
