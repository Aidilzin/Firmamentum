"""Builder report model. `src/orch/schemas/build.json` is generated from it: `python -m orch.build`."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_PATH = Path(__file__).parent / "schemas" / "build.json"


class BuildReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    files_changed: list[str] = Field(description="Paths relative to the working directory.")
    tests_added: list[str]
    blocked: bool = Field(description="True only if the step could not be completed.")
    question: str | None = Field(description="Required when blocked is true, otherwise null.")
    notes: str

    @model_validator(mode="after")
    def _check(self) -> BuildReport:
        if self.blocked and not (self.question and self.question.strip()):
            raise ValueError("question is required when blocked is true")
        return self


def build_json_schema() -> dict:
    return BuildReport.model_json_schema()


def load_schema_file() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    SCHEMA_PATH.write_text(json.dumps(build_json_schema(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {SCHEMA_PATH}")
