"""`orch doctor` Docker checks. The engine is always a fake; nothing here starts Docker."""

from test_phase6a import DARK_T, _cfg_file, by_name, ctx_for, levels

from orch import doctor, sandbox
from orch.sandbox import ProbeResult
from orch.ui import render_doctor

OK = ProbeResult(True, True, "sha256:0123456789abcdef", "29.9.3")
DOWN = ProbeResult(False, detail="failed to connect to the docker API")
NO_IMAGE = ProbeResult(True, False, "", "29.9.3", "image orch-sbx:py312 not found")

DOCKER_REPO = {"path": "repo", "test_command": "pytest -q"}
OFF_REPO = {"path": "repo", "test_command": "pytest -q", "sandbox": "off"}


class Engine:
    def __init__(self, reply=OK, per_image=None):
        self.reply, self.per_image, self.asked = reply, per_image or {}, []

    def __call__(self, image):
        self.asked.append(image)
        return self.per_image.get(image, self.reply)


def run(tmp_path, repos, engine=None, **over):
    eng = engine or Engine()
    cfg = _cfg_file(tmp_path, repos=repos, **over)
    return doctor.run_checks(ctx_for(tmp_path, config_path=cfg, docker=eng)), eng


def test_docker_ok_shows_the_engine_and_the_image(tmp_path):
    checks, eng = run(tmp_path, {"demo": DOCKER_REPO})
    c = by_name(checks)
    assert c["docker engine"].level == doctor.OK and "29.9.3" in c["docker engine"].detail
    assert c["docker image orch-sbx:py312"].level == doctor.OK and c["docker image orch-sbx:py312"].detail == "0123456789ab"
    assert levels(checks, doctor.ERR) == [] and not [n for n in c if n.startswith("sandbox repo")]
    assert eng.asked == ["orch-sbx:py312"]


def test_engine_down_is_an_error_when_a_repo_needs_docker(tmp_path):
    checks, _ = run(tmp_path, {"demo": DOCKER_REPO}, Engine(DOWN))
    c = by_name(checks)["docker engine"]
    assert c.level == doctor.ERR and "not running" in c.detail and "failed to connect" in c.detail and "repo demo" in c.detail
    assert "start Docker Desktop" in c.fix
    assert doctor.exit_code(checks) == 1
    assert not [n for n in by_name(checks) if n.startswith("docker image")]  # no image question to a dead engine


def test_engine_down_is_only_a_warning_when_every_repo_opted_out(tmp_path):
    checks, _ = run(tmp_path, {"demo": OFF_REPO}, Engine(DOWN))
    assert by_name(checks)["docker engine"].level == doctor.WARN
    assert doctor.ERR not in {c.level for c in checks if c.name.startswith("docker")}


def test_missing_image_is_an_error_with_the_build_command(tmp_path):
    checks, _ = run(tmp_path, {"demo": DOCKER_REPO}, Engine(NO_IMAGE))
    c = by_name(checks)
    assert c["docker engine"].level == doctor.OK
    img = c["docker image orch-sbx:py312"]
    assert img.level == doctor.ERR and "orch sandbox build" in img.fix and "used by repo demo" in img.detail
    assert doctor.exit_code(checks) == 1


def test_an_off_repo_is_a_warning_row_in_the_warning_colour(tmp_path):
    checks, _ = run(tmp_path, {"demo": OFF_REPO, "other": DOCKER_REPO})
    row = by_name(checks)["sandbox repo demo"]
    assert row.level == doctor.WARN and row.detail == sandbox.OFF_WARNING
    assert "remove `sandbox: off` from repos.demo" in row.fix
    assert "sandbox repo other" not in by_name(checks)
    assert doctor.exit_code(checks) == 0  # a warning, not a failure
    lines = render_doctor([row], DARK_T)
    spans = {str(sp.style) for t in lines for sp in t.spans}
    assert DARK_T.style("warning") in spans
    assert any(sandbox.OFF_WARNING in t.plain for t in lines)


def test_each_distinct_image_is_checked_once(tmp_path):
    repos = {"a": DOCKER_REPO, "b": {**DOCKER_REPO, "image": "orch-sbx:php83"}, "c": {**DOCKER_REPO, "image": "orch-sbx:php83"}}
    eng = Engine(OK, per_image={"orch-sbx:php83": NO_IMAGE})
    checks, _ = run(tmp_path, repos, eng)
    c = by_name(checks)
    assert sorted(eng.asked) == ["orch-sbx:php83", "orch-sbx:py312"]
    assert c["docker image orch-sbx:py312"].level == doctor.OK
    assert c["docker image orch-sbx:php83"].level == doctor.ERR and c["docker image orch-sbx:php83"].detail == "not found; used by repo b, c"


def test_top_level_image_setting_is_the_one_checked(tmp_path):
    checks, eng = run(tmp_path, {"demo": DOCKER_REPO}, sandbox={"image": "orch-sbx:custom"})
    assert eng.asked == ["orch-sbx:custom"] and "docker image orch-sbx:custom" in by_name(checks)


def test_all_off_with_a_running_engine_asks_about_the_default_image_only_for_the_engine(tmp_path):
    checks, _ = run(tmp_path, {"demo": OFF_REPO})
    assert by_name(checks)["docker engine"].level == doctor.OK
    assert [c.name for c in checks if c.name.startswith("sandbox repo")] == ["sandbox repo demo"]


def test_a_broken_config_adds_no_docker_rows(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("roles: 3\n", encoding="utf-8")
    eng = Engine()
    checks = doctor.run_checks(ctx_for(tmp_path, config_path=bad, docker=eng))
    assert not [c for c in checks if c.name.startswith(("docker", "sandbox"))] and eng.asked == []


# ---------- containers left over from a run that is not running ----------

from orch import runlock  # noqa: E402
from orch.runs import create_run, write_meta  # noqa: E402


def with_runs(tmp_path, statuses):
    """Run folders in repo `demo` (path `repo`), by status. Returns {status: run id}."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    ids = {}
    for status in statuses:
        d = create_run(repo, f"a {status} run")
        write_meta(d, {"run_id": d.name, "status": status})
        ids[status] = d.name
    return ids


def run_with_boxes(tmp_path, boxes, **over):
    """doctor with a fake engine and `boxes` = [(id, run label, docker status)]."""
    found = [sandbox.Container(*b) for b in boxes]
    cfg = _cfg_file(tmp_path, repos={"demo": DOCKER_REPO})
    return doctor.run_checks(ctx_for(tmp_path, config_path=cfg, docker=Engine(), containers=lambda: found, **over))


def orphan_rows(checks):
    return {n: c for n, c in by_name(checks).items() if n.startswith("sandbox containers")}


def test_no_containers_no_rows(tmp_path):
    assert orphan_rows(run_with_boxes(tmp_path, [])) == {}


def test_a_container_of_a_finished_or_failed_run_is_a_warning_with_the_exact_fix(tmp_path):
    ids = with_runs(tmp_path, ["done", "failed", "paused"])
    boxes = [("aaaaaaaaaaaa", ids["done"], "Exited (0) 2 hours ago"), ("bbbbbbbbbbbb", ids["failed"], "Up 3 minutes"),
             ("cccccccccccc", ids["failed"], "Exited (1) 1 hour ago"), ("dddddddddddd", ids["paused"], "Up 9 minutes")]  # fmt: skip
    rows = orphan_rows(run_with_boxes(tmp_path, boxes))
    assert len(rows) == 3
    done = rows[f"sandbox containers {ids['done']}"]
    assert done.level == doctor.WARN and "the run is done" in done.detail and done.fix == "if nothing is running: docker rm -f aaaaaaaaaaaa"
    failed = rows[f"sandbox containers {ids['failed']}"]
    assert "2 left over" in failed.detail and failed.fix.endswith("docker rm -f bbbbbbbbbbbb cccccccccccc")
    assert "the run is paused" in rows[f"sandbox containers {ids['paused']}"].detail


def test_an_unknown_run_is_a_warning(tmp_path):
    rows = orphan_rows(run_with_boxes(tmp_path, [("eeeeeeeeeeee", "20200101T000000Z-gone", "Up 1 hour")]))
    (row,) = rows.values()
    assert row.level == doctor.WARN and "no run with this id" in row.detail


def test_an_executing_run_with_no_process_is_stale_and_warns(tmp_path):
    ids = with_runs(tmp_path, ["executing"])
    (row,) = orphan_rows(run_with_boxes(tmp_path, [("ffffffffffff", ids["executing"], "Up 1 hour")])).values()
    assert row.level == doctor.WARN and "died mid-step" in row.detail


def test_an_executing_run_with_a_live_lock_is_not_a_warning(tmp_path):
    ids = with_runs(tmp_path, ["executing"])
    lock = runlock.acquire(tmp_path / "repo" / ".orch" / "runs" / ids["executing"])
    try:
        assert orphan_rows(run_with_boxes(tmp_path, [("ffffffffffff", ids["executing"], "Up 1 hour")])) == {}
    finally:
        lock.release()


def test_a_manual_test_container_is_reported_with_the_note(tmp_path):
    (row,) = orphan_rows(run_with_boxes(tmp_path, [("999999999999", "manual-ab12cd34", "Up 5 seconds")])).values()
    assert row.level == doctor.WARN and "orch sandbox test" in row.detail and "may still be running" in row.detail
    assert row.fix == "if nothing is running: docker rm -f 999999999999"


def test_no_container_row_when_the_engine_is_down_or_cannot_list(tmp_path):
    cfg = _cfg_file(tmp_path, repos={"demo": DOCKER_REPO})

    def boom():
        raise sandbox.SandboxError("cannot list")

    assert orphan_rows(doctor.run_checks(ctx_for(tmp_path, config_path=cfg, docker=Engine(DOWN), containers=lambda: [
        sandbox.Container("aaaaaaaaaaaa", "x", "Up")]))) == {}  # fmt: skip
    assert orphan_rows(doctor.run_checks(ctx_for(tmp_path, config_path=cfg, docker=Engine(), containers=boom))) == {}


def test_doctor_only_lists_it_never_removes(tmp_path, fake_docker):
    fake_docker.plant("manual-ab12cd34")
    cfg = _cfg_file(tmp_path, repos={"demo": DOCKER_REPO})
    checks = doctor.run_checks(ctx_for(tmp_path, config_path=cfg, docker=Engine()))  # the real list_containers on the fake engine
    assert len(orphan_rows(checks)) == 1
    assert "rm" not in fake_docker.verbs() and len(fake_docker.boxes) == 1
