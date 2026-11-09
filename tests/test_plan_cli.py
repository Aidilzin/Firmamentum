"""Run storage and the plan/runs/show commands, via CliRunner and FakeAdapter."""

import json
from datetime import datetime, timezone

import pytest
import yaml
from plan_factory import good_plan, result
from typer.testing import CliRunner

from orch import cli
from orch.adapters.fake import FakeAdapter
from orch.config import default_config_path
from orch.runs import create_run, find_run, list_runs, slugify, RunNotFound

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = yaml.safe_load(open(default_config_path().parent / "config.yaml", encoding="utf-8"))
    proj = tmp_path / "proj"
    proj.mkdir()
    data["repos"] = {"demo": {"path": "proj", "test_command": "pytest -q"}}
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("ORCH_CONFIG", str(cfgfile))
    fakes = {}

    def install(results_by_agent):
        def get(name):
            return fakes.setdefault(
                name.value, FakeAdapter(list(results_by_agent[name.value]), agent_name=name.value)
            )

        monkeypatch.setattr(cli, "get_adapter", get)

    return proj, fakes, install


def test_plan_prints_and_stores_run(env):
    proj, fakes, install = env
    install({"claude": [result(good_plan(), tokens=1234)]})
    r = runner.invoke(cli.app, ["plan", "Add a version command", "--repo", "demo"])
    assert r.exit_code == 0, r.output
    assert "1. [s1]" in r.output and "2. [s2]" in r.output
    assert "depends on: s1 (#1)" in r.output and "risk flags: new_dependency" in r.output
    assert "1234 tokens" in r.output
    (run_dir,) = (proj / ".orch" / "runs").iterdir()
    assert run_dir.name.endswith("-add-a-version-command")
    assert sorted(p.name for p in run_dir.iterdir()) == ["meta.json", "plan.json", "planner_raw.json", "request.md"]
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "planned" and meta["total_tokens"] == 1234
    assert meta["models"] == {"planner": "claude:opus"}
    assert (run_dir / "request.md").read_text(encoding="utf-8").strip() == "Add a version command"
    assert json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))["goal"] == "Add a version command"
    raw = json.loads((run_dir / "planner_raw.json").read_text(encoding="utf-8"))
    assert len(raw["attempts"]) == 1


def test_plan_model_override_and_scout_files(env):
    proj, fakes, install = env
    install({"claude": [result(good_plan())], "agy": [result(text="summary", agent="agy", tokens=10)]})
    r = runner.invoke(cli.app, ["plan", "x y z", "--repo", "demo", "--scout", "--model", "planner=sonnet", "--model", "scout=flash"])
    assert r.exit_code == 0, r.output
    assert fakes["claude"].requests[0].model == "sonnet"
    assert fakes["agy"].requests[0].model == "flash"
    (run_dir,) = (proj / ".orch" / "runs").iterdir()
    assert (run_dir / "scout.md").read_text(encoding="utf-8").strip() == "summary"
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["models"] == {"scout": "agy:flash", "planner": "claude:sonnet"}


def test_plan_needs_input_exits_2(env):
    proj, _, install = env
    install({"claude": [result(good_plan(needs_input=True, question="Which database should it use?", steps=[]))]})
    r = runner.invoke(cli.app, ["plan", "add storage", "--repo", "demo"])
    assert r.exit_code == 2
    assert "Which database should it use?" in r.output
    (run_dir,) = (proj / ".orch" / "runs").iterdir()
    assert json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))["status"] == "needs_input"


def test_plan_failure_exits_1_and_keeps_raw(env):
    proj, _, install = env
    install({"claude": [result(good_plan(steps=[]))]})
    r = runner.invoke(cli.app, ["plan", "bad one", "--repo", "demo"])
    assert r.exit_code == 1
    assert "invalid plan twice" in r.output
    (run_dir,) = (proj / ".orch" / "runs").iterdir()
    assert not (run_dir / "plan.json").exists()
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "failed" and "twice" in meta["error"]
    assert len(json.loads((run_dir / "planner_raw.json").read_text(encoding="utf-8"))["attempts"]) == 2


def test_plan_bad_model_override_and_repo(env):
    _, _, install = env
    install({"claude": [result(good_plan())]})
    assert runner.invoke(cli.app, ["plan", "x", "--repo", "demo", "--model", "nobody=x"]).exit_code == 2
    assert runner.invoke(cli.app, ["plan", "x", "--repo", "no-such-repo-or-dir"]).exit_code == 2


def test_runs_and_show(env):
    proj, _, install = env
    assert "(no runs)" in runner.invoke(cli.app, ["runs", "--repo", "demo"]).output
    install({"claude": [result(good_plan())]})
    runner.invoke(cli.app, ["plan", "first thing", "--repo", "demo"])
    r = runner.invoke(cli.app, ["runs", "--repo", "demo"])
    assert r.exit_code == 0 and "first-thing" in r.output and "planned" in r.output
    run_id = list_runs(proj)[0].run_id
    s = runner.invoke(cli.app, ["show", run_id, "--repo", "demo"])
    assert s.exit_code == 0 and "1. [s1]" in s.output
    assert runner.invoke(cli.app, ["show", run_id[:12], "--repo", "demo"]).exit_code == 0  # unique prefix
    assert runner.invoke(cli.app, ["show", "nope", "--repo", "demo"]).exit_code == 1


def test_run_id_format_and_collision(tmp_path):
    now = datetime(2026, 11, 10, 8, 30, 5, tzinfo=timezone.utc)
    a = create_run(tmp_path, "Add `orch version`!", now)
    b = create_run(tmp_path, "Add `orch version`!", now)
    assert a.name == "20261110T083005Z-add-orch-version"
    assert b.name == "20261110T083005Z-add-orch-version-2"
    with pytest.raises(RunNotFound):
        find_run(tmp_path, "2026111")  # ambiguous prefix
    assert slugify("???") == "run"
