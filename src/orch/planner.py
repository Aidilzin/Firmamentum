"""Planner flow: optional scout -> planner call -> validate -> one retry -> result."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from .adapters.base import AgentAdapter, AgentRequest, AgentResult, ErrorKind
from .adapters.claude_code import CONFINE_ARGS
from .router import Routed, call_role
from .config import Config, Entry, ResolvedRepo
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
    routes: list[dict] = field(default_factory=list)  # {role, entry, fallback} per call
    paused: bool = False  # every entry for a role is rate limited / cooling
    resume_after: datetime | None = None
    cancelled: bool = False  # the caller's cancel token was set (Ctrl-C): no retry, no fallback

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
    agy has no tool allow-list; `--mode plan --sandbox` is its read-only mode (plan mode alone blocks commands;
    file writes were not exercised, so the sandbox is added as a second layer).
    """
    if agent == "claude":
        return [*CONFINE_ARGS, "--tools", READ_ONLY_TOOLS, "--permission-prompts", "none"]
    return ["--mode", "plan", "--sandbox"]


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


def _agent_args(role: Entry, system_text: str | None) -> tuple[list[str], str | None]:
    """(extra CLI args, text to put inline in the prompt). claude takes system text by flag; agy cannot."""
    args = read_only_args(role.agent)
    if system_text and role.agent == "claude":
        return args + ["--append-system-prompt", system_text], None
    return args, system_text


def _with_env(build: Callable[[Entry], AgentRequest], repo: ResolvedRepo) -> Callable[[Entry], AgentRequest]:
    return lambda entry: build(entry).model_copy(update={"env_passthrough": list(repo.env_passthrough)})


def run_scout(
    request: str, repo: ResolvedRepo, cfg: Config, get_adapter: AdapterFactory, cancel: threading.Event | None = None
) -> Routed:
    prompt = SCOUT_PROMPT + f"\n\nThe planner will work on this request (for focus only): {request}"

    def build(entry: Entry) -> AgentRequest:
        args, _ = _agent_args(entry, None)
        return AgentRequest(prompt=prompt, cwd=repo.path, model=entry.model, timeout_s=SCOUT_TIMEOUT_S, extra_args=args)

    return call_role("scout", _with_env(build, repo), get_adapter, cfg, cancel=cancel)


def _feedback(errors: list[str]) -> str:
    return (
        "Your previous plan failed validation:\n"
        + "\n".join(f"- {e}" for e in errors)
        + "\n\nReturn the corrected, complete plan as JSON matching the schema."
    )


def _note(outcome: PlannerOutcome, role: str, routed: Routed) -> None:
    if routed.entry is not None:
        outcome.routes.append({"role": role, "entry": routed.entry.label, "fallback": routed.fallback})


def _pause(outcome: PlannerOutcome, role: str, routed: Routed) -> PlannerOutcome:
    outcome.paused, outcome.resume_after = True, routed.resume_after
    when = routed.resume_after.astimezone().strftime("%Y-%m-%d %H:%M %Z") if routed.resume_after else "unknown"
    outcome.error = f"{role}: every entry is rate limited or cooling; try again after {when}"
    return outcome


CANCELLED_TEXT = "planning cancelled by you"


def cancelled_outcome(outcome: PlannerOutcome | None = None) -> PlannerOutcome:
    """Mark planning as cancelled by the user. Whatever was already called stays in the outcome (tokens, raw replies)."""
    outcome = outcome or PlannerOutcome(plan=None, attempts=[])
    outcome.plan, outcome.cancelled, outcome.error = None, True, CANCELLED_TEXT
    return outcome


def _was_cancelled(result: AgentResult | None) -> bool:
    return result is not None and result.error_kind == ErrorKind.CANCELLED


def make_plan(
    request: str,
    repo: ResolvedRepo,
    cfg: Config,
    get_adapter: AdapterFactory,
    *,
    use_scout: bool = False,
    cancel: threading.Event | None = None,
) -> PlannerOutcome:
    """`cancel` (set by Ctrl-C or the shell) kills the running call; the outcome then has `cancelled=True`."""
    outcome = PlannerOutcome(plan=None, attempts=[])
    scout_text: str | None = None
    if use_scout:
        routed = run_scout(request, repo, cfg, get_adapter, cancel)
        _note(outcome, "scout", routed)
        if routed.paused:
            if routed.rate_limited is not None:
                outcome.scout = routed.rate_limited
            return _pause(outcome, "scout", routed)
        outcome.scout = routed.result
        if _was_cancelled(outcome.scout):
            return cancelled_outcome(outcome)
        if not outcome.scout.ok:
            outcome.error = f"scout failed: {outcome.scout.error_kind.value}: {outcome.scout.text}"
            return outcome
        scout_text = outcome.scout.text

    schema = load_schema_file()
    instructions = planner_instructions()
    first_entry: list[Entry] = []  # the entry of the first planner call (a session id only works there)

    def base(inline: str | None) -> str:
        return _build_prompt(request, repo, cfg, scout_text, inline)

    def call(make: Callable[[Entry, str | None], tuple[str, str | None]]) -> Routed:
        """make(entry, inline_role_text) -> (prompt, resume_id); runs through the router."""

        def build(entry: Entry) -> AgentRequest:
            args, inline = _agent_args(entry, instructions)
            p, resume_id = make(entry, inline)
            return AgentRequest(prompt=p, cwd=repo.path, model=entry.model, timeout_s=PLANNER_TIMEOUT_S,
                                json_schema=schema, resume_id=resume_id, extra_args=args)  # fmt: skip

        routed = call_role("planner", _with_env(build, repo), get_adapter, cfg, cancel=cancel)
        _note(outcome, "planner", routed)
        return routed

    first_routed = call(lambda e, inline: (base(inline), None))
    if first_routed.paused:
        if first_routed.rate_limited is not None:
            outcome.attempts.append(Attempt(first_routed.rate_limited))
        return _pause(outcome, "planner", first_routed)
    first = first_routed.result
    first_entry.append(first_routed.entry)
    if _was_cancelled(first):
        outcome.attempts.append(Attempt(first))
        return cancelled_outcome(outcome)
    if not first.ok:
        outcome.attempts.append(Attempt(first))
        outcome.error = f"planner failed: {first.error_kind.value}: {first.text}"
        return outcome
    plan, errors = validate_result(first)
    outcome.attempts.append(Attempt(first, errors))
    if plan:
        outcome.plan = plan
        return outcome

    previous = json.dumps(first.structured, indent=2) if first.structured is not None else first.text

    def fresh(inline: str | None) -> str:
        return f"{base(inline)}\n\n## Previous attempt (invalid)\n{previous}\n\n{_feedback(errors)}"

    # One retry. Resume the session if there is one and the same entry is still usable (resume is untested
    # against the real CLIs); if the resumed call itself fails to run, fall back to a fresh call.
    second: AgentResult | None = None
    if first.session_id:
        resumed = call(
            lambda e, inline: (_feedback(errors), first.session_id) if e == first_entry[0] else (fresh(inline), None)
        )
        if resumed.paused:
            if resumed.rate_limited is not None:
                outcome.attempts.append(Attempt(resumed.rate_limited))
            return _pause(outcome, "planner", resumed)
        second = resumed.result
        if _was_cancelled(second):
            outcome.attempts.append(Attempt(second))
            return cancelled_outcome(outcome)
        if not second.ok:
            outcome.attempts.append(Attempt(second, ["resume call failed: " + second.error_kind.value]))
            second = None
    if second is None:
        again = call(lambda e, inline: (fresh(inline), None))
        if again.paused:
            if again.rate_limited is not None:
                outcome.attempts.append(Attempt(again.rate_limited))
            return _pause(outcome, "planner", again)
        second = again.result
    if _was_cancelled(second):
        outcome.attempts.append(Attempt(second))
        return cancelled_outcome(outcome)
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
