"""Fail closed: a repo with `sandbox: docker` never runs without Docker (docs/sandbox-design.md, decision 1)."""

from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from orch import cli, sandbox, services
from orch.config import ResolvedRepo, default_config_path
from orch.runs import create_run, write_meta
from orch.sandbox import ProbeResult

OK = ProbeResult(True, True, "sha256:abc", "29.9.3")
DOWN = ProbeResult(False, detail="failed to connect to the docker API")
NO_IMAGE = ProbeResult(True, False, "", "29.9.3", "image orch-sbx:py312 not found")


class Engine:
    """A docker engine that can change its mind between calls."""

    def __init__(self, *results):
        self.results, self.calls = list(results), 0

    def __call__(self, image):
        self.calls += 1
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class FakeExecutor:
    def __init__(self, repo):
        self.repo, self.called = repo, []

    def __getattr__(self, name):
        def act(**kw):
            self.called.append(name)
            return SimpleNamespace(status="done", step_id=None, message="")

        return act


def target(tmp_path, kind="docker"):
    return ResolvedRepo(name="demo", path=tmp_path, test_command="pytest -q", sandbox=kind)


# ---------- check_run_request ----------


def test_run_is_refused_before_anything_when_docker_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "docker_probe", Engine(DOWN))
    with pytest.raises(services.ServiceError) as e:
        services.check_run_request(target(tmp_path))
    assert e.value.code == 2
    assert "Docker is not running" in e.value.message and "sandbox: off" in e.value.message


def test_run_is_refused_when_the_image_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "docker_probe", Engine(NO_IMAGE))
    with pytest.raises(services.ServiceError, match="orch sandbox build"):
        services.check_run_request(target(tmp_path))


def test_run_is_allowed_with_docker_and_with_an_explicit_off(tmp_path, monkeypatch):
    eng = Engine(DOWN)
    monkeypatch.setattr(sandbox, "docker_probe", eng)
    services.check_run_request(target(tmp_path, "off"))  # explicit opt-out: Docker is not even asked
    assert eng.calls == 0
    monkeypatch.setattr(sandbox, "docker_probe", Engine(OK))
    services.check_run_request(target(tmp_path))


# ---------- drive: start, resume, approve and answer all re-check ----------


@pytest.mark.parametrize("action", ["start", "resume", "approve", "answer"])
def test_every_action_that_can_run_tests_is_refused_when_docker_is_down(tmp_path, monkeypatch, action):
    monkeypatch.setattr(sandbox, "docker_probe", Engine(DOWN))
    ex = FakeExecutor(target(tmp_path))
    with pytest.raises(services.ServiceError, match="Docker is not running"):
        services.drive(ex, action)
    assert ex.called == []  # the executor was never touched: no worktree, no model call, no test run


def test_reject_still_works_without_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "docker_probe", Engine(DOWN))
    ex = FakeExecutor(target(tmp_path))
    services.drive(ex, "reject", step_id="s1")
    assert ex.called == ["reject"]


@pytest.mark.parametrize("action", ["start", "resume", "approve", "answer"])
def test_an_off_repo_is_driven_without_asking_docker(tmp_path, monkeypatch, action):
    eng = Engine(DOWN)
    monkeypatch.setattr(sandbox, "docker_probe", eng)
    ex = FakeExecutor(target(tmp_path, "off"))
    services.drive(ex, action)
    assert ex.called == [action] and eng.calls == 0


def test_probe_ok_then_down_the_next_run_is_refused_even_within_30_seconds(tmp_path, monkeypatch):
    """The 30 s cache is for display only; a run always asks the engine again."""
    eng = Engine(OK, DOWN)
    monkeypatch.setattr(sandbox, "docker_probe", eng)
    t = target(tmp_path)
    services.check_run_request(t)  # first run: engine is up
    ex = FakeExecutor(t)
    with pytest.raises(services.ServiceError, match="Docker is not running"):
        services.drive(ex, "resume")  # a moment later the engine is gone
    assert ex.called == [] and eng.calls == 2


def test_a_cached_ok_from_the_display_path_does_not_let_a_run_through(tmp_path, monkeypatch):
    eng = Engine(OK, DOWN)
    monkeypatch.setattr(sandbox, "docker_probe", eng)
    assert sandbox.status("docker", "orch-sbx:py312").state == "ok"  # e.g. orch doctor just ran
    with pytest.raises(services.ServiceError):
        services.drive(FakeExecutor(target(tmp_path)), "start")


def test_a_failed_probe_is_not_remembered_when_docker_comes_back(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "docker_probe", Engine(DOWN, OK))
    t = target(tmp_path)
    with pytest.raises(services.ServiceError):
        services.check_run_request(t)
    services.check_run_request(t)  # Docker Desktop finished starting


# ---------- through the CLI ----------


def _cfg(tmp_path, monkeypatch, kind=None):
    data = yaml.safe_load(open(default_config_path(), encoding="utf-8"))
    repo = {"path": str(tmp_path), "test_command": "pytest -q"}
    if kind:
        repo["sandbox"] = kind
    data["repos"] = {"demo": repo}
    f = tmp_path / "config.yaml"
    f.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("ORCH_CONFIG", str(f))


def _no_agents(monkeypatch):
    def boom(name):
        raise AssertionError("an agent was started")

    monkeypatch.setattr(cli, "get_adapter", boom)


def test_cli_run_stops_with_a_clear_message_and_no_agent_call(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    _no_agents(monkeypatch)
    monkeypatch.setattr(sandbox, "docker_probe", Engine(DOWN))
    r = CliRunner().invoke(cli.app, ["run", "add a thing", "--repo", "demo"])
    assert r.exit_code == 2
    assert "Error: Docker is not running" in r.output and "Start Docker Desktop" in r.output
    assert "sandbox: off" in r.output and "Nothing was run" in r.output
    assert not (tmp_path / ".orch").exists()  # no run directory was created


def test_cli_exec_and_resume_stop_the_same_way(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    _no_agents(monkeypatch)
    run_dir = create_run(tmp_path, "add a thing")
    write_meta(run_dir, {"run_id": run_dir.name, "status": "planned"})
    monkeypatch.setattr(sandbox, "docker_probe", Engine(DOWN))
    for cmd in ("exec", "resume", "approve"):
        r = CliRunner().invoke(cli.app, [cmd, run_dir.name, "--repo", "demo", "--plain"])
        assert r.exit_code == 2 and "Docker is not running" in r.output, (cmd, r.output)
