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


# ---------- export_tree ----------


def test_export_tree_writes_the_commit_without_git_untracked_or_ignored_files(repo, tmp_path):
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "a")
    sha = git(repo, "rev-parse", "HEAD").strip()
    (repo / "untracked.txt").write_text("x", encoding="utf-8")
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")  # uncommitted edit
    dest = tmp_path / "out"
    workspace.export_tree(repo, sha, dest)
    assert (dest / "src" / "a.py").read_text(encoding="utf-8") == "a = 1\n"
    assert not (dest / ".git").exists() and not (dest / "untracked.txt").exists() and not (dest / ".orch").exists()


def test_export_tree_refuses_a_dirty_destination_and_a_flag_as_revision(repo, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "x").write_text("x", encoding="utf-8")
    with pytest.raises(workspace.WorkspaceError, match="not empty"):
        workspace.export_tree(repo, "HEAD", dest)
    for bad in ("--output=evil", "-x", "", "a b", "a;b"):
        with pytest.raises(workspace.WorkspaceError, match="bad revision"):
            workspace.export_tree(repo, bad, tmp_path / "o2")
    assert not (tmp_path / "o2").exists()


def test_export_tree_unknown_revision(repo, tmp_path):
    with pytest.raises(workspace.WorkspaceError, match="git archive"):
        workspace.export_tree(repo, "no-such-branch", tmp_path / "out")
    assert not (tmp_path / "out").exists()


# ---------- merge candidate ----------


def _commit_file(where, name, text, msg):
    (where / name).write_text(text, encoding="utf-8")
    git(where, "add", "-A")
    git(where, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    return git(where, "rev-parse", "HEAD").strip()


def _two_sides(repo):
    wt = workspace.create(repo, "proj", "run1")
    _commit_file(wt.path, "a.py", "a = 1\n", "branch change")
    git(repo, "checkout", "-q", "-b", "work")
    _commit_file(repo, "b.py", "b = 1\n", "work change")
    return wt


def test_merge_candidate_is_a_two_parent_commit_and_touches_nothing(repo):
    _two_sides(repo)
    target, branch = workspace.commit_sha(repo, "work"), workspace.commit_sha(repo, "orch/run1")
    before = git(repo, "status", "--porcelain=v1", "--branch")
    cand = workspace.merge_candidate(repo, target, branch, "Merge orch run: x")
    assert git(repo, "rev-list", "--parents", "-n1", cand).split() == [cand, target, branch]
    assert git(repo, "log", "-1", "--format=%s", cand).strip() == "Merge orch run: x"
    assert git(repo, "ls-tree", "--name-only", cand).split() == [".gitignore", "README.md", "a.py", "b.py"]
    assert workspace.commit_sha(repo, "work") == target and git(repo, "status", "--porcelain=v1", "--branch") == before
    assert not (repo / "a.py").exists()  # the checkout never received the branch's file


def test_merge_candidate_conflict_names_the_files_and_changes_nothing(repo):
    wt = workspace.create(repo, "proj", "run1")
    _commit_file(wt.path, "README.md", "from the branch\n", "branch")
    git(repo, "checkout", "-q", "-b", "work")
    _commit_file(repo, "README.md", "from work\n", "work")
    target, branch = workspace.commit_sha(repo, "work"), workspace.commit_sha(repo, "orch/run1")
    with pytest.raises(workspace.MergeConflict) as e:
        workspace.merge_candidate(repo, target, branch, "m")
    assert e.value.files == ["README.md"] and "README.md" in str(e.value)
    assert workspace.commit_sha(repo, "work") == target and workspace.is_clean(repo)


def test_merge_candidate_refuses_names_that_are_not_full_ids(repo):
    with pytest.raises(WorkspaceError, match="full commit id"):
        workspace.merge_candidate(repo, "work", "orch/run1", "m")
    with pytest.raises(WorkspaceError, match="full commit id"):
        workspace.add_detached(repo, "proj", "merge-x", "--orphan")


def test_is_ancestor(repo):
    wt = _two_sides(repo)
    target, branch = workspace.commit_sha(repo, "work"), workspace.commit_sha(repo, "orch/run1")
    assert not workspace.is_ancestor(repo, branch, target)
    assert workspace.is_ancestor(repo, wt.base_sha, target) and workspace.is_ancestor(repo, target, target)


def test_a_detached_worktree_runs_no_repo_hooks_and_is_removable(repo, tmp_path, isolated_worktree_root):
    _two_sides(repo)
    target, branch = workspace.commit_sha(repo, "work"), workspace.commit_sha(repo, "orch/run1")
    cand = workspace.merge_candidate(repo, target, branch, "m")
    marker = tmp_path / "checkout-hook-ran.txt"
    hooks = _hook(repo, "post-checkout", marker)
    git(repo, "config", "core.hooksPath", str(hooks))
    tmp = workspace.add_detached(repo, "proj", "merge-run1", cand)
    assert tmp == isolated_worktree_root / "proj" / "merge-run1" and not marker.exists()
    assert (tmp / "a.py").is_file() and (tmp / "b.py").is_file()
    assert git(tmp, "rev-parse", "HEAD").strip() == cand and workspace.current_branch(tmp) is None
    with pytest.raises(WorkspaceError, match="already exists"):
        workspace.add_detached(repo, "proj", "merge-run1", cand)
    assert workspace.remove(repo, "proj", "merge-run1", path=tmp) and not tmp.exists()


def test_fast_forward_moves_the_branch_and_runs_no_hooks(repo, tmp_path):
    _two_sides(repo)
    target, branch = workspace.commit_sha(repo, "work"), workspace.commit_sha(repo, "orch/run1")
    cand = workspace.merge_candidate(repo, target, branch, "m")
    marker = tmp_path / "merge-hook-ran.txt"
    git(repo, "config", "core.hooksPath", str(_hook(repo, "post-merge", marker)))
    workspace.fast_forward(repo, cand)
    assert workspace.commit_sha(repo, "work") == cand and (repo / "a.py").is_file() and not marker.exists()


def test_fast_forward_refuses_after_the_target_moved(repo):
    _two_sides(repo)
    target, branch = workspace.commit_sha(repo, "work"), workspace.commit_sha(repo, "orch/run1")
    cand = workspace.merge_candidate(repo, target, branch, "m")
    moved = _commit_file(repo, "c.py", "c = 1\n", "you committed meanwhile")
    with pytest.raises(WorkspaceError, match="fast-forward refused"):
        workspace.fast_forward(repo, cand)
    assert workspace.commit_sha(repo, "work") == moved


def test_git_version_reads_major_and_minor(monkeypatch):
    class P:
        stdout = "git version 2.56.0.windows.4\n"

    monkeypatch.setattr(workspace.subprocess, "run", lambda *a, **k: P())
    assert workspace.git_version() == (2, 56)
