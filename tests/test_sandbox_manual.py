"""`orch sandbox test`: the by-hand run through the same path as a real run, and `--probe`. Fake engine only."""

import ast

import pytest
import yaml
from git_helpers import git, make_repo
from test_sandbox_gate import DOWN, Engine
from test_sandbox_run import exits
from typer.testing import CliRunner

from orch import cli, sandbox, workspace
from orch.adapters.process import ProcessOutput
from orch.config import default_config_path

invoke = lambda *a: CliRunner().invoke(cli.app, list(a))  # noqa: E731
PY = "python"
PASS = f'{PY} -c "import sys; sys.exit(0)"'
FAIL = f'{PY} -c "import sys; print(\'1 failed\'); sys.exit(1)"'
SLEEP = f'{PY} -c "import time; time.sleep(60)"'


def cfg(tmp_path, monkeypatch, path=None, test_command=PASS, **repo_over):
    data = yaml.safe_load(open(default_config_path(), encoding="utf-8"))
    data["repos"] = {"demo": {"path": str(path or tmp_path), "test_command": test_command, **repo_over}}
    f = tmp_path / "config.yaml"
    f.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("ORCH_CONFIG", str(f))


# ---------- a plain run ----------


def test_pass(tmp_path, monkeypatch, fake_docker):
    cfg(tmp_path, monkeypatch)
    r = invoke("sandbox", "test", "--repo", "demo")
    assert r.exit_code == 0 and "PASS" in r.output and "repo demo: tests in the sandbox" in r.output
    (run,) = fake_docker.runs
    assert f"type=bind,src={tmp_path.resolve()},dst=/src,readonly" in run["argv"] and run["inner"] == [PY, "-c", "import sys; sys.exit(0)"]
    label = run["argv"][run["argv"].index("--label") + 1]
    assert label.startswith("orch.run=manual-") and fake_docker.boxes == {}


def test_failed_tests_show_their_output_and_exit_1(tmp_path, monkeypatch, fake_docker):
    cfg(tmp_path, monkeypatch, test_command=FAIL)
    r = invoke("sandbox", "test", "--repo", "demo")
    assert r.exit_code == 1 and "1 failed" in r.output and "TESTS FAILED (exit 1)" in r.output


def test_a_container_problem_is_a_sandbox_error_exit_2_not_a_failed_test(tmp_path, monkeypatch, fake_docker):
    cfg(tmp_path, monkeypatch)
    fake_docker.script = [exits(137, oom=True)]
    r = invoke("sandbox", "test", "--repo", "demo")
    assert r.exit_code == 2 and "SANDBOX ERROR: out of memory" in r.output and "TESTS FAILED" not in r.output


def test_docker_down_refuses_and_runs_nothing(tmp_path, monkeypatch, fake_docker):
    cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(sandbox, "docker_probe", Engine(DOWN))
    r = invoke("sandbox", "test", "--repo", "demo")
    assert r.exit_code == 2 and "SANDBOX ERROR: Docker is not running" in r.output and fake_docker.runs == []


def test_timeout_is_a_failed_run_and_the_container_is_gone(tmp_path, monkeypatch, fake_docker):
    cfg(tmp_path, monkeypatch, test_command=SLEEP)
    r = invoke("sandbox", "test", "--repo", "demo", "--timeout", "1")
    assert r.exit_code == 1 and "TESTS FAILED (timed out)" in r.output
    assert fake_docker.boxes == {}


def test_ctrl_c_removes_the_container_and_exits_130(tmp_path, monkeypatch, fake_docker):
    cfg(tmp_path, monkeypatch)

    def interrupted(sim, box, argv):
        raise KeyboardInterrupt

    fake_docker.script = [interrupted]
    r = invoke("sandbox", "test", "--repo", "demo")
    assert r.exit_code == 130 and "Stopped by you" in r.output and fake_docker.boxes == {}


def test_retest_runs_retest_command_and_needs_one(tmp_path, monkeypatch, fake_docker):
    cfg(tmp_path, monkeypatch, retest_command=f'{PY} -c "print(7)"')
    assert invoke("sandbox", "test", "--repo", "demo", "--retest").exit_code == 0
    assert fake_docker.runs[0]["inner"] == [PY, "-c", "print(7)"]
    cfg(tmp_path, monkeypatch)
    r = invoke("sandbox", "test", "--repo", "demo", "--retest")
    assert r.exit_code == 2 and "no retest_command" in r.output


def test_src_mounts_another_folder(tmp_path, monkeypatch, fake_docker):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    cfg(tmp_path, monkeypatch)
    assert invoke("sandbox", "test", "--repo", "demo", "--src", str(scratch)).exit_code == 0
    assert f"type=bind,src={scratch.resolve()},dst=/src,readonly" in fake_docker.runs[0]["argv"]
    r = invoke("sandbox", "test", "--repo", "demo", "--src", str(tmp_path / "missing"))
    assert r.exit_code == 2 and "is not a folder" in r.output


def test_an_off_repo_has_nothing_to_test_and_never_calls_docker(tmp_path, monkeypatch, fake_docker):
    cfg(tmp_path, monkeypatch, sandbox="off")
    r = invoke("sandbox", "test", "--repo", "demo")
    assert r.exit_code == 2 and "sandbox: off" in r.output and fake_docker.calls == []


def test_the_not_ready_environment_is_warmed_from_the_folder_itself(tmp_path, monkeypatch, fake_docker):
    cfg(tmp_path, monkeypatch)
    fake_docker.script = [exits(96), exits(0)]
    r = invoke("sandbox", "test", "--repo", "demo")
    assert r.exit_code == 0 and "preparing the test environment" in r.output
    assert f"type=bind,src={tmp_path.resolve()},dst=/src,readonly" in fake_docker.runs[1]["argv"]  # the warm run


# ---------- --probe ----------

PROBE_SCRIPT = """\
import pathlib, sys
if not pathlib.Path("tests/test_escape_probe.py").is_file():
    sys.exit("the probe test was not planted")
for kind in sys.argv[1:]:
    print("BLOCKED: " + kind + ": it did not work")
"""


@pytest.fixture
def probe_repo(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "proj")
    (repo / "check_probe.py").write_text(PROBE_SCRIPT, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "probe script")

    def configure(*kinds):
        cfg(tmp_path, monkeypatch, path=repo, test_command=f"{PY} check_probe.py " + " ".join(f"'{k}'" for k in kinds))

    return repo, configure


def test_probe_ok_plants_the_tests_sets_the_canary_outside_the_argv_and_cleans_up(probe_repo, fake_docker):
    repo, configure = probe_repo
    configure("write", "network", "host env", "host file")
    r = invoke("sandbox", "test", "--repo", "demo", "--probe")
    assert r.exit_code == 0, r.output
    assert "PROBE OK" in r.output and "blocked (network)" in r.output
    run = fake_docker.runs[0]
    scratch = run["src"]
    assert scratch != repo.resolve() and workspace.warm_root() in scratch.parents
    assert not scratch.exists() and list(workspace.warm_root().iterdir()) == []  # deleted afterwards
    assert run["env"]["ORCH_PROBE_CANARY"].startswith("canary-")
    assert not any("ORCH_PROBE_CANARY" in a or "canary-" in a for a in run["argv"])  # host env never crosses
    assert not (repo / "tests" / "test_escape_probe.py").exists()  # the real repo is untouched
    assert git(repo, "status", "--porcelain").strip() == ""


def test_probe_fails_when_an_attempt_is_not_reported_blocked(probe_repo, fake_docker):
    _, configure = probe_repo
    configure("write", "network", "host env")
    r = invoke("sandbox", "test", "--repo", "demo", "--probe")
    assert r.exit_code == 1 and "PROBE FAILED" in r.output and "host file" in r.output


PYTEST_FAILURE = """\
    def test_host_environment_and_drives_are_not_visible():
        leaked = []
>       assert not drives, f"ESCAPED: host drives visible: {drives}"
E       AssertionError: ESCAPED: host drives visible (mounted, linked or not empty): ['/mnt/c']
E       assert not ['/mnt/c']

tests/test_escape_probe.py:54: AssertionError
"""


def test_probe_failed_shows_the_real_assertion_not_the_source_line(probe_repo, fake_docker):
    _, configure = probe_repo
    configure("write", "network", "host env", "host file")

    def escaped(sim, box, argv):
        exits(1)(sim, box, argv)
        return ProcessOutput(stdout=PYTEST_FAILURE, returncode=1)

    fake_docker.script = [escaped]
    r = invoke("sandbox", "test", "--repo", "demo", "--probe")
    assert r.exit_code == 1
    assert "PROBE FAILED: ESCAPED: host drives visible (mounted, linked or not empty): ['/mnt/c']" in r.output
    assert 'f"ESCAPED' not in r.output.split("PROBE FAILED")[1]


def test_probe_fails_when_an_escape_gets_through(probe_repo, fake_docker):
    _, configure = probe_repo
    configure("write", "network", "host env", "host file")
    fake_docker.script = [
        lambda sim, box, argv: exits(1)(sim, box, argv).__class__(
            stdout="ESCAPED: connected to 1.1.1.1:53\nBLOCKED: write: x\n", returncode=1)
    ]  # fmt: skip
    r = invoke("sandbox", "test", "--repo", "demo", "--probe")
    assert r.exit_code == 1 and "PROBE FAILED: ESCAPED: connected to 1.1.1.1:53" in r.output


def test_probe_needs_a_git_repository(tmp_path, monkeypatch, fake_docker):
    cfg(tmp_path, monkeypatch)
    r = invoke("sandbox", "test", "--repo", "demo", "--probe")
    assert r.exit_code == 2 and "--probe needs a git repository" in r.output and fake_docker.runs == []


def test_probe_appends_the_report_flag_only_for_pytest(probe_repo, tmp_path, monkeypatch, fake_docker):
    repo, _ = probe_repo
    cfg(tmp_path, monkeypatch, path=repo, test_command="uv run pytest -q")
    fake_docker.script = [exits(0)]
    invoke("sandbox", "test", "--repo", "demo", "--probe")
    assert fake_docker.runs[0]["inner"] == ["uv", "run", "pytest", "-q", "-rP"]


# ---------- the planted file itself ----------


def test_the_probe_file_refuses_to_run_outside_a_container_and_has_the_four_attempts():
    tree = ast.parse(sandbox.dockerfile_dir().joinpath("probe", "probe_escape.py").read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert sorted(n for n in funcs if n.startswith("test_")) == [
        "test_a_host_file_cannot_be_read", "test_cannot_open_a_socket", "test_cannot_write_outside_the_working_copy",
        "test_host_environment_and_drives_are_not_visible",
    ]  # fmt: skip
    guard = funcs["only_inside_a_container"]
    text = ast.unparse(guard)
    assert "autouse=True" in text and "/.dockerenv" in text and "pytest.fail" in text
    src = ast.unparse(tree)
    assert "1.1.1.1" in src and "ORCH_PROBE_CANARY" in src and "USERPROFILE" in src and "/src/orch-probe.txt" in src
    assert src.count("BLOCKED: ") == 4
    assert "probe_target.json" in src
    assert "os.path.ismount" in src and "os.listdir" in src and "'/mnt/c'" in src and "'/mnt/host'" in src


def test_the_probe_folder_is_not_collected_by_the_normal_suite():
    root = sandbox.dockerfile_dir().parent
    assert not list((root / "sandbox").rglob("test_*.py")) and not list((root / "sandbox").rglob("*_test.py"))
    assert (root / "sandbox" / "probe" / "probe_escape.py").is_file()



def test_host_variants_lists_every_spelling_a_container_could_use_for_a_windows_file():
    from pathlib import PureWindowsPath

    from orch.sandbox_manual import host_variants

    bs = chr(92)
    raw = bs.join(["C:", "Users", "me", "AppData", "Local", "orch", "warm", "probe-1.canary"])
    got = host_variants(PureWindowsPath(raw))
    assert raw in got
    for base in ("/mnt", "/host_mnt", "/run/desktop/mnt/host", "/mnt/host", ""):
        assert f"{base}/c/Users/me/AppData/Local/orch/warm/probe-1.canary" in got
    assert len(got) == len(set(got))
    assert host_variants(PureWindowsPath("/tmp/x")) == [bs + "tmp" + bs + "x", "/tmp/x"]  # not a drive path


def test_probe_plants_a_real_host_canary_outside_the_mount_and_removes_it(probe_repo, fake_docker):
    import json as _json

    _, configure = probe_repo
    configure("write", "network", "host env", "host file")
    seen = {}

    def look(sim, box, argv):
        src = sim.runs[-1]["src"]
        seen["paths"] = _json.loads((src / "probe_target.json").read_text(encoding="utf-8"))["paths"]
        files = [p for p in workspace.warm_root().iterdir() if p.suffix == ".canary"]
        seen["canary"] = [str(p) for p in files]
        seen["text"] = [p.read_text(encoding="utf-8") for p in files]
        exits(0)(sim, box, argv)
        lines = "".join(f"BLOCKED: {k}: x" + chr(10) for k in ("write", "network", "host env", "host file"))
        return ProcessOutput(stdout=lines, returncode=0)

    fake_docker.script = [look]
    r = invoke("sandbox", "test", "--repo", "demo", "--probe")
    assert r.exit_code == 0, r.output
    assert len(seen["canary"]) == 1 and seen["text"][0].startswith("canary-")
    assert seen["canary"][0] in seen["paths"]
    assert not list(workspace.warm_root().iterdir())  # canary and scratch are gone
