"""Container clean-up and listing, against the fake engine (tests/docker_sim.py). No Docker is started."""

import pytest

from orch import sandbox
from orch.adapters.process import ProcessOutput
from orch.sandbox import SandboxError


def test_sweep_removes_every_container_of_the_run_and_only_those(fake_docker):
    mine_running = fake_docker.plant("run-1", "running")
    mine_exited = fake_docker.plant("run-1", "exited")
    other = fake_docker.plant("run-2", "running")
    assert sorted(sandbox.sweep("run-1")) == sorted([mine_running.id, mine_exited.id])
    assert list(fake_docker.boxes) == [other.name]
    assert fake_docker.calls[0] == ["docker", "ps", "-aq", "--filter", "label=orch.run=run-1"]
    assert fake_docker.calls[1][:3] == ["docker", "rm", "-f"]


def test_sweep_with_nothing_to_remove_makes_one_call(fake_docker):
    assert sandbox.sweep("run-1") == []
    assert fake_docker.verbs() == ["ps"]


def test_sweep_never_raises_and_says_how_to_check_by_hand(fake_docker):
    fake_docker.plant("run-1")
    fake_docker.fail["ps"] = ProcessOutput(stderr="error during connect", returncode=1)
    logs = []
    assert sandbox.sweep("run-1", logs.append) == []
    assert "docker ps -a --filter label=orch.run=run-1" in logs[0] and "error during connect" in logs[0]
    del fake_docker.fail["ps"]
    fake_docker.fail["rm"] = ProcessOutput(stderr="denied", returncode=1)
    logs.clear()
    assert sandbox.sweep("run-1", logs.append) == []
    assert "docker rm -f " in logs[0]
    fake_docker.fail["rm"] = ProcessOutput(timed_out=True)
    sandbox.sweep("run-1", logs.append)
    fake_docker.fail["ps"] = ProcessOutput(not_found=True)
    sandbox.sweep("run-1", logs.append)
    assert "not found on PATH" in logs[-1]


def test_sweep_ignores_anything_in_the_answer_that_is_not_a_container_id(fake_docker):
    fake_docker.fail["ps"] = ProcessOutput(stdout="--privileged\nabc123def456\n; rm\n", returncode=0)
    sandbox.sweep("run-1")
    rm = next(c for c in fake_docker.calls if c[1] == "rm")
    assert rm == ["docker", "rm", "-f", "abc123def456"]


def test_sweep_refuses_a_run_id_that_could_be_a_flag(fake_docker):
    with pytest.raises(SandboxError):
        sandbox.sweep("-a")
    assert fake_docker.calls == []


def test_list_containers_reads_label_and_status(fake_docker):
    a = fake_docker.plant("run-1", "running")
    b = fake_docker.plant("manual-ab12cd34", "exited")
    got = sandbox.list_containers()
    assert [(c.id, c.run) for c in got] == [(a.id, "run-1"), (b.id, "manual-ab12cd34")]
    assert got[0].status.startswith("Up") and got[1].status.startswith("Exited")
    argv = fake_docker.calls[0]
    assert argv[:5] == ["docker", "ps", "-a", "--filter", "label=orch.run"]
    assert argv[argv.index("--format") + 1] == '{{.ID}}|{{.Label "orch.run"}}|{{.Status}}'


def test_list_containers_raises_when_the_engine_cannot_answer(fake_docker):
    fake_docker.fail["ps"] = ProcessOutput(stderr="failed to connect", returncode=1)
    with pytest.raises(SandboxError, match="failed to connect"):
        sandbox.list_containers()


def test_the_sim_leaves_a_cancelled_container_running_until_it_is_swept(fake_docker, tmp_path):
    """The reason `sweep` exists: killing the docker CLI does not stop the container."""
    import sys
    import threading

    ev = threading.Event()
    threading.Timer(0.4, ev.set).start()
    argv = sandbox.test_argv(sandbox.Spec("img:1"), tmp_path, ["x"], "run-9", "orch-venv-abc")
    out = sandbox.docker_run(argv, inner=[sys.executable, "-c", "import time; time.sleep(60)"], src=tmp_path, env={},
                             timeout_s=60, cancel=ev)  # fmt: skip
    assert out.cancelled and len(fake_docker.running("run-9")) == 1
    sandbox.sweep("run-9")
    assert fake_docker.running() == []
