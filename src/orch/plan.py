"""Plan model. One validation path: `Plan.model_validate(...)` checks shape and the step graph."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import RiskFlag

SCHEMA_PATH = Path(__file__).parent / "schemas" / "plan.json"
MAX_STEPS = 8


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^s[0-9]+$", description='Step id: "s1", "s2", ...')
    title: str
    description: str = Field(description="What to change and why, enough for a builder with no other context.")
    files: list[str] = Field(description="Files expected to change or be created.")
    depends_on: list[str] = Field(description="Ids of steps that must finish first.")
    acceptance: str = Field(description="How we know it is done: tests to add or pass.")
    risk_flags: list[RiskFlag] = Field(description="Only the approval rules that apply to this step.")


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str
    assumptions: list[str]
    needs_input: bool
    question: str | None = Field(description="Required when needs_input is true, otherwise null.")
    test_command: str
    steps: list[Step] = Field(max_length=MAX_STEPS)

    @model_validator(mode="after")
    def _check(self) -> Plan:
        errors: list[str] = []
        if self.needs_input and not (self.question and self.question.strip()):
            errors.append("question is required when needs_input is true")
        if not self.needs_input and not self.steps:
            errors.append("a plan needs at least one step unless needs_input is true")
        ids = [s.id for s in self.steps]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            errors.append(f"duplicate step ids: {', '.join(dupes)}")
        known = set(ids)
        for s in self.steps:
            for d in s.depends_on:
                if d not in known:
                    errors.append(f"step {s.id} depends on unknown step {d!r}")
                elif d == s.id:
                    errors.append(f"step {s.id} depends on itself")
        if not errors and (stuck := _cycle_members(self.steps)):
            errors.append(f"dependency cycle among steps: {', '.join(stuck)}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def topo_order(self) -> list[Step]:
        """Dependencies first; ties keep the original step order."""
        return _toposort(self.steps)[0]


def _toposort(steps: list[Step]) -> tuple[list[Step], list[str]]:
    """Kahn's algorithm. Returns (ordered steps, ids left over because of a cycle)."""
    by_id = {s.id: s for s in steps}
    remaining = {s.id: set(s.depends_on) for s in steps}
    order: list[Step] = []
    while remaining:
        ready = [i for i in by_id if i in remaining and not remaining[i]]
        if not ready:
            return order, list(remaining)
        nxt = ready[0]  # lowest original position first
        order.append(by_id[nxt])
        del remaining[nxt]
        for deps in remaining.values():
            deps.discard(nxt)
    return order, []


def _cycle_members(steps: list[Step]) -> list[str]:
    return _toposort(steps)[1]


def plan_json_schema() -> dict:
    return Plan.model_json_schema()


def load_schema_file() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def format_plan(plan: Plan) -> str:
    """Numbered list in execution order, with dependencies, files and risk flags."""
    lines = [f"Goal: {plan.goal}", f"Test command: {plan.test_command}"]
    if plan.assumptions:
        lines.append("Assumptions:")
        lines += [f"  - {a}" for a in plan.assumptions]
    if plan.needs_input:
        lines.append(f"Question: {plan.question}")
    number = {s.id: n for n, s in enumerate(plan.topo_order(), 1)}
    for s in plan.topo_order():
        lines.append("")
        lines.append(f"{number[s.id]}. [{s.id}] {s.title}")
        lines.append(f"   {s.description}")
        lines.append("   depends on: " + (", ".join(f"{d} (#{number[d]})" for d in s.depends_on) or "none"))
        lines.append("   files: " + (", ".join(s.files) or "none"))
        lines.append(f"   acceptance: {s.acceptance}")
        lines.append("   risk flags: " + (", ".join(s.risk_flags) or "none"))
    return "\n".join(lines)


if __name__ == "__main__":  # regenerate the committed schema: python -m orch.plan
    SCHEMA_PATH.write_text(json.dumps(plan_json_schema(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {SCHEMA_PATH}")
