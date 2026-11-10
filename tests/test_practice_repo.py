"""scripts/make_practice_repo.py, run in a temp dir with `uv lock` skipped. No real CLI."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from git_helpers import git

from orch.config import load_config, resolve_repo
from orch.executor import check_hidden_tests

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make_practice_repo.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("make_practice_repo", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_creates_expected_files_and_one_commit(mod, tmp_path):
    repo, hidden = mod.make_practice_repo(tmp_path / "orch-practice", lock=False)
    assert hidden == tmp_path / "orch-practice-hidden"
    tracked = set(git(repo, "ls-files").split())
    assert tracked == {
        ".gitignore", ".python-version", "pyproject.toml", "src/practice/__init__.py", "src/practice/text.py",
        "tests/test_text.py",
    }  # fmt: skip
    assert git(repo, "log", "--format=%s").splitlines() == ["Initial practice repo"]
    assert git(repo, "status", "--porcelain").strip() == ""
    ignore = (repo / ".gitignore").read_text(encoding="utf-8").split()
    assert {".venv/", "__pycache__/", ".pytest_cache/", ".orch/"} <= set(ignore)
    py = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "practice"' in py and "pytest" in py and ">=3.12" in py
    text = (repo / "src/practice/text.py").read_text(encoding="utf-8")
    assert "def slugify" in text and "def old_slug" in text and "Deprecated" in text


def test_no_hidden_test_file_is_committed_or_inside_the_repo(mod, tmp_path):
    repo, hidden = mod.make_practice_repo(tmp_path / "orch-practice", lock=False)
    assert not [f for f in git(repo, "ls-files").split() if "slug_extra" in f or "hidden" in f]
    assert not list(repo.rglob("test_slug_extra.py")) and not (repo / "hidden_tests").exists()
    assert (hidden / "test_slug_extra.py").is_file() and not (hidden / ".git").exists()
    assert hidden.resolve() not in repo.resolve().parents and repo.resolve() not in hidden.resolve().parents
    all_history = git(repo, "log", "--all", "--name-only", "--format=").split()
    assert "test_slug_extra.py" not in " ".join(all_history)


def _pytest(cwd, *args):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=cwd,
                          capture_output=True, text=True)  # fmt: skip


def test_committed_tests_pass_and_hidden_tests_fail_on_the_basic_slugify(mod, tmp_path):
    repo, hidden = mod.make_practice_repo(tmp_path / "orch-practice", lock=False)
    ok = _pytest(repo)
    assert ok.returncode == 0 and "5 passed" in ok.stdout, ok.stdout + ok.stderr
    names = check_hidden_tests(hidden, repo)  # the marker dotfile is not copied
    assert names == ["test_slug_extra.py"]
    shutil.copy(hidden / names[0], repo / "tests" / names[0])
    bad = _pytest(repo)
    assert bad.returncode != 0 and "3 failed" in bad.stdout, bad.stdout


def test_refuses_existing_unless_force_and_only_deletes_its_own(mod, tmp_path):
    p = tmp_path / "orch-practice"
    mod.make_practice_repo(p, lock=False)
    with pytest.raises(mod.PracticeError, match="already exists"):
        mod.make_practice_repo(p, lock=False)
    (p / "extra.txt").write_text("x", encoding="utf-8")
    repo, hidden = mod.make_practice_repo(p, force=True, lock=False)  # replaced together
    assert not (repo / "extra.txt").exists() and (hidden / "test_slug_extra.py").is_file()
    assert git(repo, "log", "--format=%s").splitlines() == ["Initial practice repo"]
    # a folder that is not ours is never deleted
    other = tmp_path / "mine"
    other.mkdir()
    (other / "keep.txt").write_text("precious", encoding="utf-8")
    with pytest.raises(mod.PracticeError, match="no marker"):
        mod.make_practice_repo(other, force=True, lock=False)
    assert (other / "keep.txt").read_text(encoding="utf-8") == "precious"
    # hidden folder exists without marker: also refused
    q = tmp_path / "second"
    mod.hidden_path_for(q).mkdir()
    (mod.hidden_path_for(q) / "keep.txt").write_text("precious", encoding="utf-8")
    with pytest.raises(mod.PracticeError, match="no marker"):
        mod.make_practice_repo(q, force=True, lock=False)
    assert not q.exists()


def test_cli_main_reports_and_refuses(mod, tmp_path, capsys):
    p = tmp_path / "cli-practice"
    assert mod.main(["--path", str(p), "--no-lock"]) == 0
    assert "Created" in capsys.readouterr().out
    assert mod.main(["--path", str(p), "--no-lock"]) == 2
    assert "already exists" in capsys.readouterr().err


def test_config_has_practice_repo():
    cfg = load_config()
    r = cfg.repos["practice"]
    assert (r.path, r.test_command, r.retest_command) == ("../orch-practice", "uv run pytest -q", "uv run pytest -q --lf")
    assert resolve_repo(cfg, "practice").path.name == "orch-practice"
    assert yaml.safe_load(open(Path(__file__).resolve().parents[1] / "config.yaml", encoding="utf-8"))["repos"]["practice"]
