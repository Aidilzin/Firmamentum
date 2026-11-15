"""`orch sandbox build` / `orch sandbox check` and the files the image is built from. No Docker, no network."""

import re
import shlex
from pathlib import Path

import pytest
import yaml
from test_sandbox_gate import DOWN, NO_IMAGE, OK, Engine, _cfg
from typer.testing import CliRunner

from orch import cli, sandbox
from orch.adapters.process import ProcessOutput
from orch.config import default_config_path

ROOT = default_config_path().parent
invoke = lambda *a: CliRunner().invoke(cli.app, list(a))  # noqa: E731


# ---------- the files ----------


def test_sandbox_folder_has_a_dockerfile_and_an_lf_only_entry_script():
    d = sandbox.dockerfile_dir()
    assert d == ROOT / "sandbox"
    docker = (d / "Dockerfile").read_text(encoding="utf-8")
    entry = (d / "entry.sh").read_bytes()
    assert b"\r" not in entry and entry.startswith(b"#!/bin/sh\n")
    assert docker.startswith("# Test sandbox image") and "FROM python:3.12-slim" in docker
    assert re.search(r"COPY --from=ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+ ", docker)  # pinned, never :latest
    assert "USER 1000:1000" in docker and 'ENTRYPOINT ["/entry.sh"]' in docker
    assert "git" in docker and "rsync" in docker


def test_the_entry_script_copies_without_git_and_venv_then_execs_the_command():
    body = (sandbox.dockerfile_dir() / "entry.sh").read_text(encoding="utf-8")
    assert "--exclude .git" in body and "--exclude .venv" in body and "set -e" in body
    assert body.rstrip().endswith('exec "$@"')
    assert "/src/" in body and "/work/" in body


def test_gitattributes_keeps_the_sandbox_files_lf():
    rules = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "sandbox/* text eol=lf" in rules


# ---------- build_argv / build_image ----------


def test_build_argv(tmp_path):
    assert sandbox.build_argv("orch-sbx:py312", tmp_path) == ["docker", "build", "--tag", "orch-sbx:py312", str(tmp_path)]


@pytest.mark.parametrize("bad", ["--privileged", "-f", "a b", "", "x;y"])
def test_build_argv_refuses_a_name_that_could_be_a_flag(bad, tmp_path):
    with pytest.raises(sandbox.SandboxError):
        sandbox.build_argv(bad, tmp_path)


def test_build_image_runs_docker_build_in_the_sandbox_folder(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return ProcessOutput(returncode=0)

    monkeypatch.setattr(sandbox, "run_process", run)
    sandbox._cache["x"] = (0.0, OK)
    out = sandbox.build_image("orch-sbx:py312")
    assert out.returncode == 0
    assert seen["argv"] == ["docker", "build", "--tag", "orch-sbx:py312", str(sandbox.dockerfile_dir())]
    assert seen["cwd"] == sandbox.dockerfile_dir() and seen["timeout_s"] == sandbox.BUILD_TIMEOUT_S
    assert "ANTHROPIC_API_KEY" not in seen["env"] and sandbox._cache == {}  # scrubbed env; old probe answers dropped


def test_build_image_without_a_dockerfile_is_refused(tmp_path):
    with pytest.raises(sandbox.SandboxError, match="no Dockerfile"):
        sandbox.build_image("orch-sbx:py312", context=tmp_path)


# ---------- orch sandbox build ----------


def fake_build(monkeypatch, out):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return out

    monkeypatch.setattr(sandbox, "run_process", run)
    return calls


def test_cli_build_success_uses_the_configured_image(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    calls = fake_build(monkeypatch, ProcessOutput(returncode=0))
    r = invoke("sandbox", "build")
    assert r.exit_code == 0 and "Built orch-sbx:py312" in r.output
    assert calls[0][:3] == ["docker", "build", "--tag"] and calls[0][3] == "orch-sbx:py312"


def test_cli_build_image_option(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    calls = fake_build(monkeypatch, ProcessOutput(returncode=0))
    assert invoke("sandbox", "build", "--image", "orch-sbx:php83").exit_code == 0
    assert calls[0][3] == "orch-sbx:php83"


def test_cli_build_refuses_a_flag_shaped_image(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    calls = fake_build(monkeypatch, ProcessOutput(returncode=0))
    r = invoke("sandbox", "build", "--image", "--privileged")
    assert r.exit_code == 2 and calls == []


def test_cli_build_failure_shows_the_tail_and_exits_2(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    fake_build(monkeypatch, ProcessOutput(returncode=1, stderr="x" * 5000 + "\nERROR: pull access denied"))
    r = invoke("sandbox", "build")
    assert r.exit_code == 2 and "docker build failed (exit 1)" in r.output and "pull access denied" in r.output
    assert len(r.output) < 2500  # only the tail


def test_cli_build_without_docker_and_on_timeout(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    fake_build(monkeypatch, ProcessOutput(not_found=True))
    r = invoke("sandbox", "build")
    assert r.exit_code == 2 and "docker command was not found" in r.output
    fake_build(monkeypatch, ProcessOutput(timed_out=True))
    r = invoke("sandbox", "build")
    assert r.exit_code == 2 and "timed out" in r.output


# ---------- orch sandbox check ----------


def test_cli_check_prints_the_exact_command_and_runs_nothing(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(sandbox, "docker_probe", Engine(OK))
    ran = []
    monkeypatch.setattr(sandbox, "run_process", lambda *a, **k: ran.append(a))
    r = invoke("sandbox", "check", "--repo", "demo")
    assert r.exit_code == 0 and ran == []
    assert "repo demo: sandbox docker, image orch-sbx:py312" in r.output and "Docker: running (server 29.9.3)" in r.output
    argv = shlex.split(r.output.strip().splitlines()[-1])
    assert argv[:2] == ["docker", "run"] and "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert argv[-2:] == ["pytest", "-q"] and f"type=bind,src={tmp_path},dst=/src,readonly" in argv
    assert any(a.startswith("type=volume,src=orch-venv-") for a in argv)


def test_cli_check_uses_the_repo_image(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    f = Path(__import__("os").environ["ORCH_CONFIG"])
    d = yaml.safe_load(f.read_text(encoding="utf-8"))
    d["repos"]["demo"]["image"] = "orch-sbx:php83"
    f.write_text(yaml.safe_dump(d), encoding="utf-8")
    asked = []
    monkeypatch.setattr(sandbox, "docker_probe", lambda image: asked.append(image) or OK)
    r = invoke("sandbox", "check", "--repo", "demo")
    assert r.exit_code == 0 and "image orch-sbx:php83" in r.output and asked == ["orch-sbx:php83"]
    assert "orch-sbx:php83" in r.output.strip().splitlines()[-1]


def test_cli_check_with_docker_down_exits_2_with_the_fix(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(sandbox, "docker_probe", Engine(DOWN))
    r = invoke("sandbox", "check", "--repo", "demo")
    assert r.exit_code == 2 and "Docker is not running" in r.output and "Start Docker Desktop" in r.output


def test_cli_check_with_a_missing_image_points_at_build(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(sandbox, "docker_probe", Engine(NO_IMAGE))
    r = invoke("sandbox", "check", "--repo", "demo")
    assert r.exit_code == 2 and "orch sandbox build" in r.output


def test_cli_check_for_an_off_repo_warns_and_does_not_ask_docker(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch, kind="off")
    eng = Engine(DOWN)
    monkeypatch.setattr(sandbox, "docker_probe", eng)
    r = invoke("sandbox", "check", "--repo", "demo")
    assert r.exit_code == 0 and "WARNING: " + sandbox.OFF_WARNING in r.output and eng.calls == 0
