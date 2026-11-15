import pytest

import git_helpers
from orch import budget, workspace


REAL_DEFAULT_DB_PATH = budget.default_db_path


@pytest.fixture(autouse=True)
def isolated_usage_db(tmp_path, monkeypatch):
    """Never touch the real global usage.db from tests."""
    monkeypatch.setattr(budget, "default_db_path", lambda: tmp_path / "usage-test.db")


@pytest.fixture(autouse=True)
def isolated_worktree_root(tmp_path, monkeypatch):
    """Never create worktrees under the real %LOCALAPPDATA%\\orch\\wt from tests."""
    root = tmp_path / "orch-wt"
    monkeypatch.setattr(workspace, "worktree_root", lambda: root)
    return root


# A test is `slow` when it makes a git repo or runs the executor: it spawns 30-70 git processes (about 45 ms each on Windows).
# `uv run pytest -q -m "not slow"` is the quick loop; the full suite still runs before every commit.
REPO_FIXTURES = {"env", "g", "started", "repo"}  # fixtures that make a git repo (directly or through test_executor.env)


def _uses_git(item) -> bool:
    if REPO_FIXTURES & set(item.fixturenames):
        return True
    ns = vars(item.module)  # a module that imports the git helpers builds repos in its own helpers and tests
    return ns.get("make_repo") is git_helpers.make_repo or ns.get("git") is git_helpers.git


def pytest_collection_modifyitems(items):
    for item in items:
        if _uses_git(item):
            item.add_marker(pytest.mark.slow)
