"""sandbox.run_tests against the fake engine: test failure versus container failure, and clean-up on every exit."""

import sys
import threading
from contextlib import contextmanager

import pytest

from orch import sandbox
from orch.adapters.process import ProcessOutput
from orch.config import ResolvedRepo, SandboxConfig
from orch.sandbox import ProbeResult

CFG = SandboxConfig()
PASS = [sys.executable, "-c", "import sys; sys.exit(0)"]
FAIL = [sys.executable, "-c", "import sys; print('1 failed'); sys.exit(1)"]
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


def target(tmp_path, **kw):
    return ResolvedRepo(name="demo", path=tmp_path, test_command="t", **kw)


def go(tmp_path, command=PASS, run_id="run-1", **kw):
    return sandbox.run_tests(target(tmp_path), CFG, tmp_path, command, run_id, timeout_s=kw.pop("timeout_s", 30), **kw)


def exits(code, *, inspect=None, stderr="", oom=False, error="", status="exited"):
    """A docker run that ends with CLI exit `code`, while the engine's record says `inspect` (default: the same)."""

    def run(sim, box, argv):
        box.status, box.exit_code, box.oom, box.error = status, code if inspect is None else inspect, oom, error
        return ProcessOutput(stdout="out", stderr=stderr, returncode=code)

    return run


# ---------- a test failure is a test failure ----------


def test_a_passing_run_and_a_failing_run(tmp_path, fake_docker):
    ok = go(tmp_path, PASS)
    assert ok.kind == "ran" and ok.exit_code == 0 and ok.passed
    bad = go(tmp_path, FAIL)
    assert bad.kind == "ran" and bad.exit_code == 1 and not bad.passed and "1 failed" in bad.stdout


def test_the_run_uses_test_argv_with_the_label_and_a_scrubbed_env(tmp_path, fake_docker, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-nope")
    monkeypatch.setenv("MY_TOKEN", "v")
    sandbox.run_tests(target(tmp_path, env_passthrough=["MY_TOKEN"]), CFG, tmp_path, PASS, "run-1", timeout_s=30)
    run = fake_docker.runs[0]
    assert run["argv"][:2] == ["docker", "run"] and "orch.run=run-1" in run["argv"] and run["src"] == tmp_path
    assert run["inner"] == PASS and "ANTHROPIC_API_KEY" not in run["env"] and run["env"]["MY_TOKEN"] == "v"
    assert "v" not in run["argv"]  # the value never appears on the command line


@pytest.mark.parametrize("code", [1, 2, 5, 125, 130])
def test_any_ordinary_exit_code_is_a_test_failure_when_the_engine_agrees(tmp_path, fake_docker, code):
    fake_docker.script = [exits(code)]
    res = go(tmp_path)
    assert (res.kind, res.exit_code) == ("ran", code)


# ---------- a container failure is never a test failure ----------


def test_missing_image(tmp_path, fake_docker):
    def run(sim, box, argv):
        del sim.boxes[box.name]  # docker never created it
        return ProcessOutput(stderr="Unable to find image 'orch-sbx:py312' locally\ndocker: Error", returncode=125)

    fake_docker.script = [run]
    res = go(tmp_path)
    assert res.kind == "infra" and "Unable to find image" in res.detail


def test_engine_stopped_mid_run(tmp_path, fake_docker):
    fake_docker.script = [exits(1, stderr="error during connect: the docker daemon is not running", status="running")]
    fake_docker.fail["inspect"] = ProcessOutput(stderr="error during connect", returncode=1)
    res = go(tmp_path)
    assert res.kind == "infra" and "stopped mid-run" in res.detail and "error during connect" in res.detail


def test_engine_stopped_mid_run_but_inspect_still_answers_running(tmp_path, fake_docker):
    fake_docker.script = [exits(1, status="running")]
    res = go(tmp_path)
    assert res.kind == "infra" and "lost contact" in res.detail


def test_container_created_but_never_started(tmp_path, fake_docker):
    fake_docker.script = [exits(125, status="created", stderr="docker: Error response from daemon")]
    assert "could not start" in go(tmp_path).detail
    fake_docker.script = [exits(126, error="OCI runtime create failed: no such file")]
    res = go(tmp_path)
    assert res.kind == "infra" and "could not start" in res.detail and "OCI runtime" in res.detail


def test_out_of_memory_is_not_a_test_failure(tmp_path, fake_docker):
    fake_docker.script = [exits(137, oom=True)]
    res = go(tmp_path)
    assert res.kind == "infra" and "out of memory (limit 4g)" in res.detail and "sandbox.memory" in res.detail


def test_killed_without_oom(tmp_path, fake_docker):
    fake_docker.script = [exits(137)]
    res = go(tmp_path)
    assert res.kind == "infra" and "killed from outside" in res.detail


def test_cli_and_engine_disagree_on_the_exit_code(tmp_path, fake_docker):
    fake_docker.script = [exits(1, inspect=0)]
    res = go(tmp_path)
    assert res.kind == "infra" and "lost contact" in res.detail


def test_unreadable_inspect_answer(tmp_path, fake_docker):
    fake_docker.script = [exits(1)]
    fake_docker.fail["inspect"] = ProcessOutput(stdout="garbage", returncode=0)
    assert go(tmp_path).kind == "infra"


def test_source_copy_failed(tmp_path, fake_docker):
    fake_docker.script = [exits(97, stderr="orch sandbox: copying /src to /work failed")]
    res = go(tmp_path)
    assert res.kind == "infra" and "copying the source" in res.detail


@pytest.mark.parametrize("code", [126, 127])
def test_command_not_found_in_the_image_is_unrunnable(tmp_path, fake_docker, code):
    fake_docker.script = [exits(code)]
    res = go(tmp_path)
    assert res.kind == "unrunnable" and res.exit_code == code


def test_a_test_that_exits_96_stops_the_run_for_you_it_never_passes(tmp_path, fake_docker):
    fake_docker.script = [exits(96)]
    res = go(tmp_path)
    assert res.kind == "infra" and not res.passed and "not prepared" in res.detail


def test_no_docker_command(tmp_path, fake_docker):
    fake_docker.script = [lambda sim, box, argv: ProcessOutput(not_found=True, error="executable not found: docker")]
    res = go(tmp_path)
    assert res.kind == "infra" and "not found on PATH" in res.detail


def test_engine_down_before_the_run_starts_nothing(tmp_path, fake_docker, monkeypatch):
    monkeypatch.setattr(sandbox, "docker_probe", lambda image: ProbeResult(False, detail="failed to connect"))
    res = go(tmp_path)
    assert res.kind == "infra" and "Docker is not running" in res.detail and "Nothing was run" in res.detail
    assert fake_docker.runs == []


def test_a_repo_with_sandbox_off_never_comes_here(tmp_path):
    with pytest.raises(sandbox.SandboxError):
        sandbox.run_tests(target(tmp_path, sandbox="off"), CFG, tmp_path, PASS, "r")


def test_a_bad_run_id_is_infra_not_a_crash(tmp_path, fake_docker):
    res = go(tmp_path, run_id="-x")
    assert res.kind == "infra" and "run id" in res.detail and fake_docker.runs == []


# ---------- timeout, cancel and clean-up ----------


def test_timeout_is_timed_out_and_the_container_is_gone(tmp_path, fake_docker):
    res = go(tmp_path, SLEEP, timeout_s=1)
    assert res.kind == "timed_out"
    assert fake_docker.boxes == {} and fake_docker.running() == []


def test_cancel_stops_the_container_too_not_just_the_docker_cli(tmp_path, fake_docker):
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    seen = {}

    def spy(argv):  # the moment the sweep starts, the container is still running
        if argv[1] == "ps":
            seen.setdefault("running_before_sweep", fake_docker.running("run-1"))

    fake_docker.on_call = spy
    res = go(tmp_path, SLEEP, timeout_s=60, cancel=ev)
    assert res.kind == "cancelled"
    assert len(seen["running_before_sweep"]) == 1  # proves the test would catch a missing sweep
    assert fake_docker.running() == [] and fake_docker.boxes == {}


def test_cancel_already_set_starts_no_container(tmp_path, fake_docker):
    ev = threading.Event()
    ev.set()
    res = go(tmp_path, cancel=ev)
    assert res.kind == "cancelled" and fake_docker.boxes == {}


@pytest.mark.parametrize("script", [exits(0), exits(1), exits(137, oom=True)])
def test_the_run_label_is_swept_after_every_outcome(tmp_path, fake_docker, script):
    fake_docker.script = [script]
    go(tmp_path)
    assert fake_docker.boxes == {}
    assert fake_docker.verbs()[-2:] == ["ps", "rm"]


def test_an_exception_or_ctrl_c_inside_the_run_still_sweeps(tmp_path, fake_docker):
    def boom(sim, box, argv):
        raise KeyboardInterrupt

    fake_docker.script = [boom]
    with pytest.raises(KeyboardInterrupt):
        go(tmp_path)
    assert fake_docker.boxes == {}


def test_other_runs_containers_survive_the_sweep(tmp_path, fake_docker):
    other = fake_docker.plant("run-2", "running")
    go(tmp_path)
    assert list(fake_docker.boxes) == [other.name]


def test_a_failing_sweep_warns_and_the_result_stands(tmp_path, fake_docker):
    fake_docker.script = [exits(1)]
    fake_docker.fail["rm"] = ProcessOutput(stderr="denied", returncode=1)
    logs = []
    res = go(tmp_path, log=logs.append)
    assert res.kind == "ran" and res.exit_code == 1
    assert any("docker rm -f" in m for m in logs)


# ---------- own_only: the agent's orch-test next to another call of the same run ----------


def test_own_only_leaves_a_sibling_container_of_the_same_run_alone(tmp_path, fake_docker):
    sibling = fake_docker.plant("run-1", "running")  # same run label, started by another orch-test call
    res = go(tmp_path, own_only=True)
    assert res.kind == "ran"
    assert list(fake_docker.boxes) == [sibling.name]  # its own container is gone, the sibling is not


def test_the_default_still_sweeps_the_whole_run_label(tmp_path, fake_docker):
    fake_docker.plant("run-1", "running")
    go(tmp_path)
    assert fake_docker.boxes == {}


def test_own_only_removes_its_container_after_cancel_timeout_and_ctrl_c(tmp_path, fake_docker):
    keep = fake_docker.plant("run-1", "running")
    ev = threading.Event()
    ev.set()
    ev2 = threading.Event()

    def cancel_midway(sim, box, argv):
        ev2.set()
        return ProcessOutput(returncode=None, cancelled=True)

    fake_docker.script = [cancel_midway]
    res = go(tmp_path, own_only=True, cancel=ev2)
    assert res.kind == "cancelled" and list(fake_docker.boxes) == [keep.name]
    fake_docker.script = [lambda sim, box, argv: ProcessOutput(timed_out=True)]
    assert go(tmp_path, own_only=True).kind == "timed_out" and list(fake_docker.boxes) == [keep.name]

    def boom(sim, box, argv):
        raise KeyboardInterrupt

    fake_docker.script = [boom]
    with pytest.raises(KeyboardInterrupt):
        go(tmp_path, own_only=True)
    assert list(fake_docker.boxes) == [keep.name]


def test_own_only_also_removes_the_warm_container(tmp_path, fake_docker):
    keep = fake_docker.plant("run-1", "running")
    fake_docker.script = [exits(96), exits(0), exits(0)]
    res = go(tmp_path, own_only=True, warm_src=lambda: _folder(tmp_path))
    assert res.kind == "ran" and list(fake_docker.boxes) == [keep.name]


@contextmanager
def _folder(path):
    yield path


# ---------- the warm step ----------


def warm_folder(tmp_path):
    """A `warm_src` that hands out a scratch folder and records that it was cleaned up."""
    folder = tmp_path / "warm-src"
    state = {"entered": 0, "left": 0}

    @contextmanager
    def src():
        state["entered"] += 1
        folder.mkdir(exist_ok=True)
        try:
            yield folder
        finally:
            state["left"] += 1

    return src, folder, state


def test_not_ready_then_warm_then_the_same_run_again(tmp_path, fake_docker):
    src, folder, state = warm_folder(tmp_path)
    fake_docker.script = [exits(96), exits(0)]  # the third docker run is the tests, on the host
    logs = []
    res = go(tmp_path, FAIL, warm_src=src, log=logs.append)
    assert res.kind == "ran" and res.exit_code == 1  # the tests ran after the warm and failed on their own
    first, warm, again = (r["argv"] for r in fake_docker.runs)
    assert "--network" in first and "--network" in again and "--network" not in warm  # only the warm has network
    assert f"type=bind,src={folder},dst=/src,readonly" in warm and f"type=bind,src={tmp_path},dst=/src,readonly" in first
    assert warm[warm.index("--name") + 1] not in (first[first.index("--name") + 1], again[again.index("--name") + 1])
    assert warm[-3:] == ["sh", "-c", "uv sync --frozen && touch /venv/.orch-ready"]
    assert fake_docker.runs[1]["inner"] == sandbox.WARM_COMMAND and fake_docker.runs[2]["inner"] == FAIL
    assert state == {"entered": 1, "left": 1}  # the folder is released as soon as the warm container is done
    assert any("preparing the test environment" in m for m in logs)
    assert fake_docker.boxes == {}


def test_an_environment_that_is_ready_is_never_warmed(tmp_path, fake_docker):
    src, _, state = warm_folder(tmp_path)
    assert go(tmp_path, PASS, warm_src=src).passed
    assert len(fake_docker.runs) == 1 and state["entered"] == 0


def test_still_not_ready_after_the_warm_does_not_loop(tmp_path, fake_docker):
    src, _, state = warm_folder(tmp_path)
    fake_docker.script = [exits(96), exits(0), exits(96)]
    res = go(tmp_path, warm_src=src)
    assert res.kind == "infra" and "still not ready" in res.detail
    assert len(fake_docker.runs) == 3 and state["entered"] == 1


def test_a_failed_warm_is_infra_with_the_last_line_of_its_output(tmp_path, fake_docker):
    src, _, _ = warm_folder(tmp_path)

    def warm_fails(sim, box, argv):
        box.status, box.exit_code = "exited", 2
        return ProcessOutput(stderr="resolving...\nerror: lockfile out of date\n", returncode=2)

    fake_docker.script = [exits(96), warm_fails]
    res = go(tmp_path, warm_src=src)
    assert res.kind == "infra" and "exit 2" in res.detail and "lockfile out of date" in res.detail
    assert len(fake_docker.runs) == 2  # the tests did not run


def test_a_warm_that_cannot_start_is_infra(tmp_path, fake_docker):
    src, _, _ = warm_folder(tmp_path)
    fake_docker.script = [exits(96), exits(137, oom=True)]
    res = go(tmp_path, warm_src=src)
    assert res.kind == "infra" and "out of memory" in res.detail and "preparing" in res.detail


def test_a_warm_that_times_out_is_infra_and_its_container_is_gone(tmp_path, fake_docker, monkeypatch):
    src, _, _ = warm_folder(tmp_path)
    monkeypatch.setattr(sandbox, "WARM_TIMEOUT_S", 1)
    monkeypatch.setattr(sandbox, "WARM_COMMAND", SLEEP)  # the sim runs `inner` on the host
    fake_docker.script = [exits(96)]
    res = go(tmp_path, warm_src=src)
    assert res.kind == "infra" and "timed out" in res.detail
    assert fake_docker.boxes == {}


def test_cancel_during_the_warm(tmp_path, fake_docker, monkeypatch):
    src, _, _ = warm_folder(tmp_path)
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    monkeypatch.setattr(sandbox, "WARM_COMMAND", SLEEP)
    fake_docker.script = [exits(96)]
    res = go(tmp_path, warm_src=src, cancel=ev)
    assert res.kind == "cancelled" and fake_docker.boxes == {}


def test_a_refused_warm_source_stops_before_any_warm_container(tmp_path, fake_docker):
    @contextmanager
    def refused():
        raise sandbox.SandboxError("dependency change not approved: uv.lock")
        yield  # pragma: no cover

    fake_docker.script = [exits(96)]
    res = go(tmp_path, warm_src=refused)
    assert res.kind == "infra" and "dependency change not approved: uv.lock" in res.detail
    assert len(fake_docker.runs) == 1


def test_without_a_warm_source_a_missing_environment_is_infra(tmp_path, fake_docker):
    fake_docker.script = [exits(96)]
    assert go(tmp_path).kind == "infra"
