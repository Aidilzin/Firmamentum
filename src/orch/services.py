"""The work behind the commands, as functions that return data.

Nothing here prints, reads the terminal or exits: the one-shot commands (cli.py) and the shell are both thin printers
over this module. An expected refusal is raised as `ServiceError`, which carries the text and the one-shot exit code."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import workspace
from .adapters.base import AgentAdapter
from .config import Config, ResolvedRepo
from .events import RunEvent
from .executor import ExecError, ExecOutcome, Executor, check_hidden_tests
from .merge import MergeError, MergeResult, merge_run
from .planner import PlannerOutcome, make_plan
from .runs import RunNotFound, create_run, is_stale, load_meta, load_plan, save_outcome, write_meta
from .view import parse_utc

EXIT_CODES = {"done": 0, "failed": 1, "needs_input": 2, "awaiting_approval": 2, "paused": 3, "rejected": 1, "cancelled": 130}
STOPPED_STATUSES = ("awaiting_approval", "needs_input", "failed")


class ServiceError(Exception):
    """An expected refusal (bad state, bad argument, git problem). Printers show `Error: {message}` and use `code`."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.message, self.code = message, code


# ---------- what the human should do next ----------

_COMMANDS = {
    "answer_plan": 'answer {run_id} "<your answer>"',
    "answer_step": 'answer {run_id} --step {step} "<your answer>"',
    "approve": "approve {run_id} --step {step}",
    "approve_retry": "approve {run_id} --step {step} --retry",
    "reject": "reject {run_id} --step {step}",
    "resume": "resume {run_id}",
    "diff_step": "diff {run_id} --step {step}",
    "diff_run": "diff {run_id}",
    "merge": "merge {run_id}",
    "exec": "exec {run_id}",
}
_ALWAYS_COMMENTED = ("approve", "approve_retry", "answer_step")  # the reason is printed even when it is missing


@dataclass(frozen=True)
class NextAction:
    """One thing the human can do next. `str()` is the line the one-shot commands print after `Next: `."""

    kind: str  # a key of _COMMANDS, or step_failed (no command: the step cannot be continued)
    run_id: str
    step: str | None = None
    reason: str | None = None  # the text after `#`
    repo: str | None = None  # configured repo name: adds ` --repo NAME`
    alt: NextAction | None = None  # reject (beside approve), merge (after diff_run)
    path: Path | None = None  # step_failed: where the step's files are

    @property
    def command(self) -> str | None:
        """The exact command line, or None when there is nothing to run."""
        template = _COMMANDS.get(self.kind)
        if template is None:
            return None
        return "orch " + template.format(run_id=self.run_id, step=self.step) + (f" --repo {self.repo}" if self.repo else "")

    def __str__(self) -> str:
        if self.kind == "step_failed":
            return f"(step {self.step} failed: {self.reason}; see {self.path}; start a new run)"
        if self.kind in ("approve", "approve_retry"):
            return f"{self.command}   # {self.reason}  (or: {self.alt.command})"  # type: ignore[union-attr]
        if self.kind == "diff_run":
            return f"{self.command}   # {self.reason}, then: {self.alt.command}"  # type: ignore[union-attr]
        if self.reason is None and self.kind not in _ALWAYS_COMMENTED:
            return str(self.command)
        return f"{self.command}   # {self.reason}"


def next_actions(meta: dict, run_dir: Path, repo_name: str | None = None) -> list[NextAction]:
    """What the human should do next, one action per stopped step. `run_dir` is needed for two facts that are not in
    `meta`: whether the worktree still exists, and whether a live process holds the run."""
    rid = run_dir.name
    repo = repo_name or None

    def act(kind: str, step: str | None = None, reason: str | None = None, **kw) -> NextAction:
        return NextAction(kind, rid, step, reason, repo, **kw)

    steps: dict = meta.get("steps") or {}
    if meta.get("status") == "needs_input" and not steps:
        return [act("answer_plan", reason="the plan needs an answer")]
    out: list[NextAction] = []
    for sid, st in steps.items():
        status, stage = st["status"], st.get("stage")
        if status == "awaiting_approval" and stage == "repairs_exhausted":
            out.append(act("approve_retry", sid, "another set of repair rounds", alt=act("reject", sid)))
        elif status == "awaiting_approval":
            out.append(act("approve", sid, st.get("rule"), alt=act("reject", sid)))
        elif status == "needs_input":
            out.append(act("answer_step", sid, st.get("question")))
        elif status == "failed":
            out.append(act("step_failed", sid, st.get("error"), path=run_dir / "steps" / sid))
        elif status == "pending" and st.get("error") == "rate limited":
            out.append(act("resume", reason="after the usage limit resets"))
        elif status == "pending" and st.get("error") == "cancelled":
            out.append(act("resume", reason="stopped by you"))
    ex = meta.get("exec") or {}
    if ex.get("worktree") and Path(ex["worktree"]).is_dir():
        for sid, st in steps.items():
            if st["status"] in STOPPED_STATUSES and not st.get("commit"):
                out.append(act("diff_step", sid, "the uncommitted change of this step"))
    if meta.get("status") in ("paused", "cancelled") and not out:
        out.append(act("resume"))
    if is_stale(run_dir, meta):
        out.append(act("resume", reason="the run died mid-step"))
    if meta.get("status") == "done" and not meta.get("merged_sha"):
        out.append(act("diff_run", reason="review", alt=act("merge")))
    return out


# ---------- diff, merge, clean ----------


@dataclass
class DiffPart:
    """One piece of a diff. `header` marks changes that are not committed yet (a stopped step)."""

    text: str
    header: str | None = None


def _uncommitted_header(sid: str, st: dict) -> str:
    return f"# step {sid} ({st.get('status')}): UNCOMMITTED changes in the worktree (git diff HEAD plus new files)\n"


def diff_run(target: ResolvedRepo, run_dir: Path, step: str | None = None) -> list[DiffPart]:
    """The diff of a run (base..branch, then the uncommitted change of each stopped step) or of one step."""
    meta = load_meta(run_dir)
    ex = meta.get("exec")
    if not ex:
        raise ServiceError("this run was never executed.")
    steps: dict = meta.get("steps") or {}
    wt = Path(ex["worktree"])
    try:
        if step:
            s = steps.get(step)
            if s is None:
                raise ServiceError(f"no step {step!r}")
            if s.get("commit"):
                return [DiffPart(workspace.diff(target.path, f"{s['commit']}^", s["commit"]))]
            if wt.is_dir():
                return [DiffPart(workspace.diff_uncommitted(wt), _uncommitted_header(step, s))]
            return [DiffPart(f"(step {step} has no commit)")]
        parts = [DiffPart(workspace.diff(target.path, ex["base_sha"], ex["branch"]))]
        for sid, s in steps.items():  # stopped steps have changes that are not committed yet
            if s.get("status") in STOPPED_STATUSES and not s.get("commit") and wt.is_dir():
                parts.append(DiffPart(workspace.diff_uncommitted(wt), _uncommitted_header(sid, s)))
        return parts
    except workspace.WorkspaceError as e:
        raise ServiceError(str(e)) from e


def merge(target: ResolvedRepo, run_dir: Path, allow_main: bool = False) -> MergeResult:
    """Merge the run's branch into the current branch (--no-ff), then run the tests. Never pushes."""
    try:
        return merge_run(target, run_dir, allow_main=allow_main)
    except (MergeError, workspace.WorkspaceError, RunNotFound) as e:
        raise ServiceError(str(e)) from e


@dataclass
class CleanResult:
    executed: bool  # False: the run never had a worktree
    removed: bool = False
    worktree: str | None = None
    branch: str | None = None


def clean_run(target: ResolvedRepo, run_dir: Path) -> CleanResult:
    """Remove the run's worktree. The branch is kept."""
    ex = load_meta(run_dir).get("exec")
    if not ex:
        return CleanResult(executed=False)
    try:
        removed = workspace.remove(target.path, ex["repo_name"], run_dir.name, Path(ex["worktree"]))
    except workspace.WorkspaceError as e:
        raise ServiceError(str(e)) from e
    return CleanResult(True, removed, ex["worktree"], ex["branch"])


# ---------- executing a run ----------

DRIVE_ACTIONS = ("start", "resume", "approve", "reject", "answer")


def build_executor(
    target: ResolvedRepo,
    run_dir: Path,
    cfg: Config,
    get_adapter: Callable[[str], AgentAdapter],
    *,
    log: Callable[[str], None],
    on_event: Callable[[RunEvent], None] | None = None,
    hidden_tests: Path | None = None,
    cancel: threading.Event | None = None,
) -> Executor:
    """The executor for one run. The caller owns the view (`log`, `on_event`) and the stop handling
    (`ex.cancel()`, `ex.stop_now()`), then hands the executor to `drive`."""
    return Executor(target, run_dir, cfg, get_adapter, log=log, on_event=on_event, hidden_tests=hidden_tests, cancel=cancel)


def drive(ex: Executor, action: str = "start", **kw) -> ExecOutcome:
    """Run one executor action (start, resume, approve, reject, answer) until the run stops. Blocks."""
    if action not in DRIVE_ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    try:
        return getattr(ex, action)(**kw)
    except (ExecError, workspace.WorkspaceError, RunNotFound) as e:
        raise ServiceError(str(e), 2) from e


def resume_blocked_until(meta: dict, force: bool = False, now: datetime | None = None) -> datetime | None:
    """When a paused run may be resumed, if that is still in the future and `force` is not set; else None."""
    after = parse_utc(meta.get("resume_after")) if meta.get("status") == "paused" else None
    if after is None or force or after <= (now or datetime.now(timezone.utc)):
        return None
    return after


# ---------- planning ----------

PLAN_EXIT_CODES = {"planned": 0, "needs_input": 2, "failed": 1, "paused": 3, "cancelled": 130}


@dataclass
class PlanResult:
    """A planning attempt: the stored run, what the planner returned, and what to do next."""

    run_dir: Path
    outcome: PlannerOutcome
    meta: dict
    next: list[NextAction] = field(default_factory=list)

    @property
    def state(self) -> str:
        """planned | needs_input | failed | paused (every entry is rate limited) | cancelled."""
        if self.outcome.plan is not None:
            return "needs_input" if self.outcome.plan.needs_input else "planned"
        return "paused" if self.outcome.paused else "failed"

    @property
    def exit_code(self) -> int:
        return PLAN_EXIT_CODES[self.state]


def planned_models(outcome: PlannerOutcome, cfg: Config, roles: list[str]) -> dict[str, str]:
    """Entry that actually served each role (falls back to the first configured entry when no call was made)."""
    used = {r["role"]: r["entry"] for r in outcome.routes}
    return {r: used.get(r) or cfg.roles[r][0].label for r in roles}


def _plan_next(outcome: PlannerOutcome, meta: dict, run_dir: Path, repo_name: str | None) -> list[NextAction]:
    if outcome.plan is None:
        return []
    if outcome.plan.needs_input:
        return next_actions(meta, run_dir, repo_name)
    return [NextAction("exec", run_dir.name, repo=repo_name or None)]


def check_run_request(target: ResolvedRepo, hidden_tests: Path | None = None) -> None:
    """Refuse `run` before any quota is spent: no test command, or a hidden-tests folder the builder could see."""
    if not target.test_command:
        raise ServiceError("this repo has no test_command in config.yaml; refusing to run.", 2)
    if hidden_tests is not None:
        try:
            check_hidden_tests(hidden_tests, target.path)
        except ExecError as e:
            raise ServiceError(str(e), 2) from e


def plan_run(
    request: str,
    target: ResolvedRepo,
    cfg: Config,
    get_adapter: Callable[[str], AgentAdapter],
    *,
    scout: bool = False,
) -> PlanResult:
    """Create a run, ask the planner (after an optional scout) and store what came back."""
    run_dir = create_run(target.path, request)
    outcome = make_plan(request, target, cfg, get_adapter, use_scout=scout)
    models = planned_models(outcome, cfg, (["scout"] if scout else []) + ["planner"])
    meta = save_outcome(run_dir, outcome, models)
    return PlanResult(run_dir, outcome, meta, _plan_next(outcome, meta, run_dir, target.name))


def answer_plan(
    target: ResolvedRepo,
    cfg: Config,
    run_dir: Path,
    text: str,
    get_adapter: Callable[[str], AgentAdapter],
) -> PlanResult:
    """Answer the planner's question and plan again.

    When the new attempt gives no plan (it failed or every entry is rate limited) the run stays `needs_input` and
    request.md is not touched, so the same answer can simply be given again (audit M11)."""
    stored = load_plan(run_dir)
    meta = load_meta(run_dir)
    if stored is None or not stored.needs_input:
        raise ServiceError("this run's plan is not waiting for an answer.", 2)
    request = (run_dir / "request.md").read_text(encoding="utf-8").strip()
    clarified = f"{request}\n\n## Clarification\nQuestion: {stored.question}\nAnswer: {text}"
    raw = run_dir / "planner_raw.json"
    if raw.exists():  # keep every attempt
        n = 1
        while (run_dir / f"planner_raw.{n}.json").exists():
            n += 1
        raw.rename(run_dir / f"planner_raw.{n}.json")
    outcome = make_plan(clarified, target, cfg, get_adapter)
    models = meta.get("models") or planned_models(outcome, cfg, ["planner"])
    new_meta = save_outcome(run_dir, outcome, models, status_without_plan="needs_input")
    new_meta["total_tokens"] += int(meta.get("total_tokens", 0))
    old_roles = meta.get("plan_role_tokens") or {"planner": int(meta.get("total_tokens", 0))}
    new_meta["plan_role_tokens"] = {
        r: new_meta["plan_role_tokens"].get(r, 0) + old_roles.get(r, 0) for r in {*new_meta["plan_role_tokens"], *old_roles}
    }
    write_meta(run_dir, new_meta)
    if outcome.plan is None:
        return PlanResult(run_dir, outcome, new_meta, next_actions(new_meta, run_dir, target.name))
    (run_dir / "request.md").write_text(clarified + "\n", encoding="utf-8")
    return PlanResult(run_dir, outcome, new_meta, _plan_next(outcome, new_meta, run_dir, target.name))
