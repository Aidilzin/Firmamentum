import json

import pytest
from plan_factory import good_plan, step
from pydantic import ValidationError

from orch.plan import Plan, SCHEMA_PATH, format_plan, plan_json_schema


def test_good_plan_parses():
    p = Plan.model_validate(good_plan())
    assert [s.id for s in p.steps] == ["s1", "s2"]


def test_duplicate_ids():
    with pytest.raises(ValidationError, match="duplicate step ids: s1"):
        Plan.model_validate(good_plan(steps=[step("s1"), step("s1")]))


def test_unknown_dependency():
    with pytest.raises(ValidationError, match="unknown step 's9'"):
        Plan.model_validate(good_plan(steps=[step("s1", deps=["s9"])]))


def test_self_dependency():
    with pytest.raises(ValidationError, match="depends on itself"):
        Plan.model_validate(good_plan(steps=[step("s1", deps=["s1"])]))


def test_cycle():
    steps = [step("s1", deps=["s2"]), step("s2", deps=["s3"]), step("s3", deps=["s1"]), step("s4")]
    with pytest.raises(ValidationError, match="cycle among steps: s1, s2, s3"):
        Plan.model_validate(good_plan(steps=steps))


def test_needs_input_without_question():
    with pytest.raises(ValidationError, match="question is required"):
        Plan.model_validate(good_plan(needs_input=True, question=None, steps=[]))
    with pytest.raises(ValidationError, match="question is required"):
        Plan.model_validate(good_plan(needs_input=True, question="  ", steps=[]))


def test_needs_input_with_question_and_no_steps_is_valid():
    p = Plan.model_validate(good_plan(needs_input=True, question="Which database?", steps=[]))
    assert p.question == "Which database?" and p.steps == []


def test_no_steps_without_needs_input():
    with pytest.raises(ValidationError, match="at least one step"):
        Plan.model_validate(good_plan(steps=[]))


def test_more_than_eight_steps():
    with pytest.raises(ValidationError):
        Plan.model_validate(good_plan(steps=[step(f"s{i}") for i in range(1, 10)]))


def test_unknown_risk_flag_and_extra_field():
    with pytest.raises(ValidationError):
        Plan.model_validate(good_plan(steps=[step("s1", flags=["made_up"])]))
    bad = good_plan()
    bad["surprise"] = 1
    with pytest.raises(ValidationError):
        Plan.model_validate(bad)


def test_bad_step_id_format():
    with pytest.raises(ValidationError):
        Plan.model_validate(good_plan(steps=[step("step-one")]))


def test_topological_order():
    steps = [step("s1", deps=["s3"]), step("s2"), step("s3", deps=["s2"]), step("s4", deps=["s1"])]
    order = [s.id for s in Plan.model_validate(good_plan(steps=steps)).topo_order()]
    assert order == ["s2", "s3", "s1", "s4"]


def test_topological_order_keeps_original_order_when_independent():
    p = Plan.model_validate(good_plan(steps=[step("s2"), step("s1"), step("s3")]))
    assert [s.id for s in p.topo_order()] == ["s2", "s1", "s3"]


def test_schema_file_is_in_sync_with_model():
    on_disk = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert on_disk == plan_json_schema(), "run: uv run python -m orch.plan"


def test_format_plan_shows_deps_files_flags():
    text = format_plan(Plan.model_validate(good_plan()))
    assert "1. [s1]" in text and "2. [s2]" in text
    assert "depends on: s1 (#1)" in text
    assert "files: src/s1.py" in text
    assert "risk flags: new_dependency" in text
