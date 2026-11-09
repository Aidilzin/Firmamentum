"""Planner flow: optional scout -> planner call -> validate -> one retry -> result."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from .adapters.base import AgentAdapter, AgentRequest, AgentResult
from .budget import run_agent
from .config import Config, ResolvedRepo, RoleConfig
from .plan import Plan, load_schema_file

ROLES_DIR = Path(__file__).parent / "roles"
READ_ONLY_TOOLS = "Read,Grep,Glob"
PLANNER_TIMEOUT_S = 900
SCOUT_TIMEOUT_S = 600

SCOUT_PROMPT = (
    "Summarise this repository for a planning agent, in under 400 words. Cover: purpose, language and "
    "tooling, directory layout, the main modules and what each does, how tests are run, and any "
    "conventions in CLAUDE.md/README. Read only; do not change anything. Reply with plain text."
)

AdapterFactory = Callable[[str], AgentAdapter]


@dataclass
class Attempt:
    result: AgentResult
    validation_errors: list[str] = field(default_factory=list)


@dataclass
class PlannerOutcome:
    plan: Plan | None
    attempts: list[Attempt]
    scout: AgentResult | None = None
    error: str | None = None  # set when plan is None

    @property
    def results(self) -> list[AgentResult]:
        return ([self.scout] if self.scout else []) + [a.result for a in self.attempts]

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens for r in self.results)


def planner_instructions() -> str:
    return (ROLES_DIR / "planner.md").read_text(encoding="utf-8")


def read_only_args(agent: str) -> list[str]:
    """Flags that keep a worker read-only (see docs/phase2-notes.md).

    claude: restrict the built-in toolset to read tools, and auto-deny anything that would prompt.
    agy has no tool allow-list; `--mode plan` is its read-only mode.
    """
    if agent == "claude":
        return ["--tools", READ_ONLY_TOOLS, "--permission-prompts", "none"]
    return ["--mode", "plan"]


def extract_json(text: str) -> object:
    """Parse JSON from text, tolerating a ```json fence or surrounding prose."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start : end + 1])
        raise


def validate_result(result: AgentResult) -> tuple[Plan | None, list[str]]:
    """Turn an agent reply into a Plan, or a list of human-readable errors."""
    data: object = result.structured
    if data is None:
        try:
            data = extract_json(result.text)
        except json.JSONDecodeError as e:
            return None, [f"the reply was not valid JSON: {e}"]
    try:
        return Plan.model_validate(data), []
    except ValidationError as e:
        errs = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            msg = err["msg"].removeprefix("Value error, ")
            errs.append(f"{loc}: {msg}" if loc else msg)
        return None, errs


def _build_prompt(
    request: str, repo: ResolvedRepo, cfg: Config, scout_text: str | None, system_inline: str | None
) -> str:
    parts: list[str] = []
    if system_inline:
        parts += [system_inline, ""]
    parts += ["## Request", request, "", "## Repo facts"]
    if repo.test_command:
        parts.append(f"Test command to use: {repo.test_command}")
    else:
        parts.append("Test command: not configured; find it in the repo.")
    parts.append("Valid risk_flags values: " + ", ".join(cfg.approval_rules))
    if scout_text:
        parts += ["", "## Repo summary (from a scout agent; verify anything important)", scout_text]
    parts += ["", "Return only the plan as JSON matching the schema."]
    return "\n".join(parts)


def _agent_args(role: RoleConfig, system_text: str | None) -> tuple[list[str], str | None]:
    """(extra CLI args, text to put inline in the prompt). claude takes system text by flag; agy cannot."""
    args = read_only_args(role.agent)
    if system_text and role.agent == "claude":
        return args + ["--append-system-prompt", system_text], None
    return args, system_text


def run_scout(request: str, repo: ResolvedRepo, cfg: Config, get_adapter: AdapterFactory) -> AgentResult:
    role = cfg.roles["scout"]
    args, _ = _agent_args(role, None)
    prompt = SCOUT_PROMPT + f"\n\nThe planner will work on this request (for focus only): {request}"
    req = AgentRequest(prompt=prompt, cwd=repo.path, model=role.model, timeout_s=SCOUT_TIMEOUT_S, extra_args=args)
    return run_agent(get_adapter(role.agent), req)


def _feedback(errors: list[str]) -> str:
    return (
        "Your previous plan failed validation:\n"
        + "\n".join(f"- {e}" for e in errors)
        + "\n\nReturn the corrected, complete plan as JSON matching the schema."
    )


def make_plan(
    request: str,
    repo: ResolvedRepo,
    cfg: Config,
    get_adapter: AdapterFactory,
    *,
    use_scout: bool = False,
) -> PlannerOutcome:
    outcome = PlannerOutcome(plan=None, attempts=[])
    scout_text: str | None = None
    if use_scout:
        outcome.scout = run_scout(request, repo, cfg, get_adapter)
        if not outcome.scout.ok:
            outcome.error = f"scout failed: {outcome.scout.error_kind.value}: {outcome.scout.text}"
            return outcome
        scout_text = outcome.scout.text

    role = cfg.roles["planner"]
    adapter = get_adapter(role.agent)
    schema = load_schema_file()
    args, inline = _agent_args(role, planner_instructions())
    prompt = _build_prompt(request, repo, cfg, scout_text, inline)

    def call(p: str, resume_id: str | None = None) -> AgentResult:
        req = AgentRequest(
            prompt=p,
            cwd=repo.path,
            model=role.model,
            timeout_s=PLANNER_TIMEOUT_S,
            json_schema=schema,
            resume_id=resume_id,
            extra_args=args,
        )
        return run_agent(adapter, req)

    first = call(prompt)
    if not first.ok:
        outcome.attempts.append(Attempt(first))
        outcome.error = f"planner failed: {first.error_kind.value}: {first.text}"
        return outcome
    plan, errors = validate_result(first)
    outcome.attempts.append(Attempt(first, errors))
    if plan:
        outcome.plan = plan
        return outcome

    # One retry. Resume the session if there is one (resume is untested against the real CLIs);
    # if the resumed call itself fails to run, fall back to a fresh call that includes the bad output.
    second: AgentResult | None = None
    if first.session_id:
        second = call(_feedback(errors), resume_id=first.session_id)
        if not second.ok:
            outcome.attempts.append(Attempt(second, ["resume call failed: " + second.error_kind.value]))
            second = None
    if second is None:
        previous = json.dumps(first.structured, indent=2) if first.structured is not None else first.text
        second = call(f"{prompt}\n\n## Previous attempt (invalid)\n{previous}\n\n{_feedback(errors)}")
    if not second.ok:
        outcome.attempts.append(Attempt(second))
        outcome.error = f"planner retry failed: {second.error_kind.value}: {second.text}"
        return outcome
    plan, errors2 = validate_result(second)
    outcome.attempts.append(Attempt(second, errors2))
    if plan:
        outcome.plan = plan
    else:
        outcome.error = "the planner returned an invalid plan twice. Last errors: " + "; ".join(errors2)
    return outcome
