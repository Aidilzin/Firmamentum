"""The warm step's source folder: the base commit plus approved dependency files, never agent-written code."""

import pytest
from git_helpers import git, make_repo

from orch import workspace
from orch.config import PostChecksConfig
from orch.sandbox import SandboxError
from orch.warm import approved_dependency_files, changed_dependency_files, warm_tree

PATTERNS = PostChecksConfig().dependency


@pytest.fixture
def run(tmp_path):
    repo = make_repo(tmp_path / "proj")
    (repo / "pyproject.toml").write_text("[project]\nname='a'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("lock-1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "deps")
    wt = workspace.create(repo, "demo", "run-1")

    def warm(approved=()):
        return warm_tree(repo, wt.path, wt.base_sha, "run-1", PATTERNS, {a.lower() for a in approved})

    return repo, wt, warm


def test_the_export_is_the_base_commit_without_agent_files_or_git(run):
    repo, wt, warm = run
    (wt.path / "src").mkdir()
    (wt.path / "src" / "agent.py").write_text("print('hi')\n", encoding="utf-8")
    (wt.path / "conftest.py").write_text("import socket\n", encoding="utf-8")
    with warm() as folder:
        assert (folder / "README.md").read_text(encoding="utf-8") == "hello\n"
        assert (folder / "uv.lock").is_file() and not (folder / ".git").exists()
        assert not (folder / "src" / "agent.py").exists() and not (folder / "conftest.py").exists()
        assert workspace.warm_root() in folder.parents  # next to the worktrees, never inside one
        assert wt.path not in folder.parents
    assert not folder.exists()


def test_the_folder_is_deleted_even_when_the_body_raises(run):
    _, _, warm = run
    with pytest.raises(RuntimeError):
        with warm() as folder:
            raise RuntimeError("boom")
    assert not folder.exists()


def test_an_unapproved_dependency_change_is_refused_before_anything_is_exported(run):
    _, wt, warm = run
    (wt.path / "uv.lock").write_text("lock-evil\n", encoding="utf-8")
    with pytest.raises(SandboxError, match="dependency change not approved: uv.lock"):
        with warm():
            pytest.fail("must not get here")
    assert not (workspace.warm_root() / "run-1").exists()


def test_an_approved_dependency_file_is_laid_over_the_base(run):
    _, wt, warm = run
    (wt.path / "uv.lock").write_text("lock-2\n", encoding="utf-8")
    (wt.path / "src").mkdir()
    (wt.path / "src" / "agent.py").write_text("x\n", encoding="utf-8")
    with warm(approved={"uv.lock"}) as folder:
        assert (folder / "uv.lock").read_text(encoding="utf-8") == "lock-2\n"
        assert (folder / "pyproject.toml").read_text(encoding="utf-8") == "[project]\nname='a'\n"  # untouched, from base
        assert not (folder / "src").exists()  # only dependency files are taken from the worktree


def test_every_changed_dependency_file_needs_its_own_approval(run):
    _, wt, warm = run
    (wt.path / "uv.lock").write_text("lock-2\n", encoding="utf-8")
    (wt.path / "pyproject.toml").write_text("[project]\nname='evil'\n", encoding="utf-8")
    with pytest.raises(SandboxError, match="pyproject.toml"):
        with warm(approved={"uv.lock"}):
            pass


def test_a_change_committed_in_an_earlier_step_still_counts(run):
    _, wt, warm = run
    (wt.path / "uv.lock").write_text("lock-2\n", encoding="utf-8")
    workspace.commit_step(wt.path, "s1", "deps")  # an earlier step, approved then and committed by orch
    assert changed_dependency_files(wt.path, wt.base_sha, PATTERNS) == ["uv.lock"]
    with pytest.raises(SandboxError):
        with warm():
            pass
    with warm(approved={"uv.lock"}) as folder:
        assert (folder / "uv.lock").read_text(encoding="utf-8") == "lock-2\n"


def test_a_new_untracked_dependency_file_and_a_nested_one_are_seen(run):
    _, wt, warm = run
    (wt.path / "web").mkdir()
    (wt.path / "web" / "package.json").write_text("{}", encoding="utf-8")
    (wt.path / "requirements-dev.txt").write_text("x\n", encoding="utf-8")
    assert changed_dependency_files(wt.path, wt.base_sha, PATTERNS) == ["requirements-dev.txt", "web/package.json"]
    with warm(approved={"web/package.json", "requirements-dev.txt"}) as folder:
        assert (folder / "web" / "package.json").is_file() and (folder / "requirements-dev.txt").is_file()


def test_a_deleted_dependency_file_is_deleted_in_the_export_too(run):
    _, wt, warm = run
    (wt.path / "uv.lock").unlink()
    with warm(approved={"uv.lock"}) as folder:
        assert not (folder / "uv.lock").exists() and (folder / "pyproject.toml").exists()


def test_a_dependency_file_that_is_a_symlink_is_refused(run, tmp_path):
    _, wt, warm = run
    (wt.path / "uv.lock").unlink()
    try:
        (wt.path / "uv.lock").symlink_to(tmp_path / "elsewhere")
    except OSError:
        pytest.skip("symlinks need a privilege on this machine")
    # either refusal is right: the link points outside the project, or it is not a regular file
    with pytest.raises(SandboxError, match="outside the project|not a regular file"):
        with warm(approved={"uv.lock"}):
            pass


def test_a_leftover_folder_of_a_crashed_run_is_replaced(run):
    _, _, warm = run
    stale = workspace.warm_root() / "run-1"
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("x", encoding="utf-8")
    with warm() as folder:
        assert not (folder / "old.txt").exists() and (folder / "README.md").exists()


def test_approvals_come_from_every_step_but_only_for_the_dependency_rule():
    meta = {"steps": {
        "s1": {"approved": [{"file": "UV.lock", "rule": "new_dependency"}, {"file": "a.py", "rule": "executes_later"}]},
        "s2": {"approved": [{"file": "web/package.json", "rule": "new_dependency"}]},
        "s3": {},
    }}  # fmt: skip
    assert approved_dependency_files(meta) == {"uv.lock", "web/package.json"}
    assert approved_dependency_files({}) == set()
