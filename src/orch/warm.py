"""The folder the sandbox warm step installs from (docs/sandbox-design.md, "The warm step").

The warm step is the only container with network, and `uv sync` can run build code. So it never sees the run's worktree
(agent-written files). It gets the run's base commit, which is your own commit, plus the dependency files you approved
for this run. A changed dependency file that nobody approved stops the run before anything starts.
"""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import workspace
from .checks import RULE_DEPENDENCY, matches, norm_path
from .sandbox import SandboxError


def _git_names(wt: Path, *args: str) -> list[str]:
    out = workspace.git(wt, *args).stdout
    return [n for n in out.split("\0") if n]


def changed_dependency_files(wt: Path, base_sha: str, patterns: list[str]) -> list[str]:
    """Dependency files (by the `post_checks.dependency` globs) that differ between `base_sha` and the worktree:
    modified, added, deleted, committed by orch in an earlier step, or not committed yet."""
    names = _git_names(wt, "diff", "--name-only", "--no-renames", "-z", base_sha)
    names += _git_names(wt, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted({n for n in names if matches(n, patterns)})


def approved_dependency_files(meta: dict) -> set[str]:
    """Files whose `new_dependency` change you approved in any step of this run (compared in normalised form)."""
    out: set[str] = set()
    for st in (meta.get("steps") or {}).values():
        for a in st.get("approved") or []:
            if a.get("rule") == RULE_DEPENDENCY:
                out.add(norm_path(a["file"]))
    return out


@contextmanager
def warm_tree(
    repo: Path, wt: Path, base_sha: str, run_id: str, patterns: list[str], approved: set[str], root: Path | None = None
) -> Iterator[Path]:  # fmt: skip
    """Export `base_sha` to a scratch folder, lay the approved dependency files from the worktree over it, hand out the
    folder, and delete it afterwards. Raises SandboxError for an unapproved dependency change, before anything is
    exported. `approved` holds normalised paths (see `approved_dependency_files`)."""
    changed = changed_dependency_files(wt, base_sha, patterns)
    unapproved = [f for f in changed if norm_path(f) not in approved]
    if unapproved:
        raise SandboxError(
            "dependency change not approved: " + ", ".join(unapproved)
            + ". Approve the step (`orch approve`) or revert the file; nothing was run."
        )
    dest = (root or workspace.warm_root()) / run_id
    shutil.rmtree(dest, ignore_errors=True)  # a leftover of a crashed run
    try:
        workspace.export_tree(repo, base_sha, dest)
        wt_real, dest_real = Path(wt).resolve(), dest.resolve()
        for name in changed:
            src, target = (wt_real / name), (dest_real / name)
            if dest_real not in target.resolve().parents or wt_real not in src.resolve().parents:
                raise SandboxError(f"dependency file outside the project: {name}")
            if src.is_symlink() or (src.exists() and not src.is_file()):
                raise SandboxError(f"dependency file is not a regular file: {name}")
            if src.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)
            else:  # the step deleted it
                target.unlink(missing_ok=True)
        yield dest
    finally:
        shutil.rmtree(dest, ignore_errors=True)
