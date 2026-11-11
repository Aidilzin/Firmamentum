import pytest
from git_helpers import git, make_repo

from orch import workspace
from orch.workspace import GuardError, WorkspaceError


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "proj")


def test_create_puts_worktree_outside_repo_on_new_branch(repo, isolated_worktree_root):
    wt = workspace.create(repo, "proj", "run1")
    assert wt.path == isolated_worktree_root / "proj" / "run1"
    assert repo.resolve() not in wt.path.resolve().parents
    assert wt.path.resolve() not in repo.resolve().parents
    assert wt.branch == "orch/run1"
    assert git(wt.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "orch/run1"
    assert wt.base_sha == git(repo, "rev-parse", "HEAD").strip()


def test_create_refuses_dirty_repo(repo):
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="uncommitted"):
        workspace.create(repo, "proj", "run1")


def test_create_ignores_orch_run_folder(repo):
    (repo / ".orch" / "runs").mkdir(parents=True)
    (repo / ".orch" / "runs" / "x.txt").write_text("x", encoding="utf-8")
    workspace.create(repo, "proj", "run1")  # does not raise


def test_changed_files_commit_and_diff(repo):
    wt = workspace.create(repo, "proj", "run1")
    (wt.path / "new.py").write_text("print(1)\n", encoding="utf-8")
    (wt.path / "README.md").write_text("edited\n", encoding="utf-8")
    assert workspace.changed_files(wt.path) == ["README.md", "new.py"]
    sha = workspace.commit_step(wt.path, "s1", "Add new", ["README.md", "new.py"])
    assert git(wt.path, "log", "-1", "--format=%s").strip() == "orch(s1): Add new"
    assert workspace.changed_files(wt.path) == []
    d = workspace.diff(repo, f"{sha}^", sha)
    assert "+print(1)" in d and "+edited" in d
    assert "+print(1)" in workspace.diff(repo, wt.base_sha, wt.branch)  # works from the main repo


def test_commit_only_stages_given_paths(repo):
    wt = workspace.create(repo, "proj", "run1")
    (wt.path / "a.py").write_text("a\n", encoding="utf-8")
    (wt.path / "junk.tmp").write_text("junk\n", encoding="utf-8")
    workspace.commit_step(wt.path, "s1", "A", ["a.py"])
    assert workspace.changed_files(wt.path) == ["junk.tmp"]


def test_changed_files_sees_deletes_and_renames(repo):
    wt = workspace.create(repo, "proj", "run1")
    git(wt.path, "mv", "README.md", "DOC.md")
    assert workspace.changed_files(wt.path) == ["DOC.md", "README.md"]


def test_reset_to_head_discards_uncommitted_work(repo):
    wt = workspace.create(repo, "proj", "run1")
    (wt.path / "stray.py").write_text("x\n", encoding="utf-8")
    (wt.path / "README.md").write_text("bad\n", encoding="utf-8")
    workspace.reset_to_head(wt.path)
    assert workspace.changed_files(wt.path) == []
    assert (wt.path / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_remove_keeps_branch(repo):
    wt = workspace.create(repo, "proj", "run1")
    assert workspace.remove(repo, "proj", "run1") is True
    assert not wt.path.exists()
    assert "orch/run1" in git(repo, "branch", "--list", "orch/run1")
    assert workspace.remove(repo, "proj", "run1") is False  # nothing left


# ---- path guard ----


def test_guard_refuses_main_repo_for_every_destructive_call(repo):
    (repo / "precious.txt").write_text("keep me\n", encoding="utf-8")
    with pytest.raises(GuardError):
        workspace.reset_to_head(repo)
    with pytest.raises(GuardError):
        workspace.remove(repo, "proj", "run1", path=repo)
    with pytest.raises(GuardError):
        workspace.commit_step(repo, "s1", "nope")
    assert (repo / "precious.txt").read_text(encoding="utf-8") == "keep me\n"
    assert (repo / ".git").is_dir()


def test_guard_refuses_dir_inside_root_that_is_not_a_worktree(isolated_worktree_root, repo):
    fake = isolated_worktree_root / "proj" / "bogus"
    fake.mkdir(parents=True)
    with pytest.raises(GuardError):
        workspace.reset_to_head(fake)
    with pytest.raises(GuardError):
        workspace.remove(repo, "proj", "bogus", path=fake)


def test_guard_refuses_the_root_itself_and_parents(isolated_worktree_root, repo, tmp_path):
    isolated_worktree_root.mkdir(parents=True, exist_ok=True)
    for p in (isolated_worktree_root, tmp_path):
        with pytest.raises(GuardError):
            workspace.reset_to_head(p)


def test_guard_refuses_a_worktree_of_another_location(repo, tmp_path):
    """A real linked worktree, but outside the orch root, is still refused."""
    other = tmp_path / "elsewhere"
    git(repo, "worktree", "add", "-q", "-b", "other", str(other))
    with pytest.raises(GuardError):
        workspace.reset_to_head(other)


def test_guard_refuses_a_git_file_redirected_to_the_main_repo(repo):
    wt = workspace.create(repo, "proj", "run1")
    (wt.path / ".git").unlink()
    (wt.path / ".git").write_text(f"gitdir: {(repo / '.git').as_posix()}\n", encoding="utf-8")
    (wt.path / "x.py").write_text("x = 1\n", encoding="utf-8")
    head = git(repo, "rev-parse", "HEAD").strip()
    with pytest.raises(GuardError):
        workspace.commit_step(wt.path, "s1", "nope")
    with pytest.raises(GuardError):
        workspace.reset_to_head(wt.path)
    assert git(repo, "rev-parse", "HEAD").strip() == head  # nothing landed on the main checkout


def test_guard_refuses_the_wrong_branch(repo):
    wt = workspace.create(repo, "proj", "run1")
    git(wt.path, "checkout", "-q", "-b", "sneaky")
    with pytest.raises(GuardError):
        workspace.reset_to_head(wt.path, "orch/run1")
    assert workspace.check_head(wt.path, wt.base_sha, "orch/run1")


def test_check_head_sees_an_agent_commit(repo):
    wt = workspace.create(repo, "proj", "run1")
    assert workspace.check_head(wt.path, wt.base_sha, "orch/run1") is None
    (wt.path / "a.py").write_text("a = 1\n", encoding="utf-8")
    git(wt.path, "add", "-A")
    git(wt.path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "sneaky")
    assert "HEAD moved" in workspace.check_head(wt.path, wt.base_sha, "orch/run1")


def _hook(repo_dir, name, marker):
    d = repo_dir / ".githooks"
    d.mkdir(exist_ok=True)
    h = d / name
    h.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\nexit 1\n", encoding="utf-8", newline="\n")
    return d


def test_commit_does_not_run_repo_hooks(repo, tmp_path):
    wt = workspace.create(repo, "proj", "run1")
    marker = tmp_path / "hook-ran.txt"
    _hook(wt.path, "pre-commit", marker)
    git(wt.path, "config", "core.hooksPath", ".githooks")  # tracked hooks folder, as husky or .githooks set up
    (wt.path / "a.py").write_text("a = 1\n", encoding="utf-8")
    sha = workspace.commit_step(wt.path, "s1", "A", ["a.py"])  # a failing pre-commit hook would abort this
    assert sha and not marker.exists()


def test_merge_does_not_run_repo_hooks(repo, tmp_path):
    wt = workspace.create(repo, "proj", "run1")
    (wt.path / "a.py").write_text("a = 1\n", encoding="utf-8")
    workspace.commit_step(wt.path, "s1", "A", ["a.py"])
    marker = tmp_path / "merge-hook-ran.txt"
    hooks = _hook(repo, "pre-merge-commit", marker)
    git(repo, "config", "core.hooksPath", str(hooks))
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "b.py").write_text("b = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "side change")
    workspace.merge_no_ff(repo, "orch/run1", "merge")  # a real merge commit, so pre-merge-commit would fire
    assert not marker.exists()
