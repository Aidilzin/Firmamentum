"""Run lock, meta.json write retry and stale-run detection. No real CLI."""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest
from git_helpers import make_repo
from plan_factory import good_plan, step
from typer.testing import CliRunner

from orch import cli, runlock, runs
from orch.config import ResolvedRepo, load_config
from orch.executor import ExecError, Executor
from orch.runs import create_run, is_stale, list_runs, load_meta, write_meta

PASS_CMD = 'python -c "import sys; sys.exit(0)"'


# ---------- the lock itself ----------


def test_second_acquire_is_refused_then_works_after_release(tmp_path):
    first = runlock.acquire(tmp_path)
    with pytest.raises(runlock.RunLocked) as e:
        runlock.acquire(tmp_path)
    assert e.value.info.pid == os.getpid()
    first.release()
    runlock.acquire(tmp_path).release()


def test_holder_is_none_without_a_lock_file_and_creates_none(tmp_path):
    assert runlock.holder(tmp_path) is None
    assert not (tmp_path / "lock").exists()


def test_holder_reports_the_owner_and_none_after_release(tmp_path):
    lock = runlock.acquire(tmp_path)
    info = runlock.holder(tmp_path)
    assert info is not None and info.pid == os.getpid() and info.started_at
    lock.release()
    assert runlock.holder(tmp_path) is None


def test_lock_held_by_another_process_dies_with_it(tmp_path):
    code = (
        "import sys, time\n"
        "from orch import runlock\n"
        "l = runlock.acquire(__import__('pathlib').Path(sys.argv[1]))\n"
        "print('locked', __import__('os').getpid(), flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        word, pid = child.stdout.readline().split()  # the venv launcher may add a process, so ask the child itself
        assert word == "locked"
        info = runlock.holder(tmp_path)  # readable while held
        assert info is not None and info.pid == int(pid) != os.getpid()
        with pytest.raises(runlock.RunLocked):
            runlock.acquire(tmp_path)
    finally:
        os.kill(int(pid), signal.SIGTERM)  # the real locking process (Popen.pid may only be the venv launcher)
        child.kill()
        child.wait()
    deadline = time.time() + 5
    while runlock.holder(tmp_path) is not None and time.time() < deadline:
        time.sleep(0.1)
    assert runlock.holder(tmp_path) is None  # the OS dropped it with the process


def test_beat_rewrites_the_heartbeat_without_losing_the_lock(tmp_path, monkeypatch):
    lock = runlock.acquire(tmp_path)
    before = runlock.holder(tmp_path).heartbeat
    monkeypatch.setattr(runlock, "BEAT_EVERY_S", 0.0)
    time.sleep(1.1)
    lock.beat()
    after = runlock.holder(tmp_path)
    assert after is not None and after.heartbeat >= before and after.pid == os.getpid()
    lock.release()


# ---------- meta.json write / read retry ----------


def test_write_meta_retries_on_permission_error(tmp_path, monkeypatch):
    real, calls = os.replace, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError("Access is denied")
        return real(a, b)

    monkeypatch.setattr(runs.os, "replace", flaky)
    monkeypatch.setattr(runs, "FILE_RETRY_S", 0)
    write_meta(tmp_path, {"status": "x"})
    assert len(calls) == 3 and load_meta(tmp_path) == {"status": "x"}
    assert [p.name for p in tmp_path.iterdir()] == ["meta.json"]


def test_write_meta_gives_up_after_bounded_tries_and_leaves_no_tmp(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runs.os, "replace", lambda a, b: calls.append(1) or (_ for _ in ()).throw(PermissionError("denied")))
    monkeypatch.setattr(runs, "FILE_RETRY_S", 0)
    with pytest.raises(PermissionError):
        write_meta(tmp_path, {"status": "x"})
    assert len(calls) == runs.FILE_RETRIES
    assert list(tmp_path.iterdir()) == []


def test_tmp_names_are_unique_per_thread(tmp_path, monkeypatch):
    names = []
    real = os.replace
    monkeypatch.setattr(runs.os, "replace", lambda a, b: names.append(os.path.basename(a)) or real(a, b))
    write_meta(tmp_path, {"a": 1})
    t = threading.Thread(target=write_meta, args=(tmp_path, {"a": 2}))
    t.start()
    t.join()
    assert len(set(names)) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing")
def test_write_meta_survives_a_reader_that_holds_the_file_open(tmp_path, monkeypatch):
    write_meta(tmp_path, {"n": 1})
    monkeypatch.setattr(runs, "FILE_RETRY_S", 0.05)
    reader = open(tmp_path / "meta.json", "rb")
    threading.Timer(0.3, reader.close).start()
    write_meta(tmp_path, {"n": 2})  # would raise PermissionError without the retry
    assert load_meta(tmp_path) == {"n": 2}


# ---------- executor refuses a locked run ----------


class CountingAdapter:
    name = "claude"

    def __init__(self):
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        raise AssertionError("no agent call expected")


@pytest.fixture
def started(tmp_path):
    repo = make_repo(tmp_path / "proj")
    run_dir = create_run(repo, "do the thing")
    (run_dir / "plan.json").write_text(json.dumps(good_plan(steps=[step("s1")])), encoding="utf-8")
    write_meta(run_dir, {"run_id": run_dir.name, "status": "planned", "total_tokens": 0})
    rr = ResolvedRepo(name="demo", path=repo, test_command=PASS_CMD)
    adapter = CountingAdapter()
    cfg = load_config().model_copy(update={"review": "off", "max_repair_rounds": 0})
    return run_dir, Executor(rr, run_dir, cfg, lambda n: adapter), adapter


@pytest.mark.parametrize("call", [
    lambda ex: ex.start(), lambda ex: ex.resume(), lambda ex: ex.approve(None), lambda ex: ex.answer(None, "x"),
    lambda ex: ex.reject("s1"),
])
def test_every_entry_point_refuses_a_locked_run(started, call):
    run_dir, ex, adapter = started
    before = (run_dir / "meta.json").read_bytes()
    held = runlock.acquire(run_dir)
    try:
        with pytest.raises(ExecError, match="being executed by pid"):
            call(ex)
    finally:
        held.release()
    assert adapter.requests == [] and (run_dir / "meta.json").read_bytes() == before


def test_lock_is_released_after_a_crash(started):
    run_dir, ex, _ = started
    with pytest.raises(AssertionError):  # the adapter raises on the first builder call
        ex.start()
    assert runlock.holder(run_dir) is None
    assert load_meta(run_dir)["steps"]["s1"]["status"] == "running"  # what a crash leaves behind


def test_lock_is_held_during_the_run_and_released_after(started):
    run_dir, ex, adapter = started
    seen = []
    adapter.run = lambda req: seen.append(runlock.holder(run_dir)) or (_ for _ in ()).throw(RuntimeError("stop"))
    with pytest.raises(RuntimeError):
        ex.start()
    assert seen and seen[0].pid == os.getpid()
    assert runlock.holder(run_dir) is None


# ---------- stale detection and display ----------


def _crashed_run(tmp_path, status="executing"):
    repo = make_repo(tmp_path / "proj")
    run_dir = create_run(repo, "do the thing")
    write_meta(run_dir, {"run_id": run_dir.name, "status": status, "total_tokens": 0, "created": "2026-11-12T10:00:00+00:00",
                         "steps": {"s1": {"title": "Do s1", "status": "running", "phase": "build"}}})
    return repo, run_dir


def test_is_stale_only_for_executing_without_a_holder(tmp_path):
    _, run_dir = _crashed_run(tmp_path)
    meta = load_meta(run_dir)
    assert is_stale(run_dir, meta)
    lock = runlock.acquire(run_dir)
    assert not is_stale(run_dir, meta)
    lock.release()
    assert not is_stale(run_dir, {**meta, "status": "paused"})


def test_list_runs_marks_stale(tmp_path):
    repo, run_dir = _crashed_run(tmp_path)
    [info] = list_runs(repo)
    assert info.stale and info.shown_status == "executing (stale)" and info.status == "executing"
    lock = runlock.acquire(run_dir)
    [info] = list_runs(repo)
    lock.release()
    assert not info.stale and info.shown_status == "executing"


def test_runs_and_status_commands_show_stale_and_a_resume_hint(tmp_path, monkeypatch):
    repo, run_dir = _crashed_run(tmp_path)
    monkeypatch.chdir(repo)
    runner = CliRunner()
    out = runner.invoke(cli.app, ["runs", "--repo", str(repo)]).output
    assert "executing (stale)" in out
    out = runner.invoke(cli.app, ["status", run_dir.name, "--repo", str(repo)]).output
    assert "status: executing (stale)" in out
    assert f"orch resume {run_dir.name}" in out and "the run died mid-step" in out


def test_a_live_run_shows_plain_executing_and_no_resume_hint(tmp_path, monkeypatch):
    repo, run_dir = _crashed_run(tmp_path)
    monkeypatch.chdir(repo)
    lock = runlock.acquire(run_dir)
    try:
        out = CliRunner().invoke(cli.app, ["status", run_dir.name, "--repo", str(repo)]).output
    finally:
        lock.release()
    assert "status: executing" in out and "(stale)" not in out and "died mid-step" not in out
