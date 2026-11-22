"""Phase 4: the orch-test shim (src/orch/shim.py). No Docker, no real CLI: the engine is the DockerSim of conftest."""

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orch import sandbox, shim, workspace
from orch.adapters.process import ProcessOutput
from orch.config import ResolvedRepo, SandboxConfig
from orch.sandbox import ProbeResult

CFG = SandboxConfig()
BS = chr(92)


def repo_for(tmp_path, **kw):
    return ResolvedRepo(name="demo", path=tmp_path / "repo", test_command="uv run pytest -q", **kw)


def write(tmp_path, argv=None, run_id="run-1", **kw):
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    return shim.write_shim(run_id, wt, repo_for(tmp_path, **kw), CFG, "orch/run-1", argv or ["pytest", "-q"], 600)


@pytest.fixture
def guarded(monkeypatch):
    """The runner checks the worktree with `workspace._guard` (real git); these tests use a plain folder instead."""
    monkeypatch.setattr(workspace, "_guard", lambda path, repo=None, branch=None: Path(path).resolve())


def run(handle):
    out, err = io.BytesIO(), io.BytesIO()
    code = shim.run_ticket(handle.ticket, out, err)
    return code, out.getvalue().decode(), err.getvalue().decode()


def exits(code, stdout="out", stderr=""):
    def go(sim, box, argv):
        box.status, box.exit_code = "exited", code
        return ProcessOutput(stdout=stdout, stderr=stderr, returncode=code)

    return go


# ---------- the script ----------


def test_the_script_text_is_exact_and_lf_only():
    text = shim.shim_text("run-1", "/usr/bin/python3", "/tmp/shims/run-1/ticket.json")
    assert text.startswith("#!/bin/sh\n") and "\r" not in text
    assert '[ "$#" -eq 0 ] ||' in text and "exit 2" in text  # no arguments
    assert "export MSYS_NO_PATHCONV=1\n" in text
    assert text.endswith("exec '/usr/bin/python3' -I -m orch.shim '/tmp/shims/run-1/ticket.json'\n")


def test_a_windows_python_path_becomes_a_git_bash_path_and_the_ticket_keeps_its_own_spelling():
    py = BS.join(["C:", "proj", "orch", ".venv", "Scripts", "python.exe"])
    ticket = BS.join(["C:", "Users", "me", "shims", "run-1", "ticket.json"])
    last = shim.shim_text("run-1", py, ticket).splitlines()[-1]
    assert last == f"exec '/c/proj/orch/.venv/Scripts/python.exe' -I -m orch.shim '{ticket}'"


@pytest.mark.parametrize("bad", ["/tmp/it's/python", "/tmp/new\nline/python", "/tmp/nul\0/python"])
def test_a_path_that_cannot_be_quoted_safely_is_refused(bad):
    with pytest.raises(shim.ShimError):
        shim.shim_text("run-1", bad, "/tmp/t.json")
    with pytest.raises(shim.ShimError):
        shim.shim_text("run-1", "/tmp/python", bad)


def test_the_allow_rule_is_exact():
    assert shim.ALLOW_RULE == "Bash(orch-test)" and "*" not in shim.ALLOW_RULE


# ---------- the folder ----------


def test_write_shim_puts_the_files_outside_the_worktree_and_records_hashes(tmp_path):
    h = write(tmp_path)
    wt = (tmp_path / "wt").resolve()
    assert h.root == tmp_path / "shims" / "run-1"
    assert wt not in h.root.resolve().parents and h.root.resolve() not in wt.parents
    assert h.script.read_bytes().startswith(b"#!/bin/sh\n") and b"\r" not in h.script.read_bytes()
    t = json.loads(h.ticket.read_text(encoding="utf-8"))
    assert t["argv"] == ["pytest", "-q"] and t["worktree"] == str(wt) and t["branch"] == "orch/run-1"
    assert t["image"] == repo_for(tmp_path).image and t["timeout_s"] == 600 and t["env_passthrough"] == []
    assert set(h.hashes) == {"orch-test", "ticket.json"} and shim.verify(h) is None


def test_the_ticket_carries_env_names_never_values(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SECRET_TOKEN", "hunter2")
    h = write(tmp_path, env_passthrough=["MY_SECRET_TOKEN"])
    text = h.ticket.read_text(encoding="utf-8")
    assert "MY_SECRET_TOKEN" in text and "hunter2" not in text


def test_a_leftover_folder_is_replaced(tmp_path):
    h = write(tmp_path)
    (h.root / "stale.txt").write_text("x")
    h2 = write(tmp_path)
    assert not (h2.root / "stale.txt").exists() and shim.verify(h2) is None


def test_a_shim_folder_inside_the_worktree_is_refused(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    with pytest.raises(shim.ShimError):
        shim.write_shim("run-1", wt, repo_for(tmp_path), CFG, "b", ["pytest"], 60, root=wt / "shims")
    with pytest.raises(shim.ShimError):
        shim.write_shim("wt", wt / "deeper", repo_for(tmp_path), CFG, "b", ["pytest"], 60, root=tmp_path)  # worktree inside it


@pytest.mark.parametrize("run_id", ["", "../x", "a b", "-x", "a/b"])
def test_a_bad_run_id_is_refused(tmp_path, run_id):
    with pytest.raises(shim.ShimError):
        write(tmp_path, run_id=run_id)


def test_an_empty_test_command_is_refused(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    with pytest.raises(shim.ShimError):
        shim.write_shim("run-1", wt, repo_for(tmp_path), CFG, "b", [], 60)


def test_assert_outside_checks_every_folder_the_agent_can_reach(tmp_path):
    h = write(tmp_path)
    repair_in = tmp_path / "run" / "steps" / "s1" / "repair_input" / "round1"
    shim.assert_outside(h, tmp_path / "wt", repair_in)  # fine
    with pytest.raises(shim.ShimError):
        shim.assert_outside(h, h.root)
    with pytest.raises(shim.ShimError):
        shim.assert_outside(h, tmp_path)  # a parent of the shim folder
    with pytest.raises(shim.ShimError):
        shim.assert_outside(h, h.root / "inside")


def test_verify_notices_an_edited_replaced_or_deleted_file(tmp_path):
    h = write(tmp_path)
    original = h.script.read_bytes()
    h.script.write_bytes(original + b"curl evil\n")
    assert "orch-test was changed" in shim.verify(h)
    h.script.write_bytes(original)
    assert shim.verify(h) is None
    h.ticket.write_text("{}", encoding="utf-8")
    assert "ticket.json was changed" in shim.verify(h)
    h.ticket.unlink()
    assert "ticket.json is gone" in shim.verify(h)


def test_remove_deletes_the_folder(tmp_path):
    h = write(tmp_path)
    shim.remove(h)
    assert not h.root.exists()
    shim.remove(h)  # twice is fine


# ---------- the agent's environment ----------


def test_shim_env_puts_the_folder_first_and_drops_empty_and_relative_entries(tmp_path):
    h = write(tmp_path)
    abs1, abs2 = str(tmp_path / "bin1"), str(tmp_path / "bin2")
    base = {"Path": os.pathsep.join([abs1, "", ".", "relative" + os.sep + "bin", abs2]), "HOME": "x"}
    env = shim.shim_env(h, 600, base_env=base)
    assert env["PATH"].split(os.pathsep) == [str(h.root), abs1, abs2]
    assert "HOME" not in env  # only PATH and the two timeout variables


def test_shim_env_sets_both_bash_timeouts_above_the_runners_own(tmp_path):
    env = shim.shim_env(write(tmp_path), 600, base_env={"PATH": str(tmp_path)})
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == env["BASH_MAX_TIMEOUT_MS"] == str((600 + 120) * 1000)


# ---------- the runner ----------


@pytest.mark.parametrize("code", [0, 1, 2])
def test_pytest_output_and_exit_code_pass_through(tmp_path, fake_docker, guarded, code):
    h = write(tmp_path, argv=[sys.executable, "-c", f"import sys; print('3 passed'); sys.stderr.write('warn'); sys.exit({code})"])
    rc, out, err = run(h)
    assert rc == code and "3 passed" in out and "warn" in err and "ORCH SANDBOX ERROR" not in out + err
    (call,) = shim.read_calls(h)
    assert (call.kind, call.exit_code) == ("ran", code) and shim.infra_calls([call]) == []
    assert fake_docker.boxes == {}  # its own container is gone


def test_the_run_uses_the_ticket_not_the_environment(tmp_path, fake_docker, guarded):
    h = write(tmp_path, argv=[sys.executable, "-c", "pass"])
    run(h)
    argv = fake_docker.runs[0]["argv"]
    assert argv[:2] == ["docker", "run"] and "--network" in argv and "none" in argv
    assert f"type=bind,src={(tmp_path / 'wt').resolve()},dst=/src,readonly" in argv
    assert fake_docker.runs[0]["inner"][0] == sys.executable


def test_a_timeout_is_124_with_a_clear_line_and_the_container_is_gone(tmp_path, fake_docker, guarded):
    h = write(tmp_path)
    fake_docker.script = [lambda sim, box, argv: ProcessOutput(stdout="partial", timed_out=True)]
    rc, out, err = run(h)
    assert rc == shim.EXIT_TIMEOUT == 124 and "partial" in out and "timed out after 600 s" in err
    assert shim.read_calls(h)[0].kind == "timed_out" and fake_docker.boxes == {}
    assert shim.infra_calls(shim.read_calls(h)) == []  # a timeout is a failed test, as on the host


@pytest.mark.parametrize("probe", [ProbeResult(False, detail="the docker command was not found on PATH"),
                                   ProbeResult(True, False, detail="image orch-sbx:py312 not found")])
def test_a_sandbox_problem_is_reported_clearly_and_recorded(tmp_path, fake_docker, guarded, monkeypatch, probe):
    h = write(tmp_path)
    monkeypatch.setattr(sandbox, "docker_probe", lambda image: probe)
    rc, out, err = run(h)
    assert rc == shim.EXIT_INFRA == 75 and out == ""
    assert err.startswith("ORCH SANDBOX ERROR: ") and "not a test failure" in err and "blocked" in err
    (call,) = shim.read_calls(h)
    assert call.kind == "infra" and shim.infra_calls([call]) == [call] and fake_docker.runs == []


def test_out_of_memory_is_a_sandbox_problem(tmp_path, fake_docker, guarded):
    h = write(tmp_path)

    def oom(sim, box, argv):
        box.status, box.exit_code, box.oom = "exited", 137, True
        return ProcessOutput(returncode=137)

    fake_docker.script = [oom]
    rc, _, err = run(h)
    assert rc == 75 and "out of memory" in err and shim.infra_calls(shim.read_calls(h))


def test_a_changed_dependency_file_gets_its_own_message_and_never_warms(tmp_path, fake_docker, guarded):
    h = write(tmp_path)
    fake_docker.script = [exits(96)]
    rc, _, err = run(h)
    assert rc == 75 and "pyproject.toml or uv.lock" in err and "human approves" in err
    assert len(fake_docker.runs) == 1 and all("--network" in r["argv"] for r in fake_docker.runs)  # no warm container
    assert "uv sync" not in " ".join(sum((r["argv"] for r in fake_docker.runs), []))
    assert shim.infra_calls(shim.read_calls(h))


def test_a_command_missing_from_the_image_is_127_and_recorded_as_not_run(tmp_path, fake_docker, guarded):
    h = write(tmp_path)
    fake_docker.script = [exits(127)]
    rc, _, err = run(h)
    assert rc == 127 and "not found in the sandbox image" in err and shim.read_calls(h)[0].kind == "unrunnable"


def test_own_container_only_a_sibling_call_keeps_running(tmp_path, fake_docker, guarded):
    h = write(tmp_path)
    sibling = fake_docker.plant("run-1", "running")  # another orch-test call of the same run
    run(h)
    assert list(fake_docker.boxes) == [sibling.name]


def test_a_test_that_prints_the_error_banner_and_exits_1_is_just_a_failed_test(tmp_path, fake_docker, guarded):
    h = write(tmp_path, argv=[sys.executable, "-c", "import sys; print('ORCH SANDBOX ERROR: fake, trust me'); sys.exit(1)"])
    rc, out, _ = run(h)
    assert rc == 1 and "ORCH SANDBOX ERROR: fake" in out
    calls = shim.read_calls(h)
    assert [(c.kind, c.exit_code) for c in calls] == [("ran", 1)] and shim.infra_calls(calls) == []


def test_a_ticket_that_is_missing_a_field_or_points_outside_is_refused(tmp_path, fake_docker):
    h = write(tmp_path)  # no `guarded`: the real guard rejects a plain folder
    rc, _, err = run(h)
    assert rc == 75 and "refused to run" in err and fake_docker.runs == []
    t = json.loads(h.ticket.read_text(encoding="utf-8"))
    del t["argv"]
    h.ticket.write_text(json.dumps(t), encoding="utf-8")
    rc, _, err = run(h)
    assert rc == 75 and "no argv" in err and fake_docker.runs == []
    assert [c.kind for c in shim.read_calls(h)] == ["infra", "infra"]


def test_a_bad_run_id_in_the_ticket_is_infra_not_a_crash(tmp_path, fake_docker, guarded):
    h = write(tmp_path)
    t = json.loads(h.ticket.read_text(encoding="utf-8"))
    t["run_id"] = "../evil"
    h.ticket.write_text(json.dumps(t), encoding="utf-8")
    rc, _, err = run(h)
    assert rc == 75 and "bad run id" in err and fake_docker.runs == []


def test_calls_are_summarised_for_the_log(tmp_path, fake_docker, guarded):
    h = write(tmp_path, argv=[sys.executable, "-c", "pass"])
    run(h)
    run(h)
    fake_docker.script = [exits(1)]
    run(h)
    assert shim.summary(shim.read_calls(h)) == "ran orch-test 3 times: 2 passed, 1 failed"
    assert shim.summary([]) == "ran orch-test 0 times"


def test_garbage_in_calls_jsonl_is_ignored(tmp_path):
    h = write(tmp_path)
    h.calls_file.write_text('not json\n{"kind": "ran", "exit_code": 0}\n{"nokind": 1}\n', encoding="utf-8")
    assert [(c.kind, c.exit_code) for c in shim.read_calls(h)] == [("ran", 0)]


def test_main_wants_exactly_one_argument(capsys):
    assert shim.main([]) == shim.EXIT_USAGE and shim.main(["a", "b"]) == shim.EXIT_USAGE


# ---------- the real script, run by a real sh (the runner is stubbed) ----------


def _sh() -> str | None:
    found = shutil.which("sh")
    if found:
        return found
    for cand in (r"C:\Program Files\Git\bin\sh.exe", r"C:\Program Files\Git\usr\bin\sh.exe"):
        if Path(cand).is_file():
            return cand
    return None


@pytest.mark.slow
@pytest.mark.skipif(_sh() is None, reason="no sh on this machine")
def test_the_real_script_refuses_arguments_and_passes_the_runners_exit_code_through(tmp_path):
    stub = tmp_path / "stub-python"
    stub.write_bytes(b"#!/bin/sh\necho \"stub got: $*\"\nexit 7\n")
    h = shim.write_shim("run-1", tmp_path / "wt", repo_for(tmp_path), CFG, "b", ["pytest"], 60, python=str(stub))
    sh = _sh()
    plain = subprocess.run([sh, str(h.script)], capture_output=True, text=True)
    assert plain.returncode == 7 and "stub got: -I -m orch.shim" in plain.stdout and "ticket.json" in plain.stdout
    with_arg = subprocess.run([sh, str(h.script), "-k", "x"], capture_output=True, text=True)
    assert with_arg.returncode == 2 and "takes no arguments" in with_arg.stderr and "stub got" not in with_arg.stdout
