"""Reviewer output model. `src/orch/schemas/review.json` is generated from it: `python -m orch.review`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_PATH = Path(__file__).parent / "schemas" / "review.json"
BLOCKING = ("blocker", "major")


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: Literal["blocker", "major", "minor"]
    file: str
    line: int | None = Field(description="Line number, or null if not tied to one line.")
    problem: str
    fix: str


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["approve", "reject"]
    issues: list[Issue]
    summary: str

    @model_validator(mode="after")
    def _check(self) -> Review:
        blocking = [i for i in self.issues if i.severity in BLOCKING]
        if blocking and self.verdict != "reject":
            raise ValueError("verdict must be 'reject' when there is a blocker or major issue")
        if not blocking and self.verdict != "approve":
            raise ValueError("verdict must be 'approve' when there are no blocker or major issues")
        return self

    @property
    def blocking_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.severity in BLOCKING]


def review_json_schema() -> dict:
    return Review.model_json_schema()


def load_schema_file() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    SCHEMA_PATH.write_text(json.dumps(review_json_schema(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {SCHEMA_PATH}")
