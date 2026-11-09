from importlib.metadata import version

from orch.adapters.process import ProcessOutput
from orch.versions import cli_version, orch_version


def make_runner(out: ProcessOutput):
    calls = []

    def runner(argv, *, cwd, env=None, stdin_text=None, timeout_s=600):
        calls.append({"argv": argv, "env": env, "timeout_s": timeout_s})
        return out

    runner.calls = calls
    return runner


def test_success_first_stdout_line():
    r = make_runner(ProcessOutput(stdout="\n  2.1.0 (Claude Code)\nextra\n", returncode=0))
    assert cli_version("claude", r) == "2.1.0 (Claude Code)"
    assert r.calls[0]["argv"] == ["claude", "--version"]
    assert r.calls[0]["timeout_s"] == 15


def test_stderr_fallback():
    r = make_runner(ProcessOutput(stderr="agy 1.2\n", returncode=0))
    assert cli_version("agy", r) == "agy 1.2"


def test_not_found():
    r = make_runner(ProcessOutput(not_found=True, error="executable not found"))
    assert cli_version("agy", r) == "not found"


def test_timeout():
    r = make_runner(ProcessOutput(timed_out=True))
    assert cli_version("agy", r) == "error (timeout)"


def test_nonzero_exit():
    r = make_runner(ProcessOutput(stderr="boom\nmore", returncode=2))
    assert cli_version("agy", r) == "error (boom)"


def test_spawn_error():
    r = make_runner(ProcessOutput(error="failed to spawn agy: denied"))
    assert cli_version("agy", r) == "error (failed to spawn agy: denied)"


def test_claude_env_scrubbed(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.setenv(k, "x")
    r = make_runner(ProcessOutput(stdout="2.0\n", returncode=0))
    cli_version("claude", r)
    env = r.calls[0]["env"]
    assert env is not None
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        assert k not in env


def test_agy_env_none():
    r = make_runner(ProcessOutput(stdout="1.0\n", returncode=0))
    cli_version("agy", r)
    assert r.calls[0]["env"] is None


def test_orch_version():
    v = orch_version()
    assert v
    assert v == version("orch")
