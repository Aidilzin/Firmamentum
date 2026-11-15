"""orch.sandbox: argv builders, probe cache, fail-closed `require`. No Docker is ever started."""

import pytest

from orch import sandbox
from orch.adapters.process import ProcessOutput
from orch.config import ResolvedRepo, SandboxConfig
from orch.sandbox import ProbeResult, SandboxError, SandboxUnavailable, Spec

SPEC = Spec(image="orch-sbx:py312", memory="4g", cpus=4)
OK = ProbeResult(True, True, "sha256:abc", "29.9.3")
DOWN = ProbeResult(False, detail="failed to connect to the docker API")
NO_IMAGE = ProbeResult(True, False, "", "29.9.3", "image orch-sbx:py312 not found")


def pairs(argv, flag):
    """Every value that follows `flag` in argv."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def repo(tmp_path, sandbox_kind="docker", name="demo"):
    return ResolvedRepo(name=name, path=tmp_path, test_command="pytest -q", sandbox=sandbox_kind)


# ---------- test_argv ----------


def test_argv_is_closed_by_default(tmp_path):
    a = sandbox.test_argv(SPEC, tmp_path, ["uv", "run", "pytest", "-q"], "run-1", "orch-venv-abc")
    assert a[:2] == ["docker", "run"]
    assert pairs(a, "--network") == ["none"]
    assert pairs(a, "--user") == ["1000:1000"]
    assert pairs(a, "--cap-drop") == ["ALL"]
    assert pairs(a, "--security-opt") == ["no-new-privileges"]
    assert "--read-only" in a and "--rm" in a
    assert pairs(a, "--label") == ["orch.run=run-1"]
    assert pairs(a, "--pids-limit") == ["512"]
    assert pairs(a, "--memory") == ["4g"] and pairs(a, "--cpus") == ["4"]
    assert "--privileged" not in a and "--env-file" not in a and "--network=host" not in a


def test_source_and_venv_are_read_only_mounts_and_nothing_else_is_mounted(tmp_path):
    a = sandbox.test_argv(SPEC, tmp_path, ["pytest"], "r", "orch-venv-abc")
    mounts = pairs(a, "--mount")
    assert mounts == [f"type=bind,src={tmp_path},dst=/src,readonly", "type=volume,src=orch-venv-abc,dst=/venv,readonly"]
    assert "-v" not in a and "--volume" not in a


def test_command_follows_the_image_and_is_not_split_or_joined(tmp_path):
    cmd = ["uv", "run", "pytest", "-q", "-k", "a and b", "--", "$(whoami)"]
    a = sandbox.test_argv(SPEC, tmp_path, cmd, "r", "orch-venv-abc")
    i = a.index(SPEC.image)
    assert a[i + 1 :] == cmd  # one element each: a space or `$()` in an argument stays inside that argument
    assert a[i - 2 : i] == ["-w", "/work"]


def test_uv_is_offline_and_the_cache_is_on_the_tmpfs(tmp_path):
    a = sandbox.test_argv(SPEC, tmp_path, ["pytest"], "r", "orch-venv-abc")
    envs = pairs(a, "-e")
    for want in ("UV_PROJECT_ENVIRONMENT=/venv", "UV_NO_SYNC=1", "UV_OFFLINE=1", "UV_PYTHON_DOWNLOADS=never",
                 "UV_CACHE_DIR=/tmp/uvc"):  # fmt: skip
        assert want in envs


def test_no_host_environment_crosses_only_named_passthrough_without_values(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "super-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-nope")
    a = sandbox.test_argv(SPEC, tmp_path, ["pytest"], "r", "orch-venv-abc", passthrough=["MY_TOKEN", "ANTHROPIC_API_KEY"])
    envs = pairs(a, "-e")
    assert "MY_TOKEN" in envs  # name only: docker reads the value from its own environment
    assert not any("super-secret-value" in x for x in a)
    assert not any(x.startswith("ANTHROPIC_") for x in envs) and "sk-ant-nope" not in " ".join(a)
    plain = sandbox.test_argv(SPEC, tmp_path, ["pytest"], "r", "orch-venv-abc")
    assert all("=" in e for e in pairs(plain, "-e"))  # only the fixed UV_* settings


def test_limits_come_from_the_spec(tmp_path):
    a = sandbox.test_argv(Spec("img:1", "2g", 2), tmp_path, ["pytest"], "r", "orch-venv-abc")
    assert pairs(a, "--memory") == ["2g"] and pairs(a, "--cpus") == ["2"] and a[a.index("-w") + 2] == "img:1"


@pytest.mark.parametrize("bad", ["a,b", 'a"b', "a'b", "a\nb"])
def test_a_source_path_that_could_add_mount_options_is_refused(tmp_path, bad):
    with pytest.raises(SandboxError):
        sandbox.test_argv(SPEC, tmp_path / bad, ["pytest"], "r", "orch-venv-abc")


def test_a_relative_source_path_is_refused():
    with pytest.raises(SandboxError):
        sandbox.test_argv(SPEC, __import__("pathlib").Path("rel/dir"), ["pytest"], "r", "orch-venv-abc")


@pytest.mark.parametrize("run_id", ["", "-x", "a b", "a=b", "a,b"])
def test_bad_run_ids_are_refused(tmp_path, run_id):
    with pytest.raises(SandboxError):
        sandbox.test_argv(SPEC, tmp_path, ["pytest"], run_id, "orch-venv-abc")


@pytest.mark.parametrize("vol", ["", "-v", "a,b", "a:b", "../x"])
def test_bad_volume_names_are_refused(tmp_path, vol):
    with pytest.raises(SandboxError):
        sandbox.test_argv(SPEC, tmp_path, ["pytest"], "r", vol)


@pytest.mark.parametrize("name", ["", "A B", "X=1", "1X", "A-B"])
def test_bad_passthrough_names_are_refused(tmp_path, name):
    with pytest.raises(SandboxError):
        sandbox.test_argv(SPEC, tmp_path, ["pytest"], "r", "orch-venv-abc", passthrough=[name])


@pytest.mark.parametrize("cmd", [[], [""]])
def test_an_empty_command_is_refused(tmp_path, cmd):
    with pytest.raises(SandboxError):
        sandbox.test_argv(SPEC, tmp_path, cmd, "r", "orch-venv-abc")


def test_spec_for_repo_uses_the_repo_image_and_the_config_limits(tmp_path):
    r = ResolvedRepo(name="x", path=tmp_path, test_command="t", image="orch-sbx:php83")
    s = Spec.for_repo(r, SandboxConfig(memory="2g", cpus=2))
    assert s == Spec("orch-sbx:php83", "2g", 2)


# ---------- warm_argv ----------


def test_warm_step_has_network_and_a_writable_venv_but_the_same_lockdown(tmp_path):
    a = sandbox.warm_argv(SPEC, tmp_path, "r", "orch-venv-abc")
    assert "--network" not in a  # the one place with network
    mounts = pairs(a, "--mount")
    assert f"type=bind,src={tmp_path},dst=/src,readonly" in mounts
    assert "type=volume,src=orch-venv-abc,dst=/venv" in mounts and not any(m.endswith("/venv,readonly") for m in mounts)
    assert "type=volume,src=orch-uv-cache,dst=/uvcache" in mounts
    assert {"UV_LINK_MODE=copy", "UV_CACHE_DIR=/uvcache", "UV_PROJECT_ENVIRONMENT=/venv"} <= set(pairs(a, "-e"))
    assert a[a.index(SPEC.image) + 1 :] == ["uv", "sync", "--frozen"]
    assert pairs(a, "--cap-drop") == ["ALL"] and "--read-only" in a and pairs(a, "--user") == ["1000:1000"]
    assert pairs(a, "--label") == ["orch.run=r"]


# ---------- venv volume name ----------


def test_volume_name_changes_with_the_lockfile_pyproject_and_image(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='a'\n")
    (tmp_path / "uv.lock").write_text("v1")
    base = sandbox.venv_volume_name(tmp_path, "sha256:1")
    assert base.startswith("orch-venv-") and len(base) == len("orch-venv-") + 12
    assert sandbox.venv_volume_name(tmp_path, "sha256:1") == base
    assert sandbox.venv_volume_name(tmp_path, "sha256:2") != base
    (tmp_path / "uv.lock").write_text("v2")
    assert sandbox.venv_volume_name(tmp_path, "sha256:1") != base
    (tmp_path / "uv.lock").write_text("v1")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='b'\n")
    assert sandbox.venv_volume_name(tmp_path, "sha256:1") != base


def test_volume_name_does_not_confuse_where_one_file_ends(tmp_path):
    (tmp_path / "pyproject.toml").write_text("ab")
    (tmp_path / "uv.lock").write_text("c")
    one = sandbox.venv_volume_name(tmp_path, "i")
    (tmp_path / "pyproject.toml").write_text("a")
    (tmp_path / "uv.lock").write_text("bc")
    assert sandbox.venv_volume_name(tmp_path, "i") != one


def test_volume_name_without_a_lockfile_still_works(tmp_path):
    assert sandbox.venv_volume_name(tmp_path, "i").startswith("orch-venv-")


# ---------- probe and cache ----------


class FakeEngine:
    def __init__(self, *results):
        self.results, self.calls = list(results), 0

    def __call__(self, image):
        self.calls += 1
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


@pytest.fixture
def clock(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(sandbox, "_clock", lambda: t[0])
    return t


def test_an_ok_probe_is_cached_for_at_most_30_seconds(monkeypatch, clock):
    eng = FakeEngine(OK)
    monkeypatch.setattr(sandbox, "docker_probe", eng)
    assert sandbox.probe("img").ok and sandbox.probe("img").ok
    assert eng.calls == 1
    clock[0] += 29.9
    sandbox.probe("img")
    assert eng.calls == 1
    clock[0] += 0.2  # 30.1 s after the first probe
    sandbox.probe("img")
    assert eng.calls == 2


def test_an_unavailable_probe_is_never_cached(monkeypatch, clock):
    eng = FakeEngine(DOWN)
    monkeypatch.setattr(sandbox, "docker_probe", eng)
    assert not sandbox.probe("img").ok and not sandbox.probe("img").ok
    assert eng.calls == 2
    eng2 = FakeEngine(NO_IMAGE)
    monkeypatch.setattr(sandbox, "docker_probe", eng2)
    sandbox.probe("img2"), sandbox.probe("img2")
    assert eng2.calls == 2


def test_a_failure_drops_an_older_cached_success(monkeypatch, clock):
    eng = FakeEngine(OK, DOWN, DOWN)
    monkeypatch.setattr(sandbox, "docker_probe", eng)
    assert sandbox.probe("img").ok
    assert not sandbox.probe("img", fresh=True).ok  # engine went away
    assert not sandbox.probe("img").ok  # the stale "ok" is gone, so this asks again
    assert eng.calls == 3


def test_the_cache_is_per_image(monkeypatch, clock):
    eng = FakeEngine(OK)
    monkeypatch.setattr(sandbox, "docker_probe", eng)
    sandbox.probe("a"), sandbox.probe("b")
    assert eng.calls == 2


# ---------- real docker_probe, with a fake process runner ----------


def fake_run(replies, seen):
    def run(argv, **kw):
        seen.append(argv)
        return replies.pop(0)

    return run


def run_probe(monkeypatch, *replies):
    seen = []
    monkeypatch.setattr(sandbox, "run_process", fake_run(list(replies), seen))
    return sandbox.real_docker_probe("orch-sbx:py312"), seen


def test_docker_probe_ok(monkeypatch):
    res, seen = run_probe(monkeypatch, ProcessOutput(stdout="29.9.3\n", returncode=0), ProcessOutput(stdout="sha256:abc\n", returncode=0))
    assert res == ProbeResult(True, True, "sha256:abc", "29.9.3")
    assert seen[0][:2] == ["docker", "info"]
    assert seen[1][:3] == ["docker", "image", "inspect"] and seen[1][-1] == "orch-sbx:py312"


def test_docker_probe_engine_down(monkeypatch):
    res, seen = run_probe(monkeypatch, ProcessOutput(stderr="\nfailed to connect to the docker API at npipe\nmore", returncode=1))
    assert not res.engine_ok and "failed to connect" in res.detail and len(seen) == 1  # no image question to a dead engine


def test_docker_probe_not_installed_and_timeout(monkeypatch):
    res, _ = run_probe(monkeypatch, ProcessOutput(not_found=True, error="executable not found: docker"))
    assert not res.engine_ok and "not found on PATH" in res.detail
    res, _ = run_probe(monkeypatch, ProcessOutput(timed_out=True))
    assert not res.engine_ok and "did not answer" in res.detail


def test_docker_probe_empty_answer_is_not_ok(monkeypatch):
    res, _ = run_probe(monkeypatch, ProcessOutput(stdout="  \n", returncode=0))
    assert not res.engine_ok


def test_docker_probe_missing_image_and_slow_image_question(monkeypatch):
    res, _ = run_probe(monkeypatch, ProcessOutput(stdout="29.9.3", returncode=0), ProcessOutput(stderr="No such image", returncode=1))
    assert res.engine_ok and not res.image_ok and "not found" in res.detail
    res, _ = run_probe(monkeypatch, ProcessOutput(stdout="29.9.3", returncode=0), ProcessOutput(timed_out=True))
    assert res.engine_ok and not res.image_ok and "did not answer" in res.detail


def test_probe_runs_docker_with_a_scrubbed_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-nope")
    monkeypatch.setenv("SOME_SECRET", "x")
    envs = []

    def run(argv, **kw):
        envs.append(kw["env"])
        return ProcessOutput(stdout="1", returncode=0)

    monkeypatch.setattr(sandbox, "run_process", run)
    sandbox.real_docker_probe("img")
    assert envs and all("ANTHROPIC_API_KEY" not in e and "SOME_SECRET" not in e for e in envs)


# ---------- require: fail closed, always fresh ----------


def test_require_is_off_for_an_opted_out_repo_and_never_asks_docker(tmp_path, monkeypatch):
    eng = FakeEngine(DOWN)
    monkeypatch.setattr(sandbox, "docker_probe", eng)
    assert sandbox.require(repo(tmp_path, "off")).state == "off"
    assert eng.calls == 0


def test_require_ok_when_docker_and_image_are_there(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "docker_probe", FakeEngine(OK))
    st = sandbox.require(repo(tmp_path))
    assert st.state == "ok" and st.image_id == "sha256:abc"


def test_probe_says_ok_then_down_the_next_require_is_refused_within_30_seconds(tmp_path, monkeypatch, clock):
    eng = FakeEngine(OK, DOWN)
    monkeypatch.setattr(sandbox, "docker_probe", eng)
    r = repo(tmp_path)
    assert sandbox.require(r).state == "ok"
    clock[0] += 1.0  # well inside the 30 s cache window
    with pytest.raises(SandboxUnavailable, match="Docker is not running"):
        sandbox.require(r)
    assert eng.calls == 2  # require did not use the cached success


def test_require_ignores_a_warm_cache_even_when_display_code_filled_it(tmp_path, monkeypatch, clock):
    eng = FakeEngine(OK, DOWN)
    monkeypatch.setattr(sandbox, "docker_probe", eng)
    assert sandbox.status("docker", "orch-sbx:py312").state == "ok"  # display path, cached
    assert sandbox.status("docker", "orch-sbx:py312").state == "ok" and eng.calls == 1
    with pytest.raises(SandboxUnavailable):
        sandbox.require(repo(tmp_path))


def test_refusal_text_for_a_stopped_engine_names_the_fix_and_the_opt_out(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "docker_probe", FakeEngine(DOWN))
    with pytest.raises(SandboxUnavailable) as e:
        sandbox.require(repo(tmp_path, name="practice"))
    msg = str(e.value)
    assert "Docker is not running" in msg and "Start Docker Desktop" in msg
    assert "`sandbox: off` for repo practice" in msg and "Nothing was run" in msg


def test_refusal_text_for_a_missing_image_points_at_the_build_command(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "docker_probe", FakeEngine(NO_IMAGE))
    with pytest.raises(SandboxUnavailable) as e:
        sandbox.require(repo(tmp_path))
    assert "orch sandbox build" in str(e.value) and "orch-sbx:py312" in str(e.value)


def test_there_is_no_way_to_get_ok_for_a_docker_repo_without_the_engine(tmp_path, monkeypatch):
    """Every failure shape refuses; none of them degrades to "off"."""
    for bad in (DOWN, NO_IMAGE, ProbeResult(False), ProbeResult(True, False)):
        monkeypatch.setattr(sandbox, "docker_probe", FakeEngine(bad))
        with pytest.raises(SandboxUnavailable):
            sandbox.require(repo(tmp_path))
