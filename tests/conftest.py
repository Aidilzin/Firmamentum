import pytest

from orch import budget


REAL_DEFAULT_DB_PATH = budget.default_db_path


@pytest.fixture(autouse=True)
def isolated_usage_db(tmp_path, monkeypatch):
    """Never touch the real global usage.db from tests."""
    monkeypatch.setattr(budget, "default_db_path", lambda: tmp_path / "usage-test.db")
