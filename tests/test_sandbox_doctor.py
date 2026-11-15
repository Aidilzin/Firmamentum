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
