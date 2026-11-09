import pytest

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
