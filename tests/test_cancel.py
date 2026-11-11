"""P3 (D1): cancel token through run_process, router and executor; the `cancelled` outcome; Ctrl-C handling. Fakes only."""

import json
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from git_helpers import git
from plan_factory import good_plan, step
from test_executor import PASS_CMD, Boom, FakeBuilder, edit, env  # noqa: F401  (env is a fixture)
from test_phase4 import ON, review, write_cfg
from typer.testing import CliRunner

from orch import budget, cli, runlock, workspace
from orch.adapters import antigravity, claude_code
from orch.adapters.base import AgentRequest, AgentResult, ErrorKind
from orch.adapters.common import parse_process_json
from orch.adapters.fake import FakeAdapter
from orch.adapters.process import ProcessOutput, run_process
from orch.config import ResolvedRepo, load_config
from orch.router import call_role
from orch.runs import load_meta

SLEEP_CMD = 'python -c "import time; time.sleep(60)"'


def cancelled_result():
    return AgentResult(agent="claude", ok=False, error_kind=ErrorKind.CANCELLED, text="cancelled")


def cancels(h):
    """A fake agent call that gets cancelled: it sets the executor's token, as Ctrl-C would, and reports CANCELLED."""

    def action(req):
        assert req.cancel is h["ex"]._cancel  # the token reaches the request
        h["ex"].cancel()
        return cancelled_result()

    return action


# ---------- run_process ----------


def test_cancel_kills_the_child_and_reports_cancelled_not_timeout(tmp_path):
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    t0 = time.perf_counter()
    o = run_process([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path, timeout_s=60, cancel=ev)
    assert o.cancelled and not o.timed_out and not o.not_found
    assert time.perf_counter() - t0 < 15


def test_cancel_kills_the_grandchild_too(tmp_path):
    marker = tmp_path / "alive.txt"
    child = (
        "import time, pathlib\n"
        "for _ in range(150):\n"
        f"    pathlib.Path({str(marker)!r}).write_text('x')\n"
        "    time.sleep(0.2)\n"
    )
    parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
    ev = threading.Event()
    threading.Timer(1.5, ev.set).start()
    o = run_process([sys.executable, "-c", parent], cwd=tmp_path, timeout_s=60, cancel=ev)
    assert o.cancelled
    time.sleep(0.5)
    marker.unlink(missing_ok=True)
    time.sleep(1.0)
    assert not marker.exists()


def test_cancel_already_set_starts_nothing(tmp_path):
    ev = threading.Event()
    ev.set()
    o = run_process(["definitely-not-a-real-exe-xyz"], cwd=tmp_path, timeout_s=5, cancel=ev)
    assert o.cancelled and not o.not_found and o.returncode is None


def test_stdin_survives_the_poll_loop(tmp_path):
    code = "import sys, time; d = sys.stdin.buffer.read(); time.sleep(0.7); sys.stdout.buffer.write(d)"
    o = run_process([sys.executable, "-c", code], cwd=tmp_path, stdin_text="café ✓\nline2", timeout_s=30, cancel=threading.Event())
    assert o.stdout == "café ✓\nline2" and not o.cancelled and o.returncode == 0


def test_timeout_still_wins_when_the_token_is_never_set(tmp_path):
    t0 = time.perf_counter()
    o = run_process([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path, timeout_s=1, cancel=threading.Event())
    assert o.timed_out and not o.cancelled and time.perf_counter() - t0 < 15


# ---------- adapters, router, usage log ----------


@pytest.mark.parametrize("agent", ["claude", "agy"])
def test_cancelled_output_is_a_cancelled_failure_even_after_a_timeout_flag(agent):
    data, failed = parse_process_json(agent, ProcessOutput(cancelled=True, timed_out=True), 10)
    assert data is None and failed.error_kind == ErrorKind.CANCELLED and not failed.ok


def test_both_adapters_pass_the_token_to_run_process(monkeypatch, tmp_path):
    seen = {}
    for mod in (claude_code, antigravity):
        monkeypatch.setattr(mod, "run_process", lambda *a, _m=mod, **k: seen.setdefault(_m.__name__, k) and ProcessOutput(cancelled=True))
    ev = threading.Event()
    req = AgentRequest(prompt="x", cwd=tmp_path, cancel=ev)
    assert claude_code.ClaudeCodeAdapter().run(req).error_kind == ErrorKind.CANCELLED
    assert antigravity.AntigravityAdapter().run(req).error_kind == ErrorKind.CANCELLED
    assert all(k["cancel"] is ev for k in seen.values()) and len(seen) == 2


def test_the_token_is_never_serialised_and_survives_a_copy(tmp_path):
    ev = threading.Event()
    req = AgentRequest(prompt="x", cwd=tmp_path, cancel=ev)
    assert "cancel" not in req.model_dump() and "cancel" not in json.loads(req.model_dump_json())
    assert req.model_copy(update={"model": "m"}).cancel is ev


def _route(tmp_path, adapter, cancel):
    cfg = load_config()
    build = lambda entry: AgentRequest(prompt="x", cwd=tmp_path)  # noqa: E731
    return call_role("builder", build, lambda name: adapter, cfg, cancel=cancel, db_path=tmp_path / "u.db"), cfg


def test_router_cancelled_result_is_final_no_cooldown_no_hop(tmp_path):
    adapter = FakeAdapter([cancelled_result()])
    routed, _ = _route(tmp_path, adapter, threading.Event())
    assert routed.result.error_kind == ErrorKind.CANCELLED and not routed.paused
    assert len(adapter.requests) == 1
    assert budget.active_cooldowns(db_path=tmp_path / "u.db") == []


def test_router_with_the_token_already_set_calls_nothing(tmp_path):
    adapter = FakeAdapter()
    ev = threading.Event()
    ev.set()
    routed, _ = _route(tmp_path, adapter, ev)
    assert routed.result.error_kind == ErrorKind.CANCELLED and adapter.requests == []


def test_router_stops_the_fallback_hop_when_cancelled_during_a_rate_limit(tmp_path):
    ev = threading.Event()
    limited = AgentResult(agent="claude", ok=False, error_kind=ErrorKind.RATE_LIMITED, text="usage limit reached")

    class SetsEvent(FakeAdapter):
        def run(self, request):
            ev.set()
            return super().run(request)

    adapter = SetsEvent([limited])
    routed, _ = _route(tmp_path, adapter, ev)
    assert routed.result.error_kind == ErrorKind.CANCELLED and not routed.paused
    assert len(adapter.requests) == 1  # no second entry was tried


def test_cancelled_call_is_logged_with_unknown_tokens_and_shown_in_usage(tmp_path):
    db = tmp_path / "u.db"
    req = AgentRequest(prompt="x", cwd=tmp_path)
    budget.run_agent(FakeAdapter([cancelled_result()]), req, db)
    budget.run_agent(FakeAdapter(), req, db)
    rows = sqlite3.connect(db).execute("SELECT error_kind, total_tokens, tokens_unknown FROM calls ORDER BY id").fetchall()
    assert rows == [("CANCELLED", 0, 1), ("OK", 15, 0)]
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert budget.unknown_token_calls(since, db) == 1
    [row] = budget.summary(since, db)
    assert (row.calls, row.tokens, row.failures) == (2, 15, 1)


def test_an_old_usage_db_gets_the_new_column(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(budget.SCHEMA.replace(",\n    tokens_unknown INTEGER NOT NULL DEFAULT 0  -- 1: the call was cancelled, so its tokens are stored as 0", ""))
    conn.commit()
    conn.close()
    budget.log_call(cancelled_result(), AgentRequest(prompt="x", cwd=tmp_path), db)
    assert sqlite3.connect(db).execute("SELECT tokens_unknown FROM calls").fetchone() == (1,)


def test_usage_command_mentions_cancelled_calls(monkeypatch):
    budget.log_call(cancelled_result(), AgentRequest(prompt="x", cwd="."))
    r = CliRunner().invoke(cli.app, ["usage", "--days", "1"])
    assert "1 call(s) cancelled, tokens unknown" in r.output
    assert json.loads(CliRunner().invoke(cli.app, ["usage", "--days", "1", "--json"]).output)["cancelled_calls"] == 1


# ---------- executor ----------


def one_step(setup, **kw):
    return setup(plan=good_plan(steps=[step("s1")]), **kw)


def test_cancel_during_build_then_resume_rebuilds(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    h = {}
    ex, fb = executor(run_dir, rr, [cancels(h)])
    h["ex"] = ex
    out = ex.start()
    assert (out.status, out.step_id) == ("cancelled", "s1") and "orch resume" in out.message
    meta = load_meta(run_dir)
    st = meta["steps"]["s1"]
    assert meta["status"] == "cancelled" and st["status"] == "pending" and st["error"] == "cancelled" and st["resume_phase"] is None
    assert st["calls"] == 1 and st["tokens"] == 0
    assert runlock.holder(run_dir) is None
    ex2, fb2 = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    assert ex2.resume().status == "done" and len(fb2.requests) == 1
    assert load_meta(run_dir)["steps"]["s1"]["error"] is None


def test_the_cancelled_call_is_in_the_usage_log(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    h = {}
    ex, _ = executor(run_dir, rr, [cancels(h)])
    h["ex"] = ex
    ex.start()
    rows = sqlite3.connect(budget.default_db_path()).execute("SELECT error_kind, total_tokens, tokens_unknown FROM calls").fetchall()
    assert rows == [("CANCELLED", 0, 1)]


def test_cancel_during_review_resumes_at_review_without_a_new_build(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    h = {}
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), cancels(h)], **ON)
    h["ex"] = ex
    out = ex.start()
    st = load_meta(run_dir)["steps"]["s1"]
    assert out.status == "cancelled" and st["resume_phase"] == "review" and st["status"] == "pending"
    ex2, fb2 = executor(run_dir, rr, [review("approve")], **ON)
    assert ex2.resume().status == "done" and len(fb2.requests) == 1


def test_cancel_during_the_tests_kills_the_test_process_and_resumes_at_tests(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup, test_command=SLEEP_CMD)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    threading.Timer(2.0, ex.cancel).start()
    t0 = time.perf_counter()
    out = ex.start()
    assert out.status == "cancelled" and time.perf_counter() - t0 < 30
    st = load_meta(run_dir)["steps"]["s1"]
    assert st["resume_phase"] == "tests" and st["tests"] is None and st["status"] == "pending"
    ex2, fb2 = executor(run_dir, ResolvedRepo(name="demo", path=repo, test_command=PASS_CMD), [])
    assert ex2.resume().status == "done" and fb2.requests == []


def test_cancel_during_repair_counts_no_round_and_rebuilds(env):
    repo, setup, executor = env
    ok_cmd = 'python -c "import sys,pathlib; sys.exit(0 if pathlib.Path(\'ok.txt\').exists() else 1)"'
    run_dir, rr = one_step(setup, test_command=ok_cmd)
    h = {}
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), cancels(h)], max_repair_rounds=3)
    h["ex"] = ex
    out = ex.start()
    st = load_meta(run_dir)["steps"]["s1"]
    assert out.status == "cancelled" and st["resume_phase"] is None  # a killed repair left partial edits: rebuild
    assert st["rounds"] == [] and st["rounds_used"] == 0 and st["rounds_total"] == 0
    ex2, fb2 = executor(run_dir, rr, [edit({"ok.txt": "x\n"})], max_repair_rounds=3)
    assert ex2.resume().status == "done" and len(fb2.requests) == 1


def test_cancel_between_steps_leaves_the_next_step_untouched(env):
    repo, setup, executor = env
    run_dir, rr = setup()
    h = {}

    class Hook(list):
        def append(self, m):
            super().append(m)
            if m.startswith("[s1] done"):
                h["ex"].cancel()

    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"src/s2.py": "b\n"})], logs=Hook())
    h["ex"] = ex
    out = ex.start()
    meta = load_meta(run_dir)
    assert (out.status, out.step_id) == ("cancelled", "s2") and len(fb.requests) == 1
    assert meta["steps"]["s1"]["status"] == "done" and meta["steps"]["s2"]["status"] == "pending"
    assert meta["steps"]["s2"]["error"] is None and meta["steps"]["s2"]["calls"] == 0
    ex2, fb2 = executor(run_dir, rr, [edit({"src/s2.py": "b\n"})])
    assert ex2.resume().status == "done" and len(fb2.requests) == 1


def test_cancel_from_another_thread_while_the_agent_blocks(env):
    """The pattern the shell's run controller will use: the executor in a worker thread, cancel() from the prompt thread."""
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    inside, result = threading.Event(), {}

    def blocking_agent(req):
        inside.set()
        req.cancel.wait(30)  # what a real adapter does: run_process polls the same event
        return cancelled_result()

    ex, _ = executor(run_dir, rr, [blocking_agent])
    worker = threading.Thread(target=lambda: result.update(out=ex.start()))
    worker.start()
    assert inside.wait(10)
    assert runlock.holder(run_dir) is not None  # alive while it works
    ex.cancel()
    worker.join(15)
    assert not worker.is_alive() and result["out"].status == "cancelled"
    assert runlock.holder(run_dir) is None
    assert load_meta(run_dir)["steps"]["s1"]["status"] == "pending"


def test_stop_now_from_another_thread_only_cancels(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    ex, _ = executor(run_dir, rr, [])
    t = threading.Thread(target=ex.stop_now)
    t.start()
    t.join()
    assert ex._cancel.is_set()  # and nothing was raised in that thread


# ---------- stop now / KeyboardInterrupt: the step stays consistent ----------


def test_stop_now_inside_the_agent_call_saves_the_step_as_cancelled(env):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    h = {}

    def second_press(req):
        h["ex"].stop_now()  # raises KeyboardInterrupt out of the call, as the signal handler would
        raise AssertionError("not reached")

    ex, _ = executor(run_dir, rr, [second_press])
    h["ex"] = ex
    out = ex.start()
    st = load_meta(run_dir)["steps"]["s1"]
    assert out.status == "cancelled" and st["status"] == "pending" and st["error"] == "cancelled" and st["resume_phase"] is None
    assert runlock.holder(run_dir) is None


def test_stop_now_during_the_commit_waits_for_it(env, monkeypatch):
    repo, setup, executor = env
    run_dir, rr = setup()
    h = {}
    real = workspace.commit_step

    def press_during_commit(*a, **k):
        h["ex"].stop_now()  # arrives inside the critical section: deferred
        return real(*a, **k)

    monkeypatch.setattr(workspace, "commit_step", press_during_commit)
    ex, fb = executor(run_dir, rr, [edit({"src/s1.py": "a\n"}), edit({"src/s2.py": "b\n"})])
    h["ex"] = ex
    out = ex.start()
    monkeypatch.setattr(workspace, "commit_step", real)
    meta = load_meta(run_dir)
    s1 = meta["steps"]["s1"]
    wt = workspace.Path(meta["exec"]["worktree"])
    assert out.status == "cancelled" and s1["status"] == "done" and s1["commit"] == git(wt, "rev-parse", "HEAD").strip()
    assert meta["steps"]["s2"]["status"] == "pending" and len(fb.requests) == 1
    ex2, fb2 = executor(run_dir, rr, [edit({"src/s2.py": "b\n"})])
    assert ex2.resume().status == "done" and len(fb2.requests) == 1


def test_stop_now_during_the_save_waits_for_the_write(env, monkeypatch):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    h = {}
    import orch.executor as exmod

    real = exmod.write_meta

    def press_during_save(run_dir_, meta):
        if meta["steps"].get("s1", {}).get("phase") == "tests" and not h.get("done"):
            h["done"] = True
            h["ex"].stop_now()
        real(run_dir_, meta)

    monkeypatch.setattr(exmod, "write_meta", press_during_save)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    h["ex"] = ex
    out = ex.start()
    assert out.status == "cancelled"
    on_disk = load_meta(run_dir)  # a complete, parseable file
    st = on_disk["steps"]["s1"]
    assert st["status"] == "pending" and st["error"] == "cancelled" and st["resume_phase"] == "tests"
    monkeypatch.setattr(exmod, "write_meta", real)
    ex2, fb2 = executor(run_dir, rr, [])
    assert ex2.resume().status == "done" and fb2.requests == []


def test_interrupt_raised_inside_the_save_leaves_a_consistent_step(env, monkeypatch):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    import orch.executor as exmod

    real, state = exmod.write_meta, {"hit": False}

    def interrupted_write(run_dir_, meta):
        if meta["steps"].get("s1", {}).get("phase") == "review" and not state["hit"]:
            state["hit"] = True
            raise KeyboardInterrupt  # lands exactly while saving (nothing was written)
        real(run_dir_, meta)

    monkeypatch.setattr(exmod, "write_meta", interrupted_write)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})], **ON)
    out = ex.start()
    st = load_meta(run_dir)["steps"]["s1"]
    assert out.status == "cancelled" and st["status"] == "pending" and st["resume_phase"] == "review"
    ex2, fb2 = executor(run_dir, rr, [review("approve")], **ON)
    assert ex2.resume().status == "done" and len(fb2.requests) == 1


def test_interrupt_before_the_commit_resumes_at_commit(env, monkeypatch):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    real = workspace.commit_step

    def dies(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(workspace, "commit_step", dies)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    out = ex.start()
    monkeypatch.setattr(workspace, "commit_step", real)
    st = load_meta(run_dir)["steps"]["s1"]
    assert out.status == "cancelled" and st["status"] == "pending" and st["resume_phase"] == "commit" and st["commit"] is None
    ex2, fb2 = executor(run_dir, rr, [])
    assert ex2.resume().status == "done" and fb2.requests == []


def test_interrupt_right_after_git_commit_does_not_commit_twice(env, monkeypatch):
    repo, setup, executor = env
    run_dir, rr = one_step(setup)
    real = workspace.commit_step

    def commit_then_interrupt(*a, **k):
        real(*a, **k)
        raise KeyboardInterrupt

    monkeypatch.setattr(workspace, "commit_step", commit_then_interrupt)
    ex, _ = executor(run_dir, rr, [edit({"src/s1.py": "a\n"})])
    assert ex.start().status == "cancelled"
    monkeypatch.setattr(workspace, "commit_step", real)
    meta = load_meta(run_dir)
    assert meta["steps"]["s1"]["resume_phase"] == "commit"
    ex2, fb2 = executor(run_dir, rr, [])
    assert ex2.resume().status == "done" and fb2.requests == []
    wt = workspace.Path(meta["exec"]["worktree"])
    assert git(wt, "log", "--format=%s", f"{meta['exec']['base_sha']}..HEAD").splitlines() == ["orch(s1): Do s1"]


# ---------- the command line ----------


def installed_handler():
    return signal.getsignal(signal.SIGINT)


def test_cli_first_ctrl_c_cancels_and_exits_130(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    write_cfg(tmp_path, monkeypatch, repo)
    run_dir, _ = one_step(setup)
    before = signal.getsignal(signal.SIGINT)

    class Presses(FakeBuilder):
        def run(self, request):
            installed_handler()(signal.SIGINT, None)  # the user presses Ctrl-C once
            return cancelled_result()

    fb = Presses([])
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    r = CliRunner().invoke(cli.app, ["exec", run_dir.name, "--repo", "demo", "--plain"])
    assert r.exit_code == 130, r.output
    assert "Result: cancelled (s1)" in r.output and f"orch resume {run_dir.name}" in r.output and "stopped by you" in r.output
    assert "Traceback" not in r.output
    assert signal.getsignal(signal.SIGINT) is before  # restored once the run is over
    assert load_meta(run_dir)["steps"]["s1"]["status"] == "pending"


def test_cli_second_ctrl_c_stops_now_without_a_traceback(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    write_cfg(tmp_path, monkeypatch, repo)
    run_dir, _ = one_step(setup)

    class Presses(FakeBuilder):
        def run(self, request):
            handler = installed_handler()
            handler(signal.SIGINT, None)
            assert signal.getsignal(signal.SIGINT) is handler  # the default handler is NOT put back
            handler(signal.SIGINT, None)  # second press: raises KeyboardInterrupt here
            raise AssertionError("not reached")

    fb = Presses([])
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    r = CliRunner().invoke(cli.app, ["exec", run_dir.name, "--repo", "demo", "--plain"])
    assert r.exit_code == 130, r.output
    assert "Result: cancelled (s1)" in r.output and "Traceback" not in r.output
    st = load_meta(run_dir)["steps"]["s1"]
    assert st["status"] == "pending" and st["error"] == "cancelled"
    assert runlock.holder(run_dir) is None


def test_cli_third_ctrl_c_exits_at_once_and_the_run_is_still_resumable(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    write_cfg(tmp_path, monkeypatch, repo)
    run_dir, _ = one_step(setup)

    class Presses(FakeBuilder):
        def run(self, request):
            handler = installed_handler()
            handler(signal.SIGINT, None)
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGINT, None)
            handler(signal.SIGINT, None)  # third: SystemExit(130)
            raise AssertionError("not reached")

    fb = Presses([])
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb)
    r = CliRunner().invoke(cli.app, ["exec", run_dir.name, "--repo", "demo", "--plain"])
    assert r.exit_code == 130
    assert runlock.holder(run_dir) is None  # the OS lock is released with the process, here by the finally blocks
    fb2 = FakeBuilder([edit({"src/s1.py": "a\n"})])
    monkeypatch.setattr(cli, "get_adapter", lambda name: fb2)
    r = CliRunner().invoke(cli.app, ["resume", run_dir.name, "--repo", "demo", "--plain"])
    assert r.exit_code == 0, r.output


def test_cli_status_after_a_cancel_shows_the_resume_hint(env, tmp_path, monkeypatch):
    repo, setup, executor = env
    write_cfg(tmp_path, monkeypatch, repo)
    run_dir, rr = one_step(setup)
    h = {}
    ex, _ = executor(run_dir, rr, [cancels(h)])
    h["ex"] = ex
    ex.start()
    r = CliRunner().invoke(cli.app, ["status", run_dir.name, "--repo", "demo"])
    assert "status: cancelled" in r.output and f"orch resume {run_dir.name}" in r.output and "stopped by you" in r.output
