import pytest

import git_helpers
from docker_sim import DockerSim
from orch import budget, sandbox, workspace


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


@pytest.fixture(autouse=True)
def fake_docker(monkeypatch):
    """Tests never start Docker: the engine probe answers "running, image present" unless a test replaces it, and the
    run / short-call seams talk to a DockerSim (tests/docker_sim.py). A test can take this fixture to inspect the sim."""
    sandbox.clear_cache()
    monkeypatch.setattr(sandbox, "docker_probe", lambda image: sandbox.ProbeResult(True, True, "sha256:fake", "0.0-test"))
    sim = DockerSim()
    monkeypatch.setattr(sandbox, "docker_run", sim.docker_run)
    monkeypatch.setattr(sandbox, "docker_call", sim.docker_call)
    yield sim
    sandbox.clear_cache()


# A test is `slow` when it makes a git repo or runs the executor: it spawns 30-70 git processes (about 45 ms each on Windows).
# `uv run pytest -q -m "not slow"` is the quick loop; the full suite still runs before every commit.
REPO_FIXTURES = {"env", "g", "started", "repo"}  # fixtures that make a git repo (directly or through test_executor.env)


def _uses_git(item) -> bool:
    if REPO_FIXTURES & set(item.fixturenames):
        return True
    ns = vars(item.module)  # a module that imports the git helpers builds repos in its own helpers and tests
    return ns.get("make_repo") is git_helpers.make_repo or ns.get("git") is git_helpers.git


# Tests that kill process trees, race threads or wait on timers. Under `-n` they all run on ONE worker (group "timing"),
# so a busy machine never squeezes them next to a whole other file; their timeouts stay as they are.
TIMING_TESTS = {
    "test_process.py": {"test_timeout_kills_process", "test_timeout_kills_grandchild", "test_interrupt_kills_the_child"},
    "test_cancel.py": {
        "test_cancel_kills_the_child_and_reports_cancelled_not_timeout", "test_cancel_kills_the_grandchild_too",
        "test_stdin_survives_the_poll_loop", "test_timeout_still_wins_when_the_token_is_never_set",
        "test_cancel_from_another_thread_while_the_agent_blocks", "test_stop_now_from_another_thread_only_cancels",
    },
    "test_events.py": {
        "test_cancel_during_a_blocked_call_from_another_thread", "test_a_slow_consumer_thread_does_not_slow_the_executor",
        "test_rendering_while_events_arrive_does_not_race",
    },
    "test_runlock.py": {
        "test_lock_held_by_another_process_dies_with_it", "test_beat_rewrites_the_heartbeat_without_losing_the_lock",
        "test_write_meta_survives_a_reader_that_holds_the_file_open",
    },
}  # fmt: skip


def _xdist_group(item) -> str:
    """`--dist loadgroup` keeps one file on one worker (what loadfile does), except the timing tests, which share one group."""
    module = item.path.name
    if getattr(item, "originalname", item.name) in TIMING_TESTS.get(module, ()):
        return "timing"
    return module


def pytest_collection_modifyitems(items):
    for item in items:
        if _uses_git(item):
            item.add_marker(pytest.mark.slow)
        item.add_marker(pytest.mark.xdist_group(name=_xdist_group(item)))
