"""The work behind the commands, as functions that return data.

Nothing here prints, reads the terminal or exits: the one-shot commands (cli.py) and the shell are both thin printers
over this module. An expected refusal is raised as `ServiceError`, which carries the text and the one-shot exit code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .runs import is_stale

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
